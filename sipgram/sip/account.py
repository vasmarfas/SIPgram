"""SIP user agent: registration, dialogs (calls), transactions with UDP retransmission."""
from __future__ import annotations

import asyncio
import enum
import ipaddress
import logging
import random
import re
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..config import SipConfig
from .codecs import AudioCodec, build_codec, codec_list
from .digest import build_authorization, parse_challenge
from .message import NameAddr, SipMessage, SipUri
from .rtp import RtpPortPool, RtpSession
from .sdp import Sdp, SdpCodec, answer_for, local_offer, match_answer
from .transport import SipTransport, TcpTransport, UdpTransport

log = logging.getLogger("sipgram.sip")
siplog = logging.getLogger("sipgram.sip.trace")

T1 = 0.5
T2 = 4.0
TIMER_B = 32.0
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, NOTIFY, UPDATE, MESSAGE"
REASONS = {
    100: "Trying", 180: "Ringing", 183: "Session Progress", 200: "OK", 202: "Accepted",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 415: "Unsupported Media Type",
    420: "Bad Extension", 480: "Temporarily Unavailable", 481: "Call/Transaction Does Not Exist",
    486: "Busy Here", 487: "Request Terminated", 488: "Not Acceptable Here",
    500: "Server Internal Error", 501: "Not Implemented", 503: "Service Unavailable",
    600: "Busy Everywhere", 603: "Decline",
}


def reason_text(code: int) -> str:
    return REASONS.get(code, "Unknown")


def new_branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex[:20]


def new_tag() -> str:
    return uuid.uuid4().hex[:10]


class CallState(enum.Enum):
    NEW = "new"
    CALLING = "calling"
    RINGING = "ringing"
    EARLY = "early"
    CONNECTED = "connected"
    TERMINATED = "terminated"


class CallError(Exception):
    def __init__(self, code: int, text: str = ""):
        super().__init__(f"{code} {text or reason_text(code)}")
        self.code = code
        self.text = text or reason_text(code)


@dataclass
class ClientTxn:
    request: SipMessage
    addr: tuple[str, int]
    future: asyncio.Future
    timer: asyncio.TimerHandle | None = None
    interval: float = T1
    deadline: float = 0.0
    provisional: Callable[[SipMessage], None] | None = None
    dialog: SipCall | None = None


class SipCall:
    """One INVITE dialog with its RTP stream. Audio in/out is 16-bit PCM at the codec rate."""

    def __init__(self, account: SipAccount, call_id: str, incoming: bool):
        self.account = account
        self.call_id = call_id
        self.incoming = incoming
        self.state = CallState.NEW
        self.local_tag = new_tag()
        self.remote_tag: str | None = None
        self.local_uri = ""
        self.remote_uri = ""
        self.remote_target = ""
        self.route_set: list[str] = []
        self.local_cseq = random.randint(1, 1000)
        self.remote_cseq = 0
        self.invite_branch = ""
        self.invite_request: SipMessage | None = None
        self.last_final: SipMessage | None = None
        self.last_ack: SipMessage | None = None
        self.final_timer: asyncio.TimerHandle | None = None
        self.final_interval = T1
        self.final_deadline = 0.0
        self.ack_received = False
        self.rtp: RtpSession | None = None
        self.codec: AudioCodec | None = None
        self.local_sdp: Sdp | None = None
        self.remote_sdp: Sdp | None = None
        self.caller_number = ""
        self.caller_name = ""
        self.dialed = ""
        self.remote_hold = False
        self.end_code = 0
        self.end_reason = ""
        self.created = time.time()
        self.connected_at = 0.0
        self.on_state: Callable[[SipCall, CallState], None] | None = None
        self.on_audio: Callable[[bytes], None] | None = None
        self.on_dtmf: Callable[[str], None] | None = None
        self.answered: asyncio.Future = account.loop.create_future()
        self.ended: asyncio.Future = account.loop.create_future()
        self.headers: dict[str, str] = {}
        self.cancelled = False
        self.auth_retry = False
        self.local_hold = False
        self.dtmf_busy = False
        self.reinvite_pending = False
        self.refer_result: asyncio.Future | None = None
        self.tag: object = None

    # ---- state helpers ----

    @property
    def account_name(self) -> str:
        return getattr(self.account.cfg, "name", "") or self.account.cfg.username

    @property
    def active(self) -> bool:
        return self.state not in (CallState.TERMINATED,)

    @property
    def rate(self) -> int:
        return self.codec.rate if self.codec else 8000

    def _set_state(self, state: CallState) -> None:
        if self.state == state or self.state == CallState.TERMINATED:
            return
        self.state = state
        if state == CallState.CONNECTED and not self.connected_at:
            self.connected_at = time.time()
        log.info("call %s: %s", self.call_id[:8], state.value)
        if self.on_state:
            try:
                self.on_state(self, state)
            except Exception:
                log.exception("on_state handler failed")

    def _terminate(self, code: int = 0, reason: str = "") -> None:
        if self.state == CallState.TERMINATED:
            return
        self.end_code = code
        self.end_reason = reason
        if self.final_timer:
            self.final_timer.cancel()
            self.final_timer = None
        if self.rtp:
            self.rtp.close()
            self.rtp = None
        if not self.answered.done():
            if code and code >= 300:
                self.answered.set_exception(CallError(code, reason))
            else:
                self.answered.set_exception(CallError(487, reason or "Terminated"))
            self.answered.exception()
        self.account._calls.pop(self.call_id, None)
        self._set_state(CallState.TERMINATED)
        if not self.ended.done():
            self.ended.set_result(code)

    # ---- media ----

    def _setup_rtp(self, codec_desc: SdpCodec, remote: Sdp | None) -> None:
        codec = build_codec(codec_desc)
        if codec is None:
            raise CallError(488, "codec not supported")
        self.codec = codec
        assert self.rtp is not None
        self.rtp.payload_type = codec.pt
        self.rtp.clock_rate = codec.rate
        if self.rtp.rtcp is not None:
            self.rtp.rtcp.stats.clock_rate = codec.rate
        self.rtp.on_payload = self._rtp_payload
        self.rtp.on_dtmf = self._rtp_dtmf
        if remote is not None:
            self._apply_remote_sdp(remote)

    def _apply_remote_sdp(self, remote: Sdp) -> None:
        self.remote_sdp = remote
        if self.rtp and remote.port:
            self.rtp.set_remote(remote.conn_ip, remote.port)
            self.rtp.dtmf_pt = remote.dtmf_pt_for(self.codec.rate if self.codec else None)
        self.remote_hold = remote.direction in ("sendonly", "inactive") or remote.port == 0

    def _rtp_payload(self, payload: bytes, marker: bool, ts: int, seq: int) -> None:
        if self.codec and self.on_audio:
            try:
                self.on_audio(self.codec.decode(payload))
            except Exception:
                log.exception("audio handler failed")

    def _rtp_dtmf(self, digit: str) -> None:
        log.info("call %s: DTMF %s from PBX", self.call_id[:8], digit)
        if self.on_dtmf:
            self.on_dtmf(digit)

    @property
    def _can_send(self) -> bool:
        return (self.rtp is not None and self.codec is not None and self.state in (CallState.EARLY, CallState.CONNECTED)
                and not self.remote_hold and not self.local_hold and not self.dtmf_busy)

    def send_pcm(self, pcm16le: bytes) -> None:
        if self._can_send:
            self.rtp.send(self.codec.encode(pcm16le), len(pcm16le) // 2)

    def send_silence(self, samples: int) -> None:
        if self._can_send:
            self.rtp.send(self.codec.silence(samples), samples)

    async def _send_inband(self, digits: str) -> None:
        """Plays the keypad tones into the audio stream itself, so any far end hears them."""
        from ..audio.tones import dtmf_pcm

        if self.rtp is None or self.codec is None:
            return
        pcm = dtmf_pcm(digits, self.rate)
        step = self.rate // 50 * 2
        self.dtmf_busy = True
        try:
            for i in range(0, len(pcm), step):
                chunk = pcm[i:i + step]
                if len(chunk) < step:
                    chunk += b"\x00\x00" * ((step - len(chunk)) // 2)
                self.rtp.send(self.codec.encode(chunk), len(chunk) // 2)
                await asyncio.sleep(0.02)
        finally:
            self.dtmf_busy = False

    async def hold(self) -> bool:
        """Puts the far end on hold (re-INVITE sendonly); Asterisk plays music on hold to it."""
        if self.local_hold:
            return True
        ok = await self.account.reinvite(self, "sendonly")
        if ok:
            self.local_hold = True
        return ok

    async def unhold(self) -> bool:
        if not self.local_hold:
            return True
        ok = await self.account.reinvite(self, "sendrecv")
        if ok:
            self.local_hold = False
            if self.rtp:
                self.rtp.advance(0)
        return ok

    async def refer(self, target: str, timeout: float = 15.0) -> tuple[bool, str]:
        """Blind transfer via REFER. Returns (success, detail); on success the PBX ends our leg with BYE."""
        return await self.account.refer(self, target, timeout)

    async def send_dtmf(self, digits: str) -> None:
        """Sends digits to the PBX. Bridge audio is muted for the duration: RFC 4733 wants the
        media stream silent while an event is in flight, and inband tones must not be mixed with speech."""
        if self.state != CallState.CONNECTED:
            return
        mode = self.account.cfg.dtmf
        if mode == "none":
            return
        if mode == "rfc2833" and self.rtp and self.rtp.dtmf_pt is not None:
            self.dtmf_busy = True
            try:
                task = self.rtp.send_dtmf(digits)
                if task:
                    await task
            finally:
                self.dtmf_busy = False
            return
        if mode == "inband" or (mode == "rfc2833" and self.rtp and self.rtp.dtmf_pt is None):
            if mode == "rfc2833":
                log.info("call %s: the PBX did not offer telephone-event, sending inband tones", self.call_id[:8])
            await self._send_inband(digits)
            return
        for d in digits:
            req = self.account._dialog_request(self, "INFO")
            req.set("Content-Type", "application/dtmf-relay")
            req.body = f"Signal={d}\r\nDuration=160\r\n".encode()
            try:
                await self.account._send_request(req, dialog=self, timeout=5)
            except Exception as e:
                log.warning("INFO DTMF failed: %s", e)
            await asyncio.sleep(0.2)

    # ---- UAS API ----

    def ringing(self) -> None:
        if self.incoming and self.state in (CallState.NEW, CallState.RINGING) and self.invite_request:
            resp = self.account._response_for(self.invite_request, 180, dialog=self)
            self.account._send_response(resp, self.invite_request)
            self._set_state(CallState.RINGING)

    def answer(self) -> None:
        if not self.incoming or not self.invite_request or self.state in (CallState.CONNECTED, CallState.TERMINATED):
            return
        resp = self.account._response_for(self.invite_request, 200, dialog=self)
        assert self.local_sdp is not None
        resp.set("Content-Type", "application/sdp")
        resp.body = self.local_sdp.encode()
        self._send_final(resp)
        self._set_state(CallState.CONNECTED)

    def reject(self, code: int = 486, reason: str = "") -> None:
        if not self.incoming or not self.invite_request or self.state in (CallState.CONNECTED, CallState.TERMINATED):
            return
        resp = self.account._response_for(self.invite_request, code, reason, dialog=self)
        self._send_final(resp)
        self._terminate(code, reason or reason_text(code))

    def redirect(self, target: str) -> None:
        """302: the PBX dials `target` itself, so out-of-hours forwarding stays a PBX matter."""
        if not self.incoming or not self.invite_request or self.state in (CallState.CONNECTED, CallState.TERMINATED):
            return
        resp = self.account._response_for(self.invite_request, 302, dialog=self)
        uri = target if target.startswith("sip:") else f"sip:{target}@{self.account.cfg.domain}"
        resp.set("Contact", f"<{uri}>")
        self._send_final(resp)
        self._terminate(302, "moved temporarily")

    def _send_final(self, resp: SipMessage) -> None:
        assert self.invite_request is not None
        self.last_final = resp
        self.ack_received = False
        self.final_interval = T1
        self.final_deadline = time.time() + TIMER_B
        self.account._send_response(resp, self.invite_request)
        if self.account.transport.kind == "UDP":
            self._schedule_final_retransmit()

    def _schedule_final_retransmit(self) -> None:
        if self.final_timer:
            self.final_timer.cancel()
        self.final_timer = self.account.loop.call_later(self.final_interval, self._retransmit_final)

    def _retransmit_final(self) -> None:
        self.final_timer = None
        if self.ack_received or self.state == CallState.TERMINATED or not self.last_final:
            return
        if time.time() > self.final_deadline:
            log.warning("call %s: no ACK for final response", self.call_id[:8])
            if self.last_final.status == 200:
                self.account.loop.create_task(self.hangup())
            return
        self.account._send_response(self.last_final, self.invite_request)
        self.final_interval = min(self.final_interval * 2, T2)
        self._schedule_final_retransmit()

    # ---- common API ----

    async def hangup(self, code: int = 0, reason: str = "") -> None:
        """Ends the call from our side, whatever its state."""
        if self.state == CallState.TERMINATED:
            return
        if self.incoming and self.state in (CallState.NEW, CallState.RINGING):
            self.reject(code or 486, reason)
            return
        if not self.incoming and self.state in (CallState.CALLING, CallState.RINGING, CallState.EARLY):
            await self.account._cancel(self)
            return
        if self.state == CallState.CONNECTED:
            req = self.account._dialog_request(self, "BYE")
            self._terminate(0, reason or "local hangup")
            try:
                await self.account._send_request(req, timeout=8)
            except Exception as e:
                log.debug("BYE failed: %s", e)
            return
        self._terminate(0, reason or "local hangup")


class SipAccount:
    """Registration + calls for one SIP identity, bound to its own transport."""

    def __init__(self, cfg: SipConfig, local_ip: str):
        self.cfg = cfg
        self.loop = asyncio.get_event_loop()
        self.local_ip = local_ip
        self.advertised_ip = cfg.public_ip or local_ip
        self.server_addr = (cfg.server, cfg.port)
        self.transport: SipTransport
        if cfg.transport == "udp":
            self.transport = UdpTransport(local_ip if local_ip != "0.0.0.0" else "0.0.0.0", cfg.local_port, self._on_message)
        else:
            self.transport = TcpTransport(local_ip, cfg.local_port, self._on_message, self.server_addr,
                                          tls=(cfg.transport == "tls"), tls_verify=cfg.tls_verify)
            self.transport.on_disconnect = self._on_transport_lost
        self.ports = RtpPortPool(cfg.rtp_port_min, cfg.rtp_port_max)
        self.codecs: list[SdpCodec] = codec_list(cfg.codecs)
        self.allow_from = [ipaddress.ip_network(a, strict=False) for a in cfg.allow_from]
        self._refused: set[str] = set()
        self._calls: dict[str, SipCall] = {}
        self._client_txns: dict[str, ClientTxn] = {}
        self._server_txns: dict[tuple[str, str], tuple[SipMessage, float]] = {}
        self.registered = False
        self.register_expires = cfg.expires
        self._reg_call_id = uuid.uuid4().hex
        self._reg_cseq = random.randint(1, 1000)
        self._reg_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._stopping = False
        self.on_incoming_call: Callable[[SipCall], Awaitable[None] | None] | None = None
        self.on_registration: Callable[[bool, str], None] | None = None
        self.on_message_text: Callable[[str, str], None] | None = None

    # ---- lifecycle ----

    async def start(self) -> None:
        await self.resolve_server()
        await self.transport.start()
        if self.cfg.register:
            self._reg_task = self.loop.create_task(self._register_loop())
        if self.cfg.keepalive > 0:
            self._keepalive_task = self.loop.create_task(self._keepalive_loop())

    async def resolve_server(self) -> None:
        """Datagrams must be addressed by IP: Windows' proactor loop rejects a host name
        in sendto() with WSAEINVAL. Re-resolved on registration failure for dynamic DNS."""
        host = self.cfg.server
        try:
            infos = await self.loop.getaddrinfo(host, self.cfg.port, type=socket.SOCK_DGRAM)
        except OSError as e:
            raise CallError(503, f"cannot resolve {host}: {e}") from e
        ipv4 = [i for i in infos if i[0] == socket.AF_INET]
        addr = (ipv4 or infos)[0][4][:2]
        if addr != self.server_addr:
            log.info("SIP server %s resolved to %s:%d", host, addr[0], addr[1])
        self.server_addr = (addr[0], addr[1])
        if isinstance(self.transport, TcpTransport):
            self.transport.remote = self.server_addr

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._reg_task, self._keepalive_task):
            if task:
                task.cancel()
        for call in list(self._calls.values()):
            try:
                await call.hangup()
            except Exception:
                pass
        if self.registered:
            try:
                await self._register_once(expires=0)
            except Exception:
                pass
        await self.transport.stop()

    def _on_transport_lost(self) -> None:
        self.registered = False
        if self.on_registration:
            self.on_registration(False, "transport closed")
        if not self._stopping and self._reg_task:
            self._reg_task.cancel()
            self._reg_task = self.loop.create_task(self._register_loop(delay=2))

    @property
    def contact_uri(self) -> str:
        port = self.transport.local_port
        params = "" if self.cfg.transport == "udp" else f";transport={self.cfg.transport}"
        return f"<sip:{self.cfg.username}@{self.advertised_ip}:{port}{params}>"

    @property
    def aor(self) -> str:
        return f"sip:{self.cfg.username}@{self.cfg.domain}"

    @property
    def local_name_addr(self) -> str:
        display = f'"{self.cfg.display_name}" ' if self.cfg.display_name else ""
        return f"{display}<{self.aor}>"

    # ---- registration ----

    async def _register_loop(self, delay: float = 0) -> None:
        await asyncio.sleep(delay)
        while not self._stopping:
            try:
                expires = await self._register_once(self.cfg.expires)
                self.registered = True
                self.register_expires = expires
                if self.on_registration:
                    self.on_registration(True, f"registered for {expires}s")
                wait = max(10.0, min(expires * 0.5, expires - 15))
            except CallError as e:
                self.registered = False
                if self.on_registration:
                    self.on_registration(False, str(e))
                log.warning("registration failed: %s", e)
                wait = 30.0
                try:
                    await self.resolve_server()
                except CallError as re:
                    log.warning("%s", re)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.registered = False
                if self.on_registration:
                    self.on_registration(False, str(e))
                log.warning("registration error: %s", e)
                wait = 30.0
            await asyncio.sleep(wait)

    async def _register_once(self, expires: int) -> int:
        req = SipMessage.request("REGISTER", f"sip:{self.cfg.domain}")
        self._reg_cseq += 1
        req.set("From", f"{self.local_name_addr};tag={new_tag()}")
        req.set("To", f"<{self.aor}>")
        req.set("Call-ID", self._reg_call_id)
        req.set("CSeq", f"{self._reg_cseq} REGISTER")
        req.set("Contact", self.contact_uri)
        req.set("Expires", expires)
        self._common_headers(req)
        resp = await self._send_request(req, timeout=TIMER_B)
        if resp.status in (401, 407):
            resp = await self._authenticate_and_resend(req, resp)
        if resp.status == 423:
            min_exp = resp.get("Min-Expires")
            if min_exp and min_exp.isdigit():
                self.cfg.expires = int(min_exp)
                return await self._register_once(int(min_exp))
        if resp.status != 200:
            raise CallError(resp.status, resp.reason)
        got = expires
        for c in resp.get_all("Contact"):
            try:
                na = NameAddr.parse(c)
            except ValueError:
                continue
            if na.uri.user == self.cfg.username and "expires" in na.params and str(na.params["expires"]).isdigit():
                got = int(str(na.params["expires"]))
                break
        else:
            exp_h = resp.get("Expires")
            if exp_h and exp_h.isdigit():
                got = int(exp_h)
        if expires:
            log.info("registered %s at %s (expires %ss)", self.aor, self.cfg.server, got)
        else:
            log.info("unregistered %s", self.aor)
            self.registered = False
        return got if expires else 0

    async def _authenticate_and_resend(self, req: SipMessage, resp: SipMessage, dialog: SipCall | None = None) -> SipMessage:
        header = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
        challenge_raw = resp.get(header)
        if not challenge_raw:
            raise CallError(resp.status, "challenge missing")
        challenge = parse_challenge(challenge_raw)
        auth = build_authorization(challenge, self.cfg.auth_username, self.cfg.password, req.method or "", req.uri or "")
        req.set("Authorization" if resp.status == 401 else "Proxy-Authorization", auth)
        num, method = req.cseq
        req.set("CSeq", f"{num + 1} {method}")
        if dialog is not None:
            dialog.local_cseq = num + 1
        else:
            self._reg_cseq = num + 1
        self._set_via(req)
        return await self._send_request(req, dialog=dialog, timeout=TIMER_B,
                                        provisional=(dialog and dialog.incoming is False and self._invite_provisional(dialog)) or None)

    async def _keepalive_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.cfg.keepalive)
            try:
                await self.transport.send_keepalive(self.server_addr)
            except Exception as e:
                log.debug("keepalive failed: %s", e)

    # ---- message building ----

    def _common_headers(self, req: SipMessage) -> None:
        req.set("Max-Forwards", 70)
        req.set("User-Agent", self.cfg.user_agent)
        req.set("Allow", ALLOW)
        self._set_via(req)

    def _set_via(self, req: SipMessage) -> None:
        req.set("Via", f"SIP/2.0/{self.transport.kind} {self.advertised_ip}:{self.transport.local_port};rport;branch={new_branch()}")

    def _response_for(self, req: SipMessage, code: int, reason: str = "", dialog: SipCall | None = None) -> SipMessage:
        resp = SipMessage.response(code, reason or reason_text(code))
        for via in req.get_all("Via"):
            resp.add("Via", via)
        resp.set("From", req.get("From") or "")
        to = req.get("To") or ""
        if dialog is not None and "tag=" not in to and code != 100:
            to = f"{to};tag={dialog.local_tag}"
        resp.set("To", to)
        resp.set("Call-ID", req.call_id)
        resp.set("CSeq", req.get("CSeq") or "")
        for rr in req.get_all("Record-Route"):
            resp.add("Record-Route", rr)
        if code != 100:
            resp.set("Contact", self.contact_uri)
        resp.set("User-Agent", self.cfg.user_agent)
        resp.set("Allow", ALLOW)
        return resp

    def _dialog_request(self, call: SipCall, method: str) -> SipMessage:
        call.local_cseq += 1
        target = call.remote_target or call.remote_uri
        req = SipMessage.request(method, str(SipUri.parse(target)))
        req.set("From", call.local_uri)
        req.set("To", call.remote_uri)
        req.set("Call-ID", call.call_id)
        req.set("CSeq", f"{call.local_cseq} {method}")
        for r in call.route_set:
            req.add("Route", r)
        if method in ("INVITE", "UPDATE", "REFER"):
            req.set("Contact", self.contact_uri)
        self._common_headers(req)
        return req

    def _send_response(self, resp: SipMessage, req: SipMessage) -> None:
        addr = self._response_addr(req)
        branch = req.branch or ""
        num, method = req.cseq
        if resp.status >= 200 or method == "INVITE":
            self._server_txns[(branch, method)] = (resp, time.time())
        self._transmit(resp, addr)

    def _response_addr(self, req: SipMessage) -> tuple[str, int]:
        if self.cfg.transport != "udp":
            return self.server_addr
        via = req.top_via
        if via is None:
            return self.server_addr
        host = via.params.get("received") or via.host
        port_s = via.params.get("rport")
        port = int(port_s) if port_s and str(port_s).isdigit() else (via.port or 5060)
        return (host, port)

    def _transmit(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        data = msg.serialize()
        if siplog.isEnabledFor(logging.DEBUG):
            siplog.debug("-> %s:%d\n%s", addr[0], addr[1], data.decode("utf-8", "replace"))
        self.loop.create_task(self._safe_send(data, addr))

    async def _safe_send(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            await self.transport.send(data, addr)
        except Exception as e:
            log.warning("send to %s failed: %s", addr, e)

    # ---- client transactions ----

    async def _send_request(
        self,
        req: SipMessage,
        dialog: SipCall | None = None,
        timeout: float = TIMER_B,
        provisional: Callable[[SipMessage], None] | None = None,
    ) -> SipMessage:
        branch = req.branch or new_branch()
        fut: asyncio.Future = self.loop.create_future()
        txn = ClientTxn(request=req, addr=self.server_addr, future=fut, provisional=provisional, dialog=dialog)
        txn.deadline = time.time() + timeout
        self._client_txns[branch] = txn
        self._transmit(req, txn.addr)
        if self.transport.kind == "UDP":
            txn.timer = self.loop.call_later(T1, self._retransmit, branch)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise CallError(408, "no response") from None
        finally:
            txn = self._client_txns.pop(branch, None)
            if txn and txn.timer:
                txn.timer.cancel()

    def _retransmit(self, branch: str) -> None:
        txn = self._client_txns.get(branch)
        if not txn or txn.future.done():
            return
        if time.time() > txn.deadline:
            return
        self._transmit(txn.request, txn.addr)
        txn.interval = min(txn.interval * 2, T2)
        txn.timer = self.loop.call_later(txn.interval, self._retransmit, branch)

    def _handle_response(self, resp: SipMessage, addr: tuple[str, int]) -> None:
        branch = resp.branch or ""
        num, method = resp.cseq
        txn = self._client_txns.get(branch)
        if txn is None:
            if method == "INVITE" and resp.status is not None and 200 <= resp.status < 300:
                call = self._calls.get(resp.call_id)
                if call and call.last_ack:
                    self._transmit(call.last_ack, self.server_addr)
            return
        if resp.status is not None and resp.status < 200:
            if txn.timer:
                txn.timer.cancel()
                txn.timer = None
                if self.transport.kind == "UDP" and method == "INVITE":
                    pass
            if txn.provisional:
                txn.provisional(resp)
            return
        if method == "INVITE" and txn.dialog is not None and resp.status is not None and resp.status >= 300:
            self._send_ack(txn.dialog, txn.request, resp)
        if not txn.future.done():
            txn.future.set_result(resp)

    def _send_ack(self, call: SipCall, invite: SipMessage, resp: SipMessage) -> None:
        """ACK for a non-2xx response reuses the INVITE branch (transaction ACK)."""
        ack = SipMessage.request("ACK", invite.uri or "")
        ack.set("Via", invite.get("Via") or "")
        ack.set("From", invite.get("From") or "")
        ack.set("To", resp.get("To") or invite.get("To") or "")
        ack.set("Call-ID", invite.call_id)
        ack.set("CSeq", f"{invite.cseq[0]} ACK")
        for r in invite.get_all("Route"):
            ack.add("Route", r)
        ack.set("Max-Forwards", 70)
        self._transmit(ack, self.server_addr)

    def _send_dialog_ack(self, call: SipCall, resp: SipMessage) -> None:
        """ACK for 2xx: a new transaction inside the dialog."""
        target = call.remote_target or call.remote_uri
        ack = SipMessage.request("ACK", str(SipUri.parse(target)))
        ack.set("From", call.local_uri)
        ack.set("To", call.remote_uri)
        ack.set("Call-ID", call.call_id)
        ack.set("CSeq", f"{resp.cseq[0]} ACK")
        for r in call.route_set:
            ack.add("Route", r)
        ack.set("Max-Forwards", 70)
        self._set_via(ack)
        call.last_ack = ack
        self._transmit(ack, self.server_addr)

    # ---- incoming messages ----

    def _on_message(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        if siplog.isEnabledFor(logging.DEBUG):
            siplog.debug("<- %s:%d\n%s", addr[0], addr[1], msg)
        try:
            if msg.is_response:
                self._handle_response(msg, addr)
            else:
                self._handle_request(msg, addr)
        except Exception:
            log.exception("failed to handle SIP message")

    def _handle_request(self, req: SipMessage, addr: tuple[str, int]) -> None:
        branch = req.branch or ""
        num, method = req.cseq
        now = time.time()
        for key, (_, ts) in list(self._server_txns.items()):
            if now - ts > TIMER_B:
                del self._server_txns[key]
        if method == req.method and (branch, method) in self._server_txns and method != "ACK":
            cached, _ = self._server_txns[(branch, method)]
            self._transmit(cached, self._response_addr(req))
            return
        call = self._calls.get(req.call_id)
        method = req.method or ""
        if call is None and method in ("INVITE", "MESSAGE") and not self._trusted(addr[0]):
            if addr[0] not in self._refused and len(self._refused) < 1000:
                self._refused.add(addr[0])
                log.warning("%s from %s ignored: calls are taken only from the PBX at %s "
                            "(add the address to sip.allow_from if the PBX sends from it)", method, addr[0], self.server_addr[0])
            return
        if method == "INVITE":
            if call is None:
                self._handle_new_invite(req, addr)
            else:
                self._handle_reinvite(call, req)
        elif method == "ACK":
            if call is not None:
                self._handle_ack(call, req)
        elif method == "CANCEL":
            self._handle_cancel(call, req)
        elif method == "BYE":
            if call is None:
                self._send_response(self._response_for(req, 481), req)
                return
            self._send_response(self._response_for(req, 200, dialog=call), req)
            call._terminate(0, "remote hangup")
        elif method == "OPTIONS":
            resp = self._response_for(req, 200, dialog=call)
            resp.set("Accept", "application/sdp")
            self._send_response(resp, req)
        elif method == "INFO":
            self._handle_info(call, req)
        elif method == "UPDATE":
            resp = self._response_for(req, 200, dialog=call)
            if call is not None and req.body and call.local_sdp is not None:
                self._apply_offer(call, req)
                resp.set("Content-Type", "application/sdp")
                resp.body = call.local_sdp.encode()
            self._send_response(resp, req)
        elif method == "NOTIFY":
            self._send_response(self._response_for(req, 200, dialog=call), req)
            if call is not None and (req.get("Event") or "").lower().startswith("refer"):
                self._handle_refer_notify(call, req)
        elif method == "MESSAGE":
            self._send_response(self._response_for(req, 200), req)
            if self.on_message_text:
                try:
                    self.on_message_text(req.from_.uri.user or "", req.body.decode("utf-8", "replace"))
                except Exception:
                    log.exception("message handler failed")
        elif method in ("REFER", "SUBSCRIBE", "PRACK", "PUBLISH"):
            self._send_response(self._response_for(req, 501), req)
        else:
            self._send_response(self._response_for(req, 405), req)

    def _trusted(self, host: str) -> bool:
        """The gateway does not authenticate incoming requests, so new calls and messages come only from the PBX."""
        if host == self.server_addr[0]:
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(ip in net for net in self.allow_from)

    def _handle_new_invite(self, req: SipMessage, addr: tuple[str, int]) -> None:
        if not req.body:
            self._send_response(self._response_for(req, 488, "Offer required"), req)
            return
        offer = Sdp.parse(req.body)
        call = SipCall(self, req.call_id, incoming=True)
        call.invite_request = req
        call.invite_branch = req.branch or ""
        call.remote_tag = req.from_.tag
        call.remote_uri = req.get("From") or ""
        to = req.to
        to.params["tag"] = call.local_tag
        call.local_uri = str(to)
        call.remote_target = req.get("Contact") or req.get("From") or ""
        call.route_set = req.get_all("Record-Route")
        call.remote_cseq = req.cseq[0]
        frm = req.from_
        call.caller_number = frm.uri.user or ""
        call.caller_name = frm.display
        pai = req.get("P-Asserted-Identity") or req.get("Remote-Party-ID")
        if pai:
            try:
                na = NameAddr.parse(pai)
                if not call.caller_name:
                    call.caller_name = na.display
            except ValueError:
                pass
        try:
            call.dialed = SipUri.parse(req.uri or "").user or ""
        except ValueError:
            call.dialed = ""
        self._send_response(self._response_for(req, 100), req)
        try:
            sock = self.ports.bind(self.local_ip)
        except OSError as e:
            log.error("no RTP port: %s", e)
            self._send_response(self._response_for(req, 503, dialog=call), req)
            return
        call.rtp = RtpSession(sock, 8000)
        result = answer_for(offer, self.advertised_ip, call.rtp.local_port, self.codecs)
        if result is None:
            call.rtp.close()
            log.warning("no common codec with PBX offer: %s", [str(c) for c in offer.codecs])
            self._send_response(self._response_for(req, 488, dialog=call), req)
            return
        answer, chosen = result
        call.local_sdp = answer
        try:
            call._setup_rtp(chosen, offer)
        except CallError as e:
            call.rtp.close()
            self._send_response(self._response_for(req, e.code, dialog=call), req)
            return
        self._calls[call.call_id] = call
        self.loop.create_task(call.rtp.start())
        log.info("incoming call %s from %s <%s> to %s, codec %s",
                 call.call_id[:8], call.caller_name, call.caller_number, call.dialed, chosen)
        if self.on_incoming_call:
            hook = self.on_incoming_call(call)
            if asyncio.iscoroutine(hook):
                self.loop.create_task(hook)
        else:
            call.reject(480)

    def _apply_offer(self, call: SipCall, req: SipMessage) -> None:
        offer = Sdp.parse(req.body)
        call._apply_remote_sdp(offer)
        if call.local_sdp is not None:
            call.local_sdp.session_version += 1
            mirrored = {"sendonly": "recvonly", "recvonly": "sendonly", "inactive": "inactive"}
            call.local_sdp.direction = mirrored.get(offer.direction, "sendrecv")
            te_pt = offer.dtmf_pt_for(call.codec.rate if call.codec else None)
            if te_pt is not None and call.rtp:
                call.rtp.dtmf_pt = te_pt

    def _handle_reinvite(self, call: SipCall, req: SipMessage) -> None:
        if call.state != CallState.CONNECTED or call.local_sdp is None or call.reinvite_pending:
            self._send_response(self._response_for(req, 491, "Request Pending", dialog=call), req)
            return
        call.remote_cseq = req.cseq[0]
        call.invite_request = req
        call.invite_branch = req.branch or ""
        contact = req.get("Contact")
        if contact:
            call.remote_target = contact
        if req.body:
            self._apply_offer(call, req)
        resp = self._response_for(req, 200, dialog=call)
        resp.set("Content-Type", "application/sdp")
        resp.body = call.local_sdp.encode()
        call._send_final(resp)
        log.info("call %s: re-INVITE answered (hold=%s)", call.call_id[:8], call.remote_hold)

    def _handle_ack(self, call: SipCall, req: SipMessage) -> None:
        call.ack_received = True
        if call.final_timer:
            call.final_timer.cancel()
            call.final_timer = None
        if req.body and call.remote_sdp is None:
            call._apply_remote_sdp(Sdp.parse(req.body))
        if call.state != CallState.TERMINATED and call.last_final and call.last_final.status == 200:
            call._set_state(CallState.CONNECTED)

    def _handle_cancel(self, call: SipCall | None, req: SipMessage) -> None:
        if call is None or not call.incoming or call.state not in (CallState.NEW, CallState.RINGING):
            self._send_response(self._response_for(req, 481), req)
            return
        self._send_response(self._response_for(req, 200, dialog=call), req)
        if call.invite_request:
            resp = self._response_for(call.invite_request, 487, dialog=call)
            self._send_response(resp, call.invite_request)
        call.cancelled = True
        call._terminate(487, "cancelled by caller")

    def _handle_refer_notify(self, call: SipCall, req: SipMessage) -> None:
        """NOTIFY with a message/sipfrag body reports how the transferred call is doing."""
        first = req.body.decode("utf-8", "replace").strip().split("\r\n")[0].split("\n")[0]
        m = re.match(r"SIP/2\.0\s+(\d{3})", first)
        if not m:
            return
        status = int(m.group(1))
        log.info("call %s: transfer progress %s", self.call_id_short(call), first.strip())
        fut = call.refer_result
        if fut is not None and not fut.done() and status >= 200:
            fut.set_result(status)

    @staticmethod
    def call_id_short(call: SipCall) -> str:
        return call.call_id[:8]

    async def reinvite(self, call: SipCall, direction: str) -> bool:
        """Sends a re-INVITE that only changes the media direction (hold / resume)."""
        if call.state != CallState.CONNECTED or call.local_sdp is None or call.reinvite_pending:
            return False
        call.reinvite_pending = True
        try:
            for attempt in range(2):
                call.local_sdp.direction = direction
                call.local_sdp.session_version += 1
                req = self._dialog_request(call, "INVITE")
                req.set("Content-Type", "application/sdp")
                req.body = call.local_sdp.encode()
                try:
                    resp = await self._send_request(req, dialog=call, timeout=TIMER_B)
                    if resp.status in (401, 407):
                        resp = await self._authenticate_and_resend(req, resp, dialog=call)
                except CallError as e:
                    log.warning("call %s: re-INVITE failed: %s", call.call_id[:8], e)
                    return False
                if resp.status == 491 and attempt == 0:
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue
                if resp.status is None or resp.status >= 300:
                    log.warning("call %s: re-INVITE rejected: %s %s", call.call_id[:8], resp.status, resp.reason)
                    return False
                if resp.body:
                    answer = Sdp.parse(resp.body)
                    call.remote_sdp = answer
                    if call.rtp and answer.port:
                        call.rtp.set_remote(answer.conn_ip, answer.port)
                self._send_dialog_ack(call, resp)
                log.info("call %s: media direction now %s", call.call_id[:8], direction)
                return True
            return False
        finally:
            call.reinvite_pending = False

    async def refer(self, call: SipCall, target: str, timeout: float = 15.0) -> tuple[bool, str]:
        if call.state != CallState.CONNECTED:
            return False, "no active call"
        if target.startswith("sip:") or target.startswith("sips:"):
            uri = target
        elif "@" in target:
            uri = f"sip:{target}"
        else:
            uri = f"sip:{target}@{self.cfg.domain}"
        req = self._dialog_request(call, "REFER")
        req.set("Refer-To", f"<{uri}>")
        req.set("Referred-By", f"<{self.aor}>")
        call.refer_result = self.loop.create_future()
        try:
            resp = await self._send_request(req, dialog=call, timeout=10)
            if resp.status in (401, 407):
                resp = await self._authenticate_and_resend(req, resp, dialog=call)
        except CallError as e:
            call.refer_result = None
            return False, str(e)
        if resp.status is None or resp.status >= 300:
            call.refer_result = None
            return False, f"{resp.status} {resp.reason}"
        try:
            done, _ = await asyncio.wait([call.refer_result, call.ended], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            fut = call.refer_result
            call.refer_result = None
        if fut.done() and not fut.cancelled():
            status = fut.result()
            if status < 300:
                if call.active:
                    await call.hangup(reason="transferred")
                return True, f"{status}"
            return False, f"{status}"
        if call.ended.done():
            return True, "call taken over by the PBX"
        return False, "no confirmation from the PBX"

    def _handle_info(self, call: SipCall | None, req: SipMessage) -> None:
        self._send_response(self._response_for(req, 200, dialog=call), req)
        ctype = (req.get("Content-Type") or "").lower()
        if call is None or "dtmf" not in ctype:
            return
        body = req.body.decode("utf-8", "replace")
        digit = ""
        if "dtmf-relay" in ctype:
            for line in body.splitlines():
                if line.lower().startswith("signal="):
                    digit = line.split("=", 1)[1].strip()
        else:
            digit = body.strip()[:1]
        if digit:
            call._rtp_dtmf(digit)

    # ---- outgoing calls ----

    def _invite_provisional(self, call: SipCall) -> Callable[[SipMessage], None]:
        def handler(resp: SipMessage) -> None:
            if call.state == CallState.TERMINATED:
                return
            if resp.to.tag and not call.remote_tag:
                call.remote_tag = resp.to.tag
                call.remote_uri = resp.get("To") or call.remote_uri
            contact = resp.get("Contact")
            if contact:
                call.remote_target = contact
            if resp.body and call.local_sdp is not None:
                answer = Sdp.parse(resp.body)
                chosen = match_answer(call.local_sdp, answer)
                if chosen and answer.port:
                    if call.codec is None:
                        call._setup_rtp(chosen, answer)
                    else:
                        call._apply_remote_sdp(answer)
                    call._set_state(CallState.EARLY)
                    return
            if resp.status in (180, 183) and call.state == CallState.CALLING:
                call._set_state(CallState.RINGING)
        return handler

    async def invite(self, destination: str, headers: dict[str, str] | None = None, display_name: str | None = None) -> SipCall:
        """Sends an INVITE; returns the call once a response other than 100 is being processed.
        Await call.answered for the 200 OK (raises CallError on failure)."""
        if destination.startswith("sip:") or destination.startswith("sips:"):
            uri = destination
        elif "@" in destination:
            uri = f"sip:{destination}"
        else:
            uri = f"sip:{destination}@{self.cfg.domain}"
        call = SipCall(self, uuid.uuid4().hex, incoming=False)
        display = self.cfg.display_name if display_name is None else display_name
        call.local_uri = (f'"{display}" ' if display else "") + f"<{self.aor}>;tag={call.local_tag}"
        call.remote_uri = f"<{uri}>"
        call.remote_target = uri
        call.headers = dict(headers or {})
        sock = self.ports.bind(self.local_ip)
        call.rtp = RtpSession(sock, 8000)
        await call.rtp.start()
        call.local_sdp = local_offer(self.advertised_ip, call.rtp.local_port, self.codecs)
        self._calls[call.call_id] = call
        self.loop.create_task(self._invite_flow(call, uri))
        return call

    async def _invite_flow(self, call: SipCall, uri: str) -> None:
        req = SipMessage.request("INVITE", uri)
        req.set("From", call.local_uri)
        req.set("To", call.remote_uri)
        req.set("Call-ID", call.call_id)
        req.set("CSeq", f"{call.local_cseq} INVITE")
        req.set("Contact", self.contact_uri)
        for k, v in call.headers.items():
            req.set(k, v)
        self._common_headers(req)
        req.set("Content-Type", "application/sdp")
        assert call.local_sdp is not None
        req.body = call.local_sdp.encode()
        call.invite_request = req
        call._set_state(CallState.CALLING)
        try:
            resp = await self._send_request(req, dialog=call, timeout=TIMER_B, provisional=self._invite_provisional(call))
            if resp.status in (401, 407) and not call.auth_retry:
                call.auth_retry = True
                call.remote_tag = None
                resp = await self._authenticate_and_resend(req, resp, dialog=call)
        except CallError as e:
            call._terminate(e.code, e.text)
            return
        except Exception as e:
            call._terminate(500, str(e))
            return
        if call.state == CallState.TERMINATED:
            if resp.status is not None and 200 <= resp.status < 300:
                self._finish_2xx(call, resp)
                self.loop.create_task(self._bye_after_cancel(call))
            return
        if resp.status is None or resp.status >= 300:
            call._terminate(resp.status or 500, resp.reason)
            return
        self._finish_2xx(call, resp)
        if call.codec is None:
            await asyncio.sleep(0)
            call._terminate(488, "no SDP in 200 OK")
            return
        call._set_state(CallState.CONNECTED)
        if not call.answered.done():
            call.answered.set_result(True)

    def _finish_2xx(self, call: SipCall, resp: SipMessage) -> None:
        call.remote_tag = resp.to.tag
        call.remote_uri = resp.get("To") or call.remote_uri
        contact = resp.get("Contact")
        if contact:
            call.remote_target = contact
        call.route_set = list(reversed(resp.get_all("Record-Route")))
        if resp.body and call.local_sdp is not None:
            answer = Sdp.parse(resp.body)
            chosen = match_answer(call.local_sdp, answer)
            if chosen:
                if call.codec is None:
                    try:
                        call._setup_rtp(chosen, answer)
                    except CallError:
                        pass
                else:
                    call._apply_remote_sdp(answer)
        self._send_dialog_ack(call, resp)

    async def _bye_after_cancel(self, call: SipCall) -> None:
        req = self._dialog_request(call, "BYE")
        try:
            await self._send_request(req, timeout=8)
        except Exception:
            pass

    async def _cancel(self, call: SipCall) -> None:
        if call.state == CallState.TERMINATED or call.invite_request is None:
            return
        call.cancelled = True
        invite = call.invite_request
        cancel = SipMessage.request("CANCEL", invite.uri or "")
        cancel.set("Via", invite.get("Via") or "")
        cancel.set("From", invite.get("From") or "")
        cancel.set("To", invite.get("To") or "")
        cancel.set("Call-ID", invite.call_id)
        cancel.set("CSeq", f"{invite.cseq[0]} CANCEL")
        for r in invite.get_all("Route"):
            cancel.add("Route", r)
        cancel.set("Max-Forwards", 70)
        call._terminate(487, "cancelled")
        try:
            await self._send_request(cancel, timeout=8)
        except CallError as e:
            log.debug("CANCEL got %s", e)
