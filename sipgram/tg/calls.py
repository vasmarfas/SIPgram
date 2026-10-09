"""Telegram private (P2P) calls and conference calls on top of NTgCalls + Telethon raw API.

Flow (outgoing):  create_p2p_call -> init_exchange -> phone.requestCall -> phoneCallAccepted(g_b)
                  -> exchange_keys -> phone.confirmCall -> connect_p2p -> CONNECTED
Flow (incoming):  phoneCallRequested(g_a_hash) -> phone.receivedCall -> [accept] create_p2p_call
                  -> init_exchange(g_a_hash) -> phone.acceptCall(g_b) -> phoneCall(g_a, fingerprint)
                  -> exchange_keys -> connect_p2p -> CONNECTED
Conference:       the peer adds people to the call and discards it with phoneCallDiscardReasonMigrateConferenceCall,
                  or invites the gateway with a messageActionConferenceCall. Last chain block -> init_conference
                  (keeps the media of the private call) -> phone.joinGroupCall(public_key, block) -> connect.
                  From then on NTgCalls asks for chain blocks and for the owners of unknown SSRCs.
Audio is exchanged as 10 ms PCM16 frames at `sample_rate` (NTgCalls resamples to Opus internally).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from collections.abc import Awaitable, Callable

import ntgcalls
from telethon import utils
from telethon.errors import RPCError
from telethon.tl import functions, types

from .account import TgAccount
from .group import GROUP_RATE, TgGroupCall, input_group_call
from .media import FrameSender, external_audio, merge_frames

log = logging.getLogger("sipgram.tg.calls")

CONNECT_TIMEOUT = 20.0
CONFERENCE_KEY_BASE = -(1 << 60)


class TgCallError(Exception):
    def __init__(self, reason: str, sip_code: int = 480):
        super().__init__(reason)
        self.reason = reason
        self.sip_code = sip_code


class TgCallState(enum.Enum):
    NEW = "new"
    REQUESTING = "requesting"
    RINGING = "ringing"
    INCOMING = "incoming"
    ACCEPTING = "accepting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ENDED = "ended"


def _map_rpc_error(e: RPCError) -> TgCallError:
    msg = str(e)
    name = getattr(e, "message", "") or msg
    if "PRIVACY" in name:
        return TgCallError("user's privacy settings do not allow calls from the gateway account", 403)
    if "BLOCKED" in name:
        return TgCallError("the user has blocked the gateway account", 403)
    if "FLOOD" in name:
        return TgCallError(f"telegram flood wait: {msg}", 503)
    if "OUTDATED" in name or "LAYER" in name:
        return TgCallError(f"telegram client/protocol mismatch: {msg}", 480)
    return TgCallError(f"telegram error: {msg}", 480)


def _discard_reason(reason: str) -> types.TypePhoneCallDiscardReason:
    return {
        "busy": types.PhoneCallDiscardReasonBusy(),
        "missed": types.PhoneCallDiscardReasonMissed(),
        "no answer": types.PhoneCallDiscardReasonMissed(),
        "disconnect": types.PhoneCallDiscardReasonDisconnect(),
    }.get(reason, types.PhoneCallDiscardReasonHangup())


class Conference:
    """One E2E conference the gateway sits in, under its NTgCalls key."""

    def __init__(self, key: int, ref, loop: asyncio.AbstractEventLoop):
        self.key = key
        self.ref = ref                                    # slug or invite message until the call id is known
        self.call: types.InputGroupCall | None = None
        self.joined = False
        self.sources: dict[int, int] = {}                 # participant id -> audio SSRC
        self.seen_others = False
        self.connected: asyncio.Future = loop.create_future()

    @property
    def input(self):
        return self.call or self.ref


class TgCall:
    def __init__(self, engine: TgCallEngine, user_id: int, outgoing: bool, sample_rate: int):
        self.engine = engine
        self.user_id = user_id
        self.outgoing = outgoing
        self.sample_rate = sample_rate
        self.state = TgCallState.NEW
        self.call_id: int | None = None
        self.access_hash: int | None = None
        self.g_a_hash: bytes | None = None
        self.invite_msg_id: int | None = None
        self.conference: Conference | None = None
        self.emojis = ""
        self.video = False
        self.created = time.time()
        self.connected_at = 0.0
        self.end_reason = ""
        self.ended: asyncio.Future = engine.loop.create_future()
        self.connected: asyncio.Future = engine.loop.create_future()
        self._accepted: asyncio.Future = engine.loop.create_future()
        self._confirmed: asyncio.Future = engine.loop.create_future()
        self._media_ready = False
        self._migrating = False
        self._sig_in: list[bytes] = []
        self._sig_out: asyncio.Queue = asyncio.Queue()
        self._sig_task: asyncio.Task | None = None
        self.sender = FrameSender(engine.ntg, user_id)
        self.on_state: Callable[[TgCall, TgCallState], None] | None = None
        self.on_audio: Callable[[bytes], None] | None = None
        self.on_update: Callable[[TgCall], None] | None = None
        self.frames_in = 0
        self.frames_out = 0
        self.library_version = ""

    @property
    def active(self) -> bool:
        return self.state != TgCallState.ENDED

    @property
    def peer(self) -> types.InputPhoneCall:
        assert self.call_id is not None and self.access_hash is not None
        return types.InputPhoneCall(id=self.call_id, access_hash=self.access_hash)

    @property
    def others(self) -> int:
        """Telegram participants besides the gateway: 1 in a private call."""
        if self.conference is None:
            return 1
        return sum(1 for uid in self.conference.sources if uid != self.engine.my_id)

    def _set_state(self, state: TgCallState) -> None:
        if self.state == state or self.state == TgCallState.ENDED:
            return
        self.state = state
        if state == TgCallState.CONNECTED:
            self.connected_at = time.time()
        log.info("tg call with %s: %s", self.user_id, state.value)
        if self.on_state:
            try:
                self.on_state(self, state)
            except Exception:
                log.exception("tg on_state failed")

    def _updated(self) -> None:
        if self.on_update and not self._migrating:
            try:
                self.on_update(self)
            except Exception:
                log.exception("tg on_update failed")

    def frame_bytes(self) -> int:
        return self.sample_rate * 2 // 100

    def send_audio(self, pcm10ms: bytes) -> None:
        """Push one 10 ms PCM16 frame toward Telegram (call from the event loop thread)."""
        if not self._media_ready or self.state == TgCallState.ENDED:
            return
        self.sender.push(pcm10ms)
        self.frames_out += 1

    async def set_sample_rate(self, rate: int) -> None:
        if rate == self.sample_rate:
            return
        self.sample_rate = rate
        if self._media_ready or self.state in (TgCallState.CONNECTING, TgCallState.CONNECTED):
            await self.engine._configure_media(self)

    async def hangup(self, reason: str = "hangup") -> None:
        await self.engine._end_call(self, reason, local=True)

    async def accept(self) -> None:
        await self.engine._accept(self)


class TgCallEngine:
    def __init__(self, account: TgAccount, default_rate: int = 8000):
        self.account = account
        self.loop = asyncio.get_event_loop()
        self.default_rate = default_rate
        self.ntg = ntgcalls.NTgCalls()
        self.calls: dict[int, TgCall] = {}
        self.groups: dict[int, TgGroupCall] = {}
        self.conferences: dict[int, Conference] = {}
        self.on_incoming: Callable[[TgCall], Awaitable[None] | None] | None = None
        self._protocol = ntgcalls.NTgCalls.get_protocol()
        self._created_conferences = 0
        self.ntg.on_connection_change(self._on_connection_change)
        self.ntg.on_frames(self._on_frames)
        self.ntg.on_signaling_data(self._on_signaling)
        self.ntg.on_update_emojis(self._on_emojis)
        self.ntg.on_outbound_block(self._on_outbound_block)
        self.ntg.on_subchain_request(self._on_subchain_request)
        self.ntg.on_request_participants(self._on_request_participants)
        account.add_raw_handler(self._on_raw_update)

    @property
    def library_versions(self) -> list[str]:
        return list(self._protocol.library_versions)

    @property
    def my_id(self) -> int:
        return self.account.me.id if self.account.me else 0

    def tl_protocol(self) -> types.PhoneCallProtocol:
        return types.PhoneCallProtocol(
            min_layer=self._protocol.min_layer, max_layer=self._protocol.max_layer,
            udp_p2p=self._protocol.udp_p2p, udp_reflector=self._protocol.udp_reflector,
            library_versions=self.library_versions,
        )

    def call_for(self, user_id: int) -> TgCall | None:
        return self.calls.get(user_id)

    def _by_call_id(self, call_id: int) -> TgCall | None:
        for c in self.calls.values():
            if c.call_id == call_id:
                return c
        return None

    # ---- media ----

    async def _configure_media(self, call: TgCall) -> None:
        media = external_audio(call.sample_rate)
        await self.ntg.set_stream_sources(call.user_id, ntgcalls.StreamMode.CAPTURE, media)
        await self.ntg.set_stream_sources(call.user_id, ntgcalls.StreamMode.PLAYBACK, media)
        call._media_ready = True

    # ---- group calls (voice chats and conferences started by the gateway) ----

    async def join_group(self, chat: str, title: str = "") -> TgGroupCall:
        """Joins (or starts) the voice chat of `chat` and returns once media is connected."""
        call, peer, name = await input_group_call(self.account, chat, title=title)
        key = utils.get_peer_id(peer)
        existing = self.groups.get(key)
        if existing is not None and existing.active:
            return existing
        group = TgGroupCall(self, key, call, peer, name)
        self.groups[key] = group
        try:
            payload = await self.ntg.create_call(key)
            media = external_audio(GROUP_RATE)
            await self.ntg.set_stream_sources(key, ntgcalls.StreamMode.CAPTURE, media)
            await self.ntg.set_stream_sources(key, ntgcalls.StreamMode.PLAYBACK, media)
            try:
                result = await self.account.invoke(functions.phone.JoinGroupCallRequest(
                    call=call, params=types.DataJSON(data=payload), muted=False,
                    video_stopped=True, join_as=types.InputPeerSelf(),
                ))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            params = self._join_params(result)
            if params is None:
                raise TgCallError("telegram did not return the voice chat connection parameters", 480)
            await self._connect_group(group, params)
            log.info("joined the voice chat of %s (%d)", name, key)
            return group
        except TgCallError:
            await self.leave_group(group, "failed")
            raise
        except Exception as e:
            log.exception("joining the voice chat failed")
            await self.leave_group(group, "failed")
            raise TgCallError(f"voice chat: {e}", 480) from e

    async def create_conference(self, title: str = "") -> TgGroupCall:
        """Starts an E2E conference call: a Telegram group call that needs no group."""
        self._created_conferences += 1
        key = CONFERENCE_KEY_BASE - self._created_conferences
        group = TgGroupCall(self, key, None, types.InputPeerSelf(), title or "Telegram", conference=True)
        self.groups[key] = group
        conf = self.conferences[key] = Conference(key, None, self.loop)
        try:
            await self.ntg.create_p2p_call(key)
            params = await self.ntg.init_conference(key, self.my_id, None)
            media = external_audio(GROUP_RATE)
            await self.ntg.set_stream_sources(key, ntgcalls.StreamMode.CAPTURE, media)
            await self.ntg.set_stream_sources(key, ntgcalls.StreamMode.PLAYBACK, media)
            try:
                result = await self.account.invoke(functions.phone.CreateConferenceCallRequest(
                    random_id=random.randint(1, 0x7FFFFFFE), muted=False, video_stopped=True, join=True,
                    public_key=int.from_bytes(params.public_key, "little", signed=True), block=params.block,
                    params=types.DataJSON(data=params.payload),
                ))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            data, blocks = self._absorb(conf, result)
            if data is None or conf.call is None:
                raise TgCallError("telegram did not return the conference connection parameters", 480)
            group.call = conf.call
            conf.joined = True
            await self._connect_group(group, data)
            for update in blocks:
                await self._apply_blocks(conf, update, from_short_poll=False)
            log.info("started conference call %d", conf.call.id)
            return group
        except TgCallError:
            await self.leave_group(group, "failed")
            raise
        except Exception as e:
            log.exception("starting the conference call failed")
            await self.leave_group(group, "failed")
            raise TgCallError(f"conference call: {e}", 480) from e

    async def _connect_group(self, group: TgGroupCall, params: str) -> None:
        await self.ntg.connect(group.key, params, False)
        group.joined = True
        try:
            await asyncio.wait_for(asyncio.shield(group.connected), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise TgCallError("group call media did not connect", 480) from None

    @staticmethod
    def _join_params(result) -> str | None:
        for update in getattr(result, "updates", []):
            if isinstance(update, types.UpdateGroupCallConnection):
                return update.params.data
        return None

    async def leave_group(self, group: TgGroupCall, reason: str = "hangup") -> None:
        if not group.active:
            return
        if group.joined and group.call is not None:
            try:
                await self.account.invoke(functions.phone.LeaveGroupCallRequest(call=group.call, source=0))
            except RPCError as e:
                log.debug("leaveGroupCall failed: %s", e)
        group.joined = False
        self.conferences.pop(group.key, None)
        try:
            await self.ntg.stop(group.key)
        except Exception:
            pass
        if not group.connected.done():
            group.connected.set_exception(TgCallError(reason))
            group.connected.exception()
        if not group.ended.done():
            group.ended.set_result(reason)
        if self.groups.get(group.key) is group:
            self.groups.pop(group.key, None)
        log.info("left the group call %s (%s)", group.title, reason)

    # ---- conferences ----

    async def _migrate(self, call: TgCall, slug: str) -> None:
        """The peer turned the private call into a conference to add people: follow it there."""
        log.info("tg call with %s moves into conference %s", call.user_id, slug)
        if call._sig_task:
            call._sig_out.put_nowait(None)
            call._sig_task = None
        try:
            await self._join_conference(call, types.InputGroupCallSlug(slug=slug))
            await asyncio.wait_for(asyncio.shield(call.conference.connected), CONNECT_TIMEOUT)
        except Exception as e:
            log.warning("tg call with %s could not follow it into the conference: %s", call.user_id, e)
            joined = call.conference is not None and call.conference.joined
            await self._end_call(call, "conference failed", local=joined)
            return
        finally:
            call._migrating = False
        log.info("tg call with %s continues as a conference", call.user_id)
        call._updated()

    async def _join_conference(self, call: TgCall, ref) -> None:
        """Joins the conference `ref` points at, taking over the media of `call`."""
        key = call.user_id
        conf = call.conference = self.conferences[key] = Conference(key, ref, self.loop)
        last_block = await self._last_block(ref)
        if last_block is None:
            raise TgCallError("the conference has no chain block to join from", 480)
        try:
            params = await self.ntg.init_conference(key, self.my_id, last_block)
        except ntgcalls.ConnectionNotFound:
            await self.ntg.create_p2p_call(key)
            params = await self.ntg.init_conference(key, self.my_id, last_block)
        await self._configure_media(call)
        try:
            result = await self.account.invoke(functions.phone.JoinGroupCallRequest(
                call=ref, join_as=types.InputPeerSelf(), params=types.DataJSON(data=params.payload),
                muted=False, video_stopped=True,
                public_key=int.from_bytes(params.public_key, "little", signed=True), block=params.block,
            ))
        except RPCError as e:
            raise _map_rpc_error(e) from e
        conf.joined = True
        data, blocks = self._absorb(conf, result)
        if data is None:
            raise TgCallError("telegram did not return the conference connection parameters", 480)
        await self.ntg.connect(key, data, False)
        for update in blocks:
            await self._apply_blocks(conf, update, from_short_poll=False)
        await self._refresh_participants(conf)

    @staticmethod
    def _absorb(conf: Conference, result) -> tuple[str | None, list]:
        """Picks the call id, the connection parameters and the first chain blocks out of a join result."""
        data = None
        blocks = []
        for update in getattr(result, "updates", []):
            if isinstance(update, types.UpdateGroupCall) and isinstance(update.call, types.GroupCall):
                conf.call = types.InputGroupCall(id=update.call.id, access_hash=update.call.access_hash)
            elif isinstance(update, types.UpdateGroupCallConnection) and not update.presentation:
                data = update.params.data
            elif isinstance(update, types.UpdateGroupCallChainBlocks):
                blocks.append(update)
        return data, blocks

    async def _last_block(self, ref) -> bytes | None:
        try:
            result = await self.account.invoke(functions.phone.GetGroupCallChainBlocksRequest(
                call=ref, sub_chain_id=0, offset=-1, limit=1,
            ))
        except RPCError as e:
            raise _map_rpc_error(e) from e
        for update in getattr(result, "updates", []):
            if isinstance(update, types.UpdateGroupCallChainBlocks) and update.blocks:
                return update.blocks[-1]
        return None

    async def _apply_blocks(self, conf: Conference, update: types.UpdateGroupCallChainBlocks, from_short_poll: bool) -> None:
        try:
            await self.ntg.apply_blocks(conf.key, update.sub_chain_id, update.next_offset, list(update.blocks), from_short_poll)
        except Exception as e:
            log.debug("conference %d: applying chain blocks failed: %s", conf.key, e)

    async def _fetch_subchain(self, key: int, subchain: int, height: int, limit: int) -> None:
        conf = self.conferences.get(key)
        if conf is None:
            return
        try:
            if conf.input is not None:
                result = await self.account.invoke(functions.phone.GetGroupCallChainBlocksRequest(
                    call=conf.input, sub_chain_id=subchain, offset=height, limit=limit,
                ))
                for update in getattr(result, "updates", []):
                    if isinstance(update, types.UpdateGroupCallChainBlocks):
                        await self._apply_blocks(conf, update, from_short_poll=True)
        except RPCError as e:
            log.debug("conference %d: chain blocks request failed: %s", key, e)
        finally:
            try:
                await self.ntg.finish_subchain_request(key, subchain)
            except Exception as e:
                log.debug("conference %d: finishing the chain request failed: %s", key, e)

    async def _broadcast_block(self, key: int, block: bytes) -> None:
        conf = self.conferences.get(key)
        if conf is None or conf.input is None:
            return
        try:
            await self.account.invoke(functions.phone.SendConferenceCallBroadcastRequest(call=conf.input, block=block))
        except RPCError as e:
            log.debug("conference %d: broadcasting a chain block failed: %s", key, e)

    async def _refresh_participants(self, conf: Conference) -> None:
        """Reads who is in the conference: NTgCalls needs the owner of each SSRC to decrypt it."""
        if conf.input is None:
            return
        sources: dict[int, int] = {}
        offset = ""
        try:
            while True:
                result = await self.account.invoke(functions.phone.GetGroupParticipantsRequest(
                    call=conf.input, ids=[], sources=[], offset=offset, limit=100,
                ))
                for p in result.participants:
                    if not p.left:
                        sources[utils.get_peer_id(p.peer)] = p.source
                offset = result.next_offset
                if not offset or not result.participants:
                    break
        except RPCError as e:
            log.debug("conference %d: reading participants failed: %s", conf.key, e)
            return
        conf.sources = sources
        await self._participants_changed(conf)

    async def _participants_changed(self, conf: Conference) -> None:
        mappings = [ntgcalls.SsrcMapping(uid, ssrc) for uid, ssrc in conf.sources.items()]
        try:
            await self.ntg.update_audio_ssrc_mappings(conf.key, mappings)
        except Exception as e:
            log.debug("conference %d: SSRC mappings not taken: %s", conf.key, e)
        call = self.calls.get(conf.key)
        if call is None or call.conference is not conf or not call.active:
            return
        if call.others:
            conf.seen_others = True
        elif conf.seen_others and not call._migrating:
            log.info("tg call with %s: everyone left the conference", call.user_id)
            await self._end_call(call, "hangup", local=True)
            return
        call._updated()

    def _conference_by_call(self, input_call) -> Conference | None:
        call_id = getattr(input_call, "id", None)
        if call_id is None:
            return None
        return next((c for c in self.conferences.values() if c.call is not None and c.call.id == call_id), None)

    async def _conference_invite(self, msg: types.MessageService) -> None:
        action = msg.action
        if msg.out or action.missed or action.active or action.duration:
            return
        uid = utils.get_peer_id(msg.peer_id)
        existing = self.calls.get(uid)
        if existing and existing.active:
            log.info("conference invite from %s while a call with them is active; declining", uid)
            try:
                await self.account.invoke(functions.phone.DeclineConferenceCallInviteRequest(msg_id=msg.id))
            except RPCError:
                pass
            return
        call = TgCall(self, uid, outgoing=False, sample_rate=self.default_rate)
        call.invite_msg_id = msg.id
        self.calls[uid] = call
        call._set_state(TgCallState.INCOMING)
        await self._offer_incoming(call)

    # ---- P2P plumbing ----

    async def _dh_config(self) -> ntgcalls.DhConfig:
        dh = await self.account.invoke(functions.messages.GetDhConfigRequest(version=0, random_length=256))
        if not isinstance(dh, types.messages.DhConfig):
            raise TgCallError("unexpected DH config response", 500)
        return ntgcalls.DhConfig(dh.g, dh.p, dh.random)

    @staticmethod
    def _servers(connections) -> list[ntgcalls.RTCServer]:
        out = []
        for c in connections:
            if isinstance(c, types.PhoneConnectionWebrtc):
                out.append(ntgcalls.RTCServer(c.id, c.ip, c.ipv6, c.port, c.username, c.password,
                                              bool(c.turn), bool(c.stun), False, None))
            elif isinstance(c, types.PhoneConnection):
                out.append(ntgcalls.RTCServer(c.id, c.ip, c.ipv6, c.port, None, None, True, False, bool(c.tcp), c.peer_tag))
        return out

    async def _connect_media(self, call: TgCall, pc: types.PhoneCall) -> None:
        versions = list(pc.protocol.library_versions)
        call.library_version = max(versions, key=lambda v: [int(x) for x in v.split(".")]) if versions else "?"
        call._set_state(TgCallState.CONNECTING)
        custom = pc.custom_parameters.data if pc.custom_parameters else None
        await self.ntg.connect_p2p(call.user_id, self._servers(pc.connections), versions, bool(pc.p2p_allowed), custom)
        call._sig_task = self.loop.create_task(self._signaling_pump(call))
        for data in call._sig_in:
            try:
                await self.ntg.send_signaling_data(call.user_id, data)
            except Exception as e:
                log.debug("replaying signaling failed: %s", e)
        call._sig_in.clear()
        try:
            await asyncio.wait_for(asyncio.shield(call.connected), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise TgCallError("telegram media did not connect (network/relay problem)", 480) from None

    async def _signaling_pump(self, call: TgCall) -> None:
        while call.active:
            data = await call._sig_out.get()
            if data is None:
                return
            if call.call_id is None:
                continue
            try:
                await self.account.invoke(functions.phone.SendSignalingDataRequest(peer=call.peer, data=data))
            except RPCError as e:
                log.debug("sendSignalingData failed: %s", e)
            except Exception as e:
                log.debug("sendSignalingData error: %s", e)

    # ---- outgoing ----

    async def call(self, input_user: types.InputUser, sample_rate: int = 0, ring_timeout: float = 45.0) -> TgCall:
        """Calls a Telegram user and returns once media is connected. Raises TgCallError otherwise."""
        uid = input_user.user_id
        if uid in self.calls and self.calls[uid].active:
            raise TgCallError("already in a call with this user", 486)
        call = TgCall(self, uid, outgoing=True, sample_rate=sample_rate or self.default_rate)
        self.calls[uid] = call
        try:
            await self.ntg.create_p2p_call(uid)
            await self._configure_media(call)
            g_a_hash = await self.ntg.init_exchange(uid, await self._dh_config(), None)
            call._set_state(TgCallState.REQUESTING)
            try:
                result = await self.account.invoke(functions.phone.RequestCallRequest(
                    user_id=input_user, g_a_hash=g_a_hash, protocol=self.tl_protocol(),
                    video=False, random_id=random.randint(1, 0x7FFFFFFE),
                ))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            pc = result.phone_call
            call.call_id = pc.id
            call.access_hash = pc.access_hash
            try:
                g_b = await asyncio.wait_for(asyncio.shield(call._accepted), ring_timeout)
            except asyncio.TimeoutError:
                raise TgCallError("no answer", 480) from None
            call._set_state(TgCallState.ACCEPTING)
            auth = await self.ntg.exchange_keys(uid, g_b, 0)
            try:
                confirmed = await self.account.invoke(functions.phone.ConfirmCallRequest(
                    peer=call.peer, g_a=auth.g_a_or_b, key_fingerprint=auth.key_fingerprint, protocol=self.tl_protocol(),
                ))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            pc2 = confirmed.phone_call
            if not isinstance(pc2, types.PhoneCall):
                raise TgCallError(f"unexpected confirmCall result {type(pc2).__name__}", 480)
            await self._connect_media(call, pc2)
            return call
        except TgCallError as e:
            if call.active:
                await self._end_call(call, e.reason, local=True)
            raise
        except Exception as e:
            log.exception("outgoing telegram call failed")
            if call.active:
                await self._end_call(call, "failed", local=True)
            raise TgCallError(f"call setup failed: {e}", 480) from e

    # ---- incoming ----

    async def _accept(self, call: TgCall) -> None:
        if call.state != TgCallState.INCOMING:
            raise TgCallError("call is not in incoming state", 480)
        uid = call.user_id
        try:
            call._set_state(TgCallState.ACCEPTING)
            if call.invite_msg_id is not None:
                await self._join_conference(call, types.InputGroupCallInviteMessage(msg_id=call.invite_msg_id))
                call._set_state(TgCallState.CONNECTING)
                try:
                    await asyncio.wait_for(asyncio.shield(call.connected), CONNECT_TIMEOUT)
                except asyncio.TimeoutError:
                    raise TgCallError("conference media did not connect", 480) from None
                return
            await self.ntg.create_p2p_call(uid)
            await self._configure_media(call)
            g_b = await self.ntg.init_exchange(uid, await self._dh_config(), call.g_a_hash)
            try:
                await self.account.invoke(functions.phone.AcceptCallRequest(peer=call.peer, g_b=g_b, protocol=self.tl_protocol()))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            try:
                pc = await asyncio.wait_for(asyncio.shield(call._confirmed), CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                raise TgCallError("caller did not confirm the call", 480) from None
            await self.ntg.exchange_keys(uid, pc.g_a_or_b, pc.key_fingerprint)
            await self._connect_media(call, pc)
        except TgCallError as e:
            if call.active:
                await self._end_call(call, e.reason, local=True)
            raise
        except Exception as e:
            log.exception("accepting telegram call failed")
            if call.active:
                await self._end_call(call, "failed", local=True)
            raise TgCallError(f"accept failed: {e}", 480) from e

    async def cancel(self, user_id: int, reason: str = "missed") -> None:
        call = self.calls.get(user_id)
        if call is not None and call.active:
            await self._end_call(call, reason, local=True)

    # ---- teardown ----

    async def _discard(self, call: TgCall, reason: str) -> None:
        """Tells Telegram the call is over: leaves the conference, declines the invite or hangs up."""
        conf = call.conference
        try:
            if conf is not None and conf.joined:
                await self.account.invoke(functions.phone.LeaveGroupCallRequest(call=conf.input, source=0))
            elif call.invite_msg_id is not None:
                await self.account.invoke(functions.phone.DeclineConferenceCallInviteRequest(msg_id=call.invite_msg_id))
            elif call.call_id is not None:
                duration = int(time.time() - call.connected_at) if call.connected_at else 0
                await self.account.invoke(functions.phone.DiscardCallRequest(
                    peer=call.peer, duration=duration, reason=_discard_reason(reason), connection_id=0, video=False,
                ))
        except RPCError as e:
            log.debug("ending the telegram call failed: %s", e)

    async def _end_call(self, call: TgCall, reason: str, local: bool, notify_peer: bool = True) -> None:
        if call.state == TgCallState.ENDED:
            return
        call.end_reason = reason
        call._media_ready = False
        if local and notify_peer:
            await self._discard(call, reason)
        if self.conferences.get(call.user_id) is call.conference:
            self.conferences.pop(call.user_id, None)
        try:
            await self.ntg.stop(call.user_id)
        except Exception:
            pass
        if call._sig_task:
            call._sig_out.put_nowait(None)
            call._sig_task = None
        futures = [call._accepted, call._confirmed, call.connected]
        if call.conference is not None:
            futures.append(call.conference.connected)
        for fut in futures:
            if not fut.done():
                fut.set_exception(TgCallError(reason))
                fut.exception()
        if not call.ended.done():
            call.ended.set_result(reason)
        if self.calls.get(call.user_id) is call:
            self.calls.pop(call.user_id, None)
        call._set_state(TgCallState.ENDED)

    # ---- NTgCalls callbacks (worker threads) ----

    def _on_connection_change(self, user_id: int, info) -> None:
        state = info.state
        name = getattr(state, "name", str(state))
        self.loop.call_soon_threadsafe(self._connection_changed, int(user_id), name)

    def _connection_changed(self, user_id: int, name: str) -> None:
        conf = self.conferences.get(user_id)
        if conf is not None and name == "CONNECTED" and not conf.connected.done():
            conf.connected.set_result(True)
        group = self.groups.get(user_id)
        if group is not None:
            log.info("group call %s: media %s", group.title, name)
            if name == "CONNECTED" and not group.connected.done():
                group.connected.set_result(True)
            elif name in ("FAILED", "TIMEOUT", "CLOSED"):
                self.loop.create_task(self.leave_group(group, "disconnect" if name != "CLOSED" else "hangup"))
            return
        call = self.calls.get(user_id)
        if call is None:
            return
        log.info("tg call with %s: media %s", user_id, name)
        if name == "CONNECTED":
            if not call.connected.done():
                call.connected.set_result(True)
            call._set_state(TgCallState.CONNECTED)
        elif name in ("FAILED", "TIMEOUT", "CLOSED"):
            if call.state == TgCallState.ENDED or call._migrating:
                return
            self.loop.create_task(self._end_call(call, "disconnect" if name != "CLOSED" else "hangup", local=True))

    def _on_frames(self, user_id: int, mode, device, frames) -> None:
        if mode != ntgcalls.StreamMode.PLAYBACK:
            return
        group = self.groups.get(int(user_id))
        if group is not None:
            if group.on_audio is None:
                return
            for f in frames:
                data = f.data
                if data:
                    group.frames_in += 1
                    group.on_audio(int(getattr(f, "ssrc", 0)), bytes(data))
            return
        call = self.calls.get(int(user_id))
        if call is None or call.on_audio is None:
            return
        pcm = merge_frames(frames)
        if pcm:
            call.frames_in += 1
            call.on_audio(pcm)

    def _on_signaling(self, user_id: int, data: bytes) -> None:
        self.loop.call_soon_threadsafe(self._queue_signaling, int(user_id), bytes(data))

    def _queue_signaling(self, user_id: int, data: bytes) -> None:
        call = self.calls.get(user_id)
        if call is not None and call.active:
            call._sig_out.put_nowait(data)

    def _on_emojis(self, key: int, emojis: str) -> None:
        self.loop.call_soon_threadsafe(self._emojis_changed, int(key), str(emojis))

    def _emojis_changed(self, key: int, emojis: str) -> None:
        call = self.calls.get(key)
        if call is not None and call.active:
            call.emojis = emojis
            call._updated()

    def _on_outbound_block(self, key: int, block: bytes) -> None:
        self.loop.call_soon_threadsafe(self._spawn, self._broadcast_block(int(key), bytes(block)))

    def _on_subchain_request(self, key: int, request) -> None:
        self.loop.call_soon_threadsafe(self._spawn, self._fetch_subchain(
            int(key), int(request.subchain), int(request.height), int(request.limit)))

    def _on_request_participants(self, key: int, request=None) -> None:
        self.loop.call_soon_threadsafe(self._refresh_participants_of, int(key))

    def _refresh_participants_of(self, key: int) -> None:
        conf = self.conferences.get(key)
        if conf is not None:
            self.loop.create_task(self._refresh_participants(conf))

    def _spawn(self, coro) -> None:
        self.loop.create_task(coro)

    # ---- MTProto updates ----

    async def _on_raw_update(self, update) -> None:
        if isinstance(update, types.UpdatePhoneCallSignalingData):
            call = self._by_call_id(update.phone_call_id)
            if call is None or call.conference is not None:
                return
            if call.state in (TgCallState.CONNECTING, TgCallState.CONNECTED) and call._sig_task is not None:
                try:
                    await self.ntg.send_signaling_data(call.user_id, update.data)
                except Exception as e:
                    log.debug("send_signaling_data failed: %s", e)
            else:
                call._sig_in.append(bytes(update.data))
            return
        if isinstance(update, types.UpdateGroupCallChainBlocks):
            conf = self._conference_by_call(update.call)
            if conf is not None:
                await self._apply_blocks(conf, update, from_short_poll=False)
            return
        if isinstance(update, types.UpdateGroupCallParticipants):
            conf = self._conference_by_call(update.call)
            if conf is None:
                return
            for p in update.participants:
                uid = utils.get_peer_id(p.peer)
                if p.left:
                    conf.sources.pop(uid, None)
                else:
                    conf.sources[uid] = p.source
            await self._participants_changed(conf)
            return
        if isinstance(update, types.UpdateGroupCall):
            call_id = getattr(update.call, "id", None)
            group = next((g for g in self.groups.values() if g.call is not None and g.call.id == call_id), None)
            if group is not None and isinstance(update.call, types.GroupCallDiscarded):
                await self.leave_group(group, "hangup")
            conf = self._conference_by_call(update.call)
            call = self.calls.get(conf.key) if conf is not None else None
            if call is not None and call.conference is conf and isinstance(update.call, types.GroupCallDiscarded):
                await self._end_call(call, "hangup", local=False)
            return
        if isinstance(update, (types.UpdateNewMessage, types.UpdateEditMessage)):
            msg = update.message
            if isinstance(msg, types.MessageService) and isinstance(msg.action, types.MessageActionConferenceCall):
                if isinstance(update, types.UpdateNewMessage):
                    await self._conference_invite(msg)
                elif msg.action.missed:
                    call = next((c for c in self.calls.values() if c.invite_msg_id == msg.id), None)
                    if call is not None and call.state == TgCallState.INCOMING:
                        await self._end_call(call, "missed", local=False)
            return
        if not isinstance(update, types.UpdatePhoneCall):
            return
        pc = update.phone_call
        if isinstance(pc, types.PhoneCallRequested):
            await self._incoming_requested(pc)
        elif isinstance(pc, types.PhoneCallWaiting):
            call = self._by_call_id(pc.id)
            if call and call.outgoing and pc.receive_date and call.state == TgCallState.REQUESTING:
                call._set_state(TgCallState.RINGING)
        elif isinstance(pc, types.PhoneCallAccepted):
            call = self._by_call_id(pc.id)
            if call and not call._accepted.done():
                call._accepted.set_result(pc.g_b)
        elif isinstance(pc, types.PhoneCall):
            call = self._by_call_id(pc.id)
            if call and not call._confirmed.done():
                call._confirmed.set_result(pc)
        elif isinstance(pc, types.PhoneCallDiscarded):
            call = self._by_call_id(pc.id)
            if call is None or call._migrating or call.conference is not None:
                return
            if isinstance(pc.reason, types.PhoneCallDiscardReasonMigrateConferenceCall) and call.state == TgCallState.CONNECTED:
                call._migrating = True              # the private media may report CLOSED before the task runs
                self.loop.create_task(self._migrate(call, pc.reason.slug))
                return
            reason = type(pc.reason).__name__.replace("PhoneCallDiscardReason", "").lower() if pc.reason else "hangup"
            log.info("tg call with %s discarded by peer: %s", call.user_id, reason)
            await self._end_call(call, reason, local=False)

    async def _incoming_requested(self, pc: types.PhoneCallRequested) -> None:
        uid = pc.admin_id
        existing = self.calls.get(uid)
        if existing and existing.active:
            if existing.call_id == pc.id:
                return
            log.info("second call from %s while one is active; declining busy", uid)
            try:
                await self.account.invoke(functions.phone.DiscardCallRequest(
                    peer=types.InputPhoneCall(id=pc.id, access_hash=pc.access_hash), duration=0,
                    reason=types.PhoneCallDiscardReasonBusy(), connection_id=0, video=False))
            except RPCError:
                pass
            return
        call = TgCall(self, uid, outgoing=False, sample_rate=self.default_rate)
        call.call_id = pc.id
        call.access_hash = pc.access_hash
        call.g_a_hash = pc.g_a_hash
        call.video = bool(pc.video)
        self.calls[uid] = call
        call._set_state(TgCallState.INCOMING)
        try:
            await self.account.invoke(functions.phone.ReceivedCallRequest(peer=call.peer))
        except RPCError as e:
            log.debug("receivedCall failed: %s", e)
        await self._offer_incoming(call)

    async def _offer_incoming(self, call: TgCall) -> None:
        if self.on_incoming:
            r = self.on_incoming(call)
            if asyncio.iscoroutine(r):
                self.loop.create_task(r)
        else:
            await self._end_call(call, "busy", local=True)
