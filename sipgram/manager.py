"""Call manager: one gateway Telegram account, many users, many SIP accounts, concurrent calls.

Per user there is at most one Telegram call; SIP legs attach to it: `active` (bridged),
`held` (music on hold from the PBX) and `waiting` (a second incoming call still ringing).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from telethon.tl import types

from .audio import tones
from .audio.record import CallRecorder, RecordingUnavailable
from .bridge import CallBridge, GroupBridge, SipSipBridge
from .config import AccountConfig, CallsConfig, Config, GatewayConfig, UserConfig
from .history import CallHistory, record
from .messages import LANGUAGES, default_language, fmt_duration, fmt_number, fmt_time, t
from .prefs import UserPrefs
from .schedule import screen, time_closed
from .sip.account import CallState, SipAccount, SipCall
from .sip.codecs import codec_list
from .tg.account import TgAccount
from .tg.calls import TgCall, TgCallEngine, TgCallError
from .tg.group import TgGroupCall
from .tg.notify import Buttons, Notifier
from .util import detect_local_ip

log = logging.getLogger("sipgram.calls")

NUMBER_RE = re.compile(r"^\+?[\d\s\-()*#]{2,20}$")   # digits plus feature codes like *97
RESULTS = ("answered", "missed", "busy", "failed", "declined", "transferred", "blocked")
DTMF_RE = re.compile(r"^[0-9*#]{1,12}$")
TG_ERROR_TO_SIP = {"busy": 486, "hangup": 603, "declined": 603, "missed": 480, "no answer": 480, "disconnect": 480}
RECONNECT_REASONS = ("disconnect", "failed", "timeout")


@dataclass
class GatewayRuntime:
    """One Telegram account: it calls the users assigned to it and receives their calls."""
    cfg: GatewayConfig
    account: TgAccount
    engine: TgCallEngine

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def free(self) -> bool:
        return len(self.engine.calls) < self.cfg.max_calls


@dataclass
class AccountRuntime:
    cfg: AccountConfig
    sip: SipAccount
    owners: list[UserState] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.cfg.name


@dataclass
class GroupSession:
    """One Telegram voice chat with the SIP legs the users moved into it."""
    call: TgGroupCall
    bridge: GroupBridge
    gateway: str
    legs: list[Leg] = field(default_factory=list)


@dataclass
class Leg:
    call: SipCall
    account: AccountRuntime
    direction: str
    peer: str
    number: str
    user: UserState
    result: str = ""
    connected_notified: bool = False


class UserState:
    def __init__(self, cfg: UserConfig, input_user: types.InputUser, gw: GatewayRuntime | None = None,
                 prefs: UserPrefs | None = None):
        self.cfg = cfg
        self.gw = gw
        self.id = input_user.user_id
        self.input_user = input_user
        self.name = cfg.name or str(self.id)
        self.prefs = prefs or UserPrefs(None)
        self.lang = self.prefs.get(self.id, "lang") or cfg.language or default_language()
        self.accounts: list[AccountRuntime] = []
        self.default_account: AccountRuntime | None = None
        self.tg: TgCall | None = None
        self.tg_pending = False
        self.active: Leg | None = None
        self.held: Leg | None = None
        self.waiting: Leg | None = None
        self.waiting_timer: asyncio.TimerHandle | None = None
        self.bridge: CallBridge | None = None
        self.pending_number: tuple[str, float] | None = None
        self.reconnect_task: asyncio.Task | None = None
        self.ringing: tuple[str, str] | None = None        # caller and line of an incoming call still ringing
        self.announced: TgCall | None = None
        self.lock = asyncio.Lock()
        self.rules = cfg.rules
        self._dnd = bool(self.prefs.get(self.id, "dnd", False))

    def t(self, key: str, **kw) -> str:
        return t(key, self.lang, **kw)

    def set_lang(self, lang: str) -> None:
        self.lang = lang
        self.prefs.set(self.id, "lang", lang)

    @property
    def dnd(self) -> bool:
        return self._dnd

    @dnd.setter
    def dnd(self, value: bool) -> None:
        self._dnd = bool(value)
        self.prefs.set(self.id, "dnd", self._dnd)

    @property
    def busy(self) -> bool:
        return self.tg is not None or self.tg_pending

    def legs(self) -> list[Leg]:
        return [leg for leg in (self.active, self.held, self.waiting) if leg is not None]


class CallManager:
    def __init__(self, cfg: Config, gateways: list[GatewayRuntime], notifier: Notifier,
                 history: CallHistory, prefs: UserPrefs | None = None):
        self.cfg = cfg
        self.calls: CallsConfig = cfg.calls
        self.gateways = {gw.name: gw for gw in gateways}
        self.notifier = notifier
        self.history = history
        self.prefs = prefs or UserPrefs(None)
        self.loop = asyncio.get_event_loop()
        self.users: dict[int, UserState] = {}
        self.accounts: list[AccountRuntime] = []
        self.shared: list[AccountRuntime] = []
        self.sip_bridges: list[tuple[SipSipBridge, SipCall, SipCall]] = []
        self.group_calls: dict[str, GroupSession] = {}
        self.alerts = None
        self._unknown_warned: set[int] = set()

    # ---- lifecycle ----

    def default_rate(self) -> int:
        if self.calls.tg_sample_rate:
            return self.calls.tg_sample_rate
        codecs = codec_list(self.cfg.accounts[0].codecs)
        return codecs[0].rate if codecs else 8000

    async def start(self) -> None:
        by_spec: dict[str, UserState] = {}
        default_gw = next(iter(self.gateways.values()))
        for ucfg in self.cfg.users:
            gw = self.gateways.get(ucfg.gateway) if ucfg.gateway else default_gw
            if gw is None:
                log.error("user %s: no gateway named %s", ucfg.id, ucfg.gateway)
                continue
            try:
                iu = await gw.account.resolve_user(ucfg.id)
            except Exception as e:
                log.error("cannot resolve user %s on gateway %s: %s", ucfg.id, gw.name, e)
                continue
            state = self.users.get(iu.user_id) or UserState(ucfg, iu, gw, self.prefs)
            self.users[iu.user_id] = state
            by_spec[ucfg.id] = state
            await gw.account.ensure_contact(iu, ucfg.name)
        if not self.users:
            raise RuntimeError("no usable users (none could be resolved)")
        for acfg in self.cfg.accounts:
            local_ip = acfg.local_ip or detect_local_ip(acfg.server, acfg.port)
            sip = SipAccount(acfg, local_ip)
            rt = AccountRuntime(acfg, sip)
            for spec in acfg.users:
                u = by_spec.get(spec)
                if u is None:
                    log.error("account %s: user %s is not resolvable, skipped", acfg.name, spec)
                    continue
                rt.owners.append(u)
                u.accounts.append(rt)
                if u.default_account is None:
                    u.default_account = rt
            if acfg.shared:
                self.shared.append(rt)
            sip.on_incoming_call = lambda call, rt=rt: self._sip_incoming(rt, call)
            sip.on_registration = lambda ok, detail, rt=rt: self._registration(rt, ok, detail)
            sip.on_message_text = lambda sender, text, rt=rt: self._sip_text(rt, sender, text)
            await sip.start()
            self.accounts.append(rt)
            log.info("account %s: %s@%s via %s:%d (local %s:%d) owners=%s shared=%s", acfg.name, acfg.username,
                     acfg.domain, acfg.server, acfg.port, local_ip, sip.transport.local_port,
                     [u.id for u in rt.owners], acfg.shared)
        for gw in self.gateways.values():
            gw.engine.on_incoming = self._tg_incoming
            gw.account.add_message_handler(self._on_account_message)
            log.info("gateway account %s: session %s, up to %d calls, users %s", gw.name, gw.cfg.session,
                     gw.cfg.max_calls, [u.id for u in self.users.values() if u.gw is gw])

    def _registration(self, rt: AccountRuntime, ok: bool, detail: str) -> None:
        log.log(logging.INFO if ok else logging.WARNING, "account %s: registration %s (%s)",
                rt.name, "OK" if ok else "FAILED", detail)
        if self.alerts is not None:
            self.alerts.registration(rt.name, ok, detail)

    async def stop(self) -> None:
        for u in list(self.users.values()):
            await self._end_everything(u, "shutdown", notify=False)
        for session in list(self.group_calls.values()):
            await self._leave_group(session)
        for bridge, a, b in list(self.sip_bridges):
            bridge.stop()
            for c in (a, b):
                if c.active:
                    await c.hangup()
        for rt in self.accounts:
            await rt.sip.stop()

    # ---- status for the health file ----

    def snapshot(self) -> dict:
        return {
            "accounts": {rt.name: rt.sip.registered for rt in self.accounts},
            "calls": sum(len(u.legs()) for u in self.users.values()) + len(self.sip_bridges) * 2,
            "telegram_calls": {gw.name: len(gw.engine.calls) for gw in self.gateways.values()},
            "group_calls": {name: len(s.legs) for name, s in self.group_calls.items()},
        }

    # ---- helpers ----

    def apply_dial_rules(self, number: str) -> str:
        out = number.strip()
        for rule in self.calls.dial_rules:
            if len(rule) >= 2:
                out = re.sub(rule[0], rule[1], out)
        return out

    @staticmethod
    def _caller_text(call: SipCall) -> str:
        number = fmt_number(call.caller_number) if call.caller_number else ""
        if call.caller_name and call.caller_name != call.caller_number:
            return f"{call.caller_name} {number}".strip()
        return number or "?"

    async def _maybe_alert(self, u: UserState, peer: str, error: Exception) -> None:
        """Only operational problems reach the administrator; a busy or missed call is normal."""
        if self.alerts is None:
            return
        reason = getattr(error, "reason", str(error))
        if any(word in reason.lower() for word in ("flood", "privacy", "blocked", "mismatch", "telegram error")):
            await self.alerts.telegram_problem("call", reason)
            await self.alerts.call_failed(u.name, peer, reason)

    @staticmethod
    def _tg_problem(u: UserState, e: TgCallError) -> str:
        """What the user should know when the gateway could not reach them in Telegram; "" for a plain miss."""
        reason = e.reason.lower()
        if "privacy" in reason:
            return u.t("tg_privacy")
        if "blocked" in reason:
            return u.t("tg_blocked")
        if "flood" in reason:
            return u.t("tg_flood")
        if "did not connect" in reason:
            return u.t("tg_no_media")
        return ""

    @staticmethod
    def _sip_reason(u: UserState, code: int, text: str) -> str:
        key = f"sip_{code}"
        human = u.t(key)
        return f"{human} ({code})" if human != key else f"{code} {text}".strip()

    def lang_of(self, user_id: int) -> str:
        u = self.users.get(user_id)
        return u.lang if u else default_language()

    @staticmethod
    def _capacity_left(u: UserState) -> bool:
        return u.gw is None or u.gw.free

    def account_of(self, user_id: int) -> TgAccount:
        u = self.users.get(user_id)
        if u is not None and u.gw is not None:
            return u.gw.account
        return next(iter(self.gateways.values())).account

    def _fresh_pending(self, u: UserState) -> str:
        item = u.pending_number
        u.pending_number = None
        if item and time.time() - item[1] <= self.calls.pending_number_ttl:
            return item[0]
        return ""

    def _outgoing_account(self, u: UserState) -> AccountRuntime | None:
        if u.default_account is not None:
            return u.default_account
        return self.shared[0] if self.shared else None

    async def _resolve_dialed(self, dialed: str) -> UserState | None:
        d = (dialed or "").strip()
        spec = ""
        if re.fullmatch(r"\+\d{7,15}", d):
            spec = d
        elif d.lower().startswith("tg#"):
            spec = "@" + d[3:]
        elif d.isdigit() and len(d) >= 6:
            spec = d
        if not spec:
            return None
        for gw in self.gateways.values():
            try:
                iu = await gw.account.resolve_user(spec)
            except Exception as e:
                log.debug("dialed %r not resolvable on gateway %s: %s", d, gw.name, e)
                continue
            user = self.users.get(iu.user_id)
            if user is not None:
                return user
        log.warning("dialed %r does not map to a configured user", d)
        return None

    # ---- card (bot) ----

    def card_for(self, uid: int) -> tuple[str, Buttons | None, bool] | None:
        u = self.users.get(uid)
        if u is None:
            return None
        if not u.legs():
            if u.ringing is None:
                return None
            caller, account = u.ringing
            return u.t("incoming", caller=caller, account=account), [[(u.t("btn_decline"), "/decline-incoming")]], False
        lines: list[str] = []
        buttons: Buttons = []
        keypad_ok = False
        if u.active:
            c = u.active.call
            if c.state == CallState.CONNECTED:
                lines.append(u.t("card_in_call", peer=u.active.peer, duration=fmt_duration(time.time() - c.connected_at)))
                keypad_ok = True
            else:
                lines.append(u.t("card_dialing", number=u.active.peer))
        tg = u.tg
        if tg is not None and tg.conference is not None:
            lines.append(u.t("card_conference", n=tg.others))
        if tg is not None and tg.emojis:
            lines.append(u.t("card_key", emojis=tg.emojis))
        if u.held:
            lines.append(u.t("card_held", peer=u.held.peer))
        if u.waiting:
            lines.append(u.t("card_waiting", caller=u.waiting.peer))
            buttons.append([(u.t("btn_accept_waiting"), "/switch"), (u.t("btn_decline"), "/decline")])
        elif u.held and u.active:
            buttons.append([(u.t("btn_switch"), "/switch"), (u.t("btn_transfer"), "/transfer")])
        elif u.held:
            buttons.append([(u.t("btn_switch"), "/switch")])
        if u.active or u.tg:
            buttons.append([(u.t("btn_hangup"), "/hangup")])
        return "\n".join(lines) or u.t("status_idle"), buttons, keypad_ok

    async def _update_card(self, u: UserState, closing: tuple[str, Buttons | None] | None = None,
                           announce: bool = True) -> bool:
        """Redraws the live call card and tells whether the user has one.

        `closing` is the last word about a call. When nothing else is going on it replaces the card, so
        a call leaves one message in the chat; otherwise, or without the bot, it goes out as a message
        of its own, unless `announce` is off.
        """
        spec = self.card_for(u.id)
        if spec is None:
            text, buttons = closing or (u.t("call_ended_short"), None)
            if not await self.notifier.close_card(u.id, text, buttons) and closing is not None and announce:
                await self._notify(u, *closing)
            return False
        if closing is not None and announce:
            await self._notify(u, *closing)
        text, buttons, keypad_ok = spec
        return await self.notifier.card(u.id, text, buttons, keypad_ok)

    async def _notify(self, u: UserState, text: str, buttons: Buttons | None = None) -> None:
        await self.notifier.send(u.id, text, buttons)

    # ---- SIP -> Telegram ----

    async def _sip_incoming(self, rt: AccountRuntime, call: SipCall) -> None:
        targets: list[UserState] = []
        if rt.cfg.shared or (rt.cfg.route_by_dialed and call.dialed and call.dialed != rt.cfg.username):
            u = await self._resolve_dialed(call.dialed)
            if u is not None:
                targets = [u]
        if not targets:
            targets = list(rt.owners) if rt.cfg.ring_all else rt.owners[:1]
        if not targets:
            log.info("account %s: no target for call from %s (dialed %s)", rt.name, call.caller_number, call.dialed)
            call.reject(404)
            return
        if not await self._screen(rt, call, targets):
            return
        if all(u.dnd for u in targets):
            call.reject(486)
            return
        idle = [u for u in targets if not u.dnd and not u.busy]
        if idle:
            if not all(self._capacity_left(u) for u in idle):
                log.warning("gateway account at capacity, rejecting SIP call from %s", call.caller_number)
                call.reject(486)
                return
            await self._ring_users(rt, call, idle)
            return
        u = next(u for u in targets if not u.dnd)
        await self._offer_waiting(u, rt, call)

    async def _screen(self, rt: AccountRuntime, call: SipCall, targets: list[UserState]) -> bool:
        """Applies each target's schedule and lists. Returns False when the call was answered by a rule."""
        allowed: list[UserState] = []
        blocked: list[tuple[UserState, str]] = []
        for u in targets:
            decision = screen(u.rules, call.caller_number, call.caller_name)
            if decision.allowed:
                allowed.append(u)
            else:
                blocked.append((u, decision.reason))
        if allowed:
            targets[:] = allowed
            return True
        u, reason = blocked[0]
        caller = self._caller_text(call)
        log.info("account %s: call from %s filtered (%s)", rt.name, call.caller_number or caller, reason)
        for user, why in blocked:
            self.history.add(user.id, record("in", call.caller_number or caller, rt.name, 0.0, "blocked"))
            if user.rules.notify:
                text = user.t("filtered_forwarded", caller=caller, reason=user.t(f"reason_{why}"),
                              number=fmt_number(user.rules.forward)) if user.rules.forward else \
                    user.t("filtered", caller=caller, reason=user.t(f"reason_{why}"))
                await self._notify(user, text, [[(user.t("btn_callback"), "/cb")]])
        if u.rules.forward:
            call.redirect(u.rules.forward)
        else:
            call.reject(u.rules.code)
        return False

    async def _ring_users(self, rt: AccountRuntime, call: SipCall, users: list[UserState]) -> None:
        caller = self._caller_text(call)
        call.ringing()
        for u in users:
            u.tg_pending = True
            if self.calls.notify_incoming:
                u.ringing = (caller, rt.name)
                if not await self._update_card(u):
                    await self._notify(u, u.t("incoming", caller=caller, account=rt.name), [[(u.t("btn_decline"), "/decline-incoming")]])
        tasks: dict[asyncio.Task, UserState] = {}
        for u in users:
            task = self.loop.create_task(u.gw.engine.call(u.input_user, sample_rate=call.rate, ring_timeout=rt.cfg.ring_timeout))
            tasks[task] = u
        winner: UserState | None = None
        tg_call: TgCall | None = None
        codes: list[int] = []
        try:
            while tasks and winner is None:
                done, _ = await asyncio.wait({*tasks, call.ended}, return_when=asyncio.FIRST_COMPLETED)
                if call.ended in done:
                    break
                for task in list(done):
                    if task is call.ended:
                        continue
                    u = tasks.pop(task)
                    try:
                        tg_call = task.result()
                        winner = u
                        break
                    except TgCallError as e:
                        codes.append(e.sip_code if e.sip_code != 480 else TG_ERROR_TO_SIP.get(e.reason, 480))
                        u.tg_pending = False
                        await self._maybe_alert(u, caller, e)
                        missed = e.reason in ("missed", "no answer")
                        self.history.add(u.id, record("in", call.caller_number or caller, rt.name, 0.0,
                                                      "missed" if missed else e.reason))
                        problem = self._tg_problem(u, e)
                        declined = e.reason in ("hangup", "declined") and not problem
                        await self._ring_over(u, caller, rt, missed=not declined, why=problem)
                    except Exception as e:
                        log.exception("telegram call task failed: %s", e)
                        u.tg_pending = False
                        codes.append(480)
                        await self._ring_over(u, caller, rt, missed=True)
        finally:
            for task, u in tasks.items():
                await u.gw.engine.cancel(u.id, "missed")
                u.tg_pending = False
                task.cancel()
        left = list(tasks.values())
        if winner is None or tg_call is None:
            if call.active:
                code = 486 if codes and all(c == 486 for c in codes) else (603 if codes and all(c == 603 for c in codes) else 480)
                call.reject(code)
            for u in left:
                self.history.add(u.id, record("in", call.caller_number or caller, rt.name, 0.0, "missed"))
                await self._ring_over(u, caller, rt, missed=True)
            return
        for u in left:
            await self._ring_over(u, caller, rt, missed=False, taken_by=winner.name)
        u = winner
        u.tg_pending = False
        if not call.active:
            log.info("PBX gave up before %s answered", u.id)
            await tg_call.hangup("hangup")
            await self._ring_over(u, caller, rt, missed=True)
            return
        u.ringing = None
        leg = Leg(call, rt, "in", caller, call.caller_number, u)
        call.tag = leg
        async with u.lock:
            u.tg = tg_call
            self._watch_tg(u, tg_call)
            self._attach(u, leg)
            call.answer()
        await self._update_card(u)

    async def _ring_over(self, u: UserState, caller: str, rt: AccountRuntime, missed: bool,
                         why: str = "", taken_by: str = "") -> None:
        """Closes the incoming call card of a user who did not take the call."""
        if u.ringing is None:
            return
        u.ringing = None
        if missed:
            text = u.t("missed", caller=caller, account=rt.name)
            if why:
                text += "\n" + u.t("missed_reason", reason=why)
            await self._update_card(u, (text, [[(u.t("btn_callback"), "/cb")]]))
        elif taken_by:
            await self._update_card(u, (u.t("taken_by", caller=caller, name=taken_by), None), announce=False)
        else:
            await self._update_card(u, (u.t("declined", caller=caller), [[(u.t("btn_callback"), "/cb")]]), announce=False)

    async def _offer_waiting(self, u: UserState, rt: AccountRuntime, call: SipCall) -> None:
        if not self.calls.call_waiting or u.waiting or u.held or u.reconnect_task or u.tg is None:
            call.reject(486)
            return
        caller = self._caller_text(call)
        leg = Leg(call, rt, "in", caller, call.caller_number, u)
        call.tag = leg
        u.waiting = leg
        call.ringing()
        self._watch_leg(leg)
        u.waiting_timer = self.loop.call_later(rt.cfg.ring_timeout, lambda: self.loop.create_task(self._waiting_timeout(u, leg)))
        buttons = [[(u.t("btn_accept_waiting"), "/switch"), (u.t("btn_decline"), "/decline")]]
        await self._notify(u, u.t("incoming_waiting", caller=caller), buttons)
        await self._update_card(u)

    async def _waiting_timeout(self, u: UserState, leg: Leg) -> None:
        if u.waiting is leg and leg.call.active:
            leg.result = "missed"
            leg.call.reject(480)

    # ---- Telegram -> SIP ----

    async def _tg_incoming(self, tg_call: TgCall) -> None:
        u = self.users.get(tg_call.user_id)
        if u is None or not u.cfg.can_call:
            log.info("call from unknown/disallowed telegram user %s declined", tg_call.user_id)
            await tg_call.hangup("busy")
            return
        if u.busy:
            await tg_call.hangup("busy")
            return
        rt = self._outgoing_account(u)
        dest = self._fresh_pending(u) or (rt.cfg.default_destination if rt else "")
        if rt is None or not dest:
            await tg_call.hangup("hangup")
            await self._notify(u, u.t("need_number") if rt else u.t("no_line"))
            return
        u.tg_pending = True
        try:
            await tg_call.accept()
        except TgCallError as e:
            log.info("accepting telegram call from %s failed: %s", u.id, e)
            u.tg_pending = False
            return
        async with u.lock:
            u.tg_pending = False
            u.tg = tg_call
            self._watch_tg(u, tg_call)
        await self._dial_leg(u, rt, dest, consult=False)

    async def _dial(self, u: UserState, number: str, rt: AccountRuntime | None = None) -> None:
        if not u.cfg.can_call:
            return
        rt = rt or self._outgoing_account(u)
        if rt is None:
            await self._notify(u, u.t("no_line"))
            return
        if u.tg is not None and not u.tg_pending:
            if u.held or u.waiting or u.active is None:
                await self._notify(u, u.t("busy_line"))
                return
            async with u.lock:
                current = u.active
                self._stop_bridge(u, current.peer)
                await current.call.hold()
                u.held = current
                u.active = None
            await self._dial_leg(u, rt, number, consult=True)
            return
        if u.busy:
            await self._notify(u, u.t("busy_line"))
            return
        if not self._capacity_left(u):
            await self._notify(u, u.t("busy_gateway", n=u.gw.cfg.max_calls))
            return
        if self.calls.outgoing_mode == "direct":
            await self._dial_direct(u, rt, number)
            return
        u.tg_pending = True
        await self._notify(u, u.t("dialing_callback", number=fmt_number(number)))
        try:
            tg_call = await u.gw.engine.call(u.input_user, sample_rate=self.default_rate(), ring_timeout=self.calls.outgoing_ring_timeout)
        except TgCallError as e:
            log.info("callback to %s failed: %s", u.id, e)
            u.tg_pending = False
            problem = self._tg_problem(u, e)
            if problem:
                await self._notify(u, u.t("callback_failed", reason=problem))
            return
        async with u.lock:
            u.tg_pending = False
            u.tg = tg_call
            self._watch_tg(u, tg_call)
        await self._dial_leg(u, rt, number, consult=False)

    async def _dial_direct(self, u: UserState, rt: AccountRuntime, number: str) -> None:
        u.tg_pending = True
        await self._notify(u, u.t("dialing_direct", number=fmt_number(number)))
        headers = {"X-TG-User-Id": str(u.id), "X-TG-User-Name": u.name}
        try:
            call = await rt.sip.invite(number, headers=headers)
        except Exception as e:
            u.tg_pending = False
            await self._notify(u, u.t("dial_error", reason=str(e)))
            return
        leg = Leg(call, rt, "out", fmt_number(number), number, u)
        call.tag = leg
        try:
            tg_call = await u.gw.engine.call(u.input_user, sample_rate=call.rate, ring_timeout=self.calls.outgoing_ring_timeout)
        except TgCallError as e:
            log.info("direct mode: telegram leg to %s failed: %s", u.id, e)
            u.tg_pending = False
            await call.hangup()
            problem = self._tg_problem(u, e)
            if problem:
                await self._notify(u, u.t("callback_failed", reason=problem))
            return
        async with u.lock:
            u.tg_pending = False
            u.tg = tg_call
            self._watch_tg(u, tg_call)
            if not call.active:
                await tg_call.hangup("hangup")
                return
            self._attach(u, leg, ringback=True)
        await self._update_card(u)

    async def _dial_leg(self, u: UserState, rt: AccountRuntime, number: str, consult: bool) -> None:
        headers = {"X-TG-User-Id": str(u.id), "X-TG-User-Name": u.name}
        try:
            call = await rt.sip.invite(number, headers=headers)
        except Exception as e:
            log.error("INVITE failed: %s", e)
            await self._notify(u, u.t("dial_error", reason=str(e)))
            if consult and u.held:
                await self._resume_held(u)
            elif u.tg:
                await self._end_everything(u, "invite failed")
            return
        leg = Leg(call, rt, "out", fmt_number(number), number, u)
        call.tag = leg
        async with u.lock:
            if u.tg is None:
                await call.hangup()
                return
            self._attach(u, leg, ringback=True)
        if consult and u.held:
            await self._notify(u, u.t("dialing_consult", number=fmt_number(number), held=u.held.peer))
        await self._update_card(u)

    # ---- attach / bridge / watch ----

    def _stop_bridge(self, u: UserState, peer: str = "") -> None:
        """Stops the audio bridge and, if it was recording, sends the recording to the chat."""
        bridge = u.bridge
        if bridge is None:
            return
        recorder = bridge.stop_recording()
        bridge.stop()
        u.bridge = None
        if recorder is not None:
            self.loop.create_task(self._send_recording(u, recorder, peer or (u.active.peer if u.active else "")))

    async def _send_recording(self, u: UserState, recorder: CallRecorder, peer: str) -> None:
        ogg, duration = recorder.finish()
        if duration < 1:
            return
        caption = u.t("rec_caption", peer=peer or "?", duration=fmt_duration(duration))
        if not await self.notifier.send_voice(u.id, ogg, duration, caption):
            await self._notify(u, u.t("rec_failed", reason="telegram"))
        elif recorder.truncated:
            await self._notify(u, u.t("rec_stopped"))

    def _wants_recording(self, u: UserState) -> bool:
        return self.calls.record == "all" or bool(u.prefs.get(u.id, "record_all", False))

    def _attach(self, u: UserState, leg: Leg, ringback: bool = False) -> None:
        assert u.tg is not None
        self._stop_bridge(u)
        u.active = leg
        rb = tones.ringback(self.calls.ringback, u.tg.sample_rate) if ringback else None
        u.bridge = CallBridge(leg.call, u.tg, self.calls.jitter_ms, ringback=rb)
        u.bridge.start()
        if self._wants_recording(u):
            try:
                u.bridge.start_recording(self.calls.record_max_minutes * 60)
            except RecordingUnavailable as e:
                log.warning("recording is not available: %s", e)
        leg.call.on_state = lambda call, state, leg=leg: self._leg_state(leg, state)
        self._watch_leg(leg)

    def _watch_leg(self, leg: Leg) -> None:
        if getattr(leg, "_watched", False):
            return
        leg._watched = True  # type: ignore[attr-defined]

        async def waiter() -> None:
            await leg.call.ended
            await self._leg_ended(leg)

        self.loop.create_task(waiter())

    def _watch_tg(self, u: UserState, tg_call: TgCall) -> None:
        tg_call.on_update = lambda c: self.loop.create_task(self._tg_updated(u, c))

        async def waiter() -> None:
            reason = await tg_call.ended
            if u.tg is tg_call:
                await self._tg_ended(u, tg_call, reason)

        self.loop.create_task(waiter())

    async def _tg_updated(self, u: UserState, tg_call: TgCall) -> None:
        """The emoji key or the conference participants changed. A peer adding people in Telegram turns
        the private call into a conference; the SIP leg stays bridged to it."""
        if u.tg is not tg_call:
            return
        if tg_call.conference is not None and tg_call.invite_msg_id is None and u.announced is not tg_call:
            u.announced = tg_call
            await self._notify(u, u.t("tg_conference"))
        await self._update_card(u)

    def _leg_state(self, leg: Leg, state: CallState) -> None:
        if state == CallState.CONNECTED and not leg.connected_notified:
            leg.connected_notified = True
            u = leg.user
            if u.active is leg and u.tg is not None and u.tg.sample_rate != leg.call.rate:
                async def fix_rate() -> None:
                    await u.tg.set_sample_rate(leg.call.rate)
                    if u.bridge:
                        u.bridge.update_rate()
                self.loop.create_task(fix_rate())
            self.loop.create_task(self._update_card(u))

    # ---- teardown paths ----

    async def _leg_ended(self, leg: Leg) -> None:
        u = leg.user
        call = leg.call
        connected = call.connected_at
        if not leg.result:
            if leg.result == "transferred":
                pass
            elif connected:
                leg.result = "answered"
            elif leg.direction == "in":
                leg.result = "declined" if call.end_code == 603 else "missed"
            else:
                leg.result = "busy" if call.end_code == 486 else ("failed" if call.end_code >= 300 else "missed")
        self.history.add(u.id, record(leg.direction, leg.number or leg.peer, leg.account.name, connected, leg.result))
        session = self._group_of(leg)
        if session is not None:
            session.bridge.remove(call)
            session.legs.remove(leg)
            await self._notify(u, u.t("group_left_by", peer=leg.peer))
            if not session.legs:
                await self._leave_group(session)
                await self._notify(u, u.t("group_ended"))
            return
        async with u.lock:
            if u.waiting is leg:
                u.waiting = None
                if u.waiting_timer:
                    u.waiting_timer.cancel()
                    u.waiting_timer = None
                if leg.result == "missed":
                    await self._notify(u, u.t("waiting_missed", caller=leg.peer))
                await self._update_card(u)
                return
            if u.held is leg:
                u.held = None
                held_for = fmt_duration(time.time() - connected) if connected else "00:00"
                await self._notify(u, u.t("call_ended", peer=leg.peer, duration=held_for))
                await self._update_card(u)
                return
            if u.active is not leg:
                return
            u.active = None
            self._stop_bridge(u, leg.peer)
            closing = None
            if leg.direction == "out" and not connected and call.end_code >= 300 and u.tg is not None:
                reason = self._sip_reason(u, call.end_code, call.end_reason)
                await self._update_card(u, (u.t("dial_failed", number=leg.peer, reason=reason),
                                            [[(u.t("btn_redial"), "/redial")]]))
                if u.held is None and u.waiting is None:
                    tone_bridge = CallBridge(call, u.tg, self.calls.jitter_ms)
                    tone_bridge.play_tone(tones.busy(self.calls.ringback, u.tg.sample_rate))
                    tone_bridge.start()
                    await asyncio.sleep(2.0)
                    tone_bridge.stop()
            elif connected and leg.result == "answered":
                closing = (u.t("call_ended", peer=leg.peer, duration=fmt_duration(time.time() - connected)),
                           self._after_call_buttons(leg))
            if u.held is not None:
                await self._resume_held(u)
            elif u.waiting is not None and u.tg is not None:
                w = u.waiting
                u.waiting = None
                if u.waiting_timer:
                    u.waiting_timer.cancel()
                    u.waiting_timer = None
                w.call.answer()
                self._attach(u, w)
                await self._notify(u, u.t("resumed", peer=w.peer))
            elif u.tg is not None:
                tg = u.tg
                u.tg = None
                await tg.hangup("hangup")
        await self._update_card(u, closing)

    def _after_call_buttons(self, leg: Leg) -> Buttons:
        u = leg.user
        if leg.direction == "in":
            return [[(u.t("btn_callback"), "/cb")]]
        return [[(u.t("btn_redial"), "/redial")]]

    async def _resume_held(self, u: UserState) -> None:
        h = u.held
        if h is None or u.tg is None:
            return
        u.held = None
        await h.call.unhold()
        self._attach(u, h)
        await self._notify(u, u.t("resumed", peer=h.peer))

    async def _tg_ended(self, u: UserState, tg_call: TgCall, reason: str) -> None:
        async with u.lock:
            if u.tg is not tg_call:
                return
            u.tg = None
            self._stop_bridge(u, u.active.peer if u.active else "")
            can_reconnect = (reason in RECONNECT_REASONS and self.calls.reconnect_timeout > 0 and u.active is not None
                             and u.active.call.state == CallState.CONNECTED and u.reconnect_task is None)
        if can_reconnect:
            u.reconnect_task = self.loop.create_task(self._reconnect(u))
            return
        if reason == "conference failed":
            await self._notify(u, u.t("tg_conference_failed"))
        await self._end_everything(u, f"telegram {reason}")

    async def _reconnect(self, u: UserState) -> None:
        leg = u.active
        assert leg is not None
        await leg.call.hold()
        await self._notify(u, u.t("reconnecting"))
        u.tg_pending = True
        try:
            tg_call = await u.gw.engine.call(u.input_user, sample_rate=leg.call.rate, ring_timeout=self.calls.reconnect_timeout)
        except TgCallError as e:
            log.info("reconnect to %s failed: %s", u.id, e)
            u.tg_pending = False
            u.reconnect_task = None
            await self._notify(u, u.t("reconnect_failed"))
            await self._end_everything(u, "reconnect failed", notify=False)
            return
        async with u.lock:
            u.tg_pending = False
            u.reconnect_task = None
            if u.active is not leg or not leg.call.active:
                await tg_call.hangup("hangup")
                await self._end_everything(u, "far end left during reconnect")
                return
            u.tg = tg_call
            self._watch_tg(u, tg_call)
            await leg.call.unhold()
            self._attach(u, leg)
        await self._notify(u, u.t("reconnected"))
        await self._update_card(u)

    async def _end_everything(self, u: UserState, reason: str, notify: bool = True) -> None:
        async with u.lock:
            self._stop_bridge(u, u.active.peer if u.active else "")
            legs = u.legs()
            u.active = u.held = u.waiting = None
            if u.waiting_timer:
                u.waiting_timer.cancel()
                u.waiting_timer = None
            tg = u.tg
            u.tg = None
            if u.reconnect_task:
                u.reconnect_task.cancel()
                u.reconnect_task = None
            if u.tg_pending:
                await u.gw.engine.cancel(u.id, "missed")
                u.tg_pending = False
        summaries: list[tuple[str, Buttons]] = []
        for leg in legs:
            if leg.call.active:
                if leg.direction == "in" and leg.call.state in (CallState.NEW, CallState.RINGING):
                    leg.result = "declined"
                    leg.call.reject(603)
                else:
                    await leg.call.hangup()
            if notify and leg.call.connected_at and leg.result in ("", "answered"):
                summaries.append((u.t("call_ended", peer=leg.peer, duration=fmt_duration(time.time() - leg.call.connected_at)),
                                  self._after_call_buttons(leg)))
                leg.result = "answered"
        if tg is not None and tg.active:
            await tg.hangup("hangup")
        log.info("user %s: all calls ended (%s)", u.id, reason)
        for text, buttons in summaries[:-1]:
            await self._notify(u, text, buttons)
        await self._update_card(u, summaries[-1] if summaries else None)

    # ---- commands (text from the account chat, text or buttons from the bot) ----

    async def _on_account_message(self, sender_id: int, text: str, event) -> None:
        await self.handle_text(sender_id, text)

    def _sip_text(self, rt: AccountRuntime, sender: str, text: str) -> None:
        for u in rt.owners[:1]:
            self.loop.create_task(self._notify(u, f"✉️ SIP {sender}: {text}"))

    async def handle_text(self, uid: int, text: str) -> None:
        u = self.users.get(uid)
        if u is None:
            if uid not in self._unknown_warned:
                self._unknown_warned.add(uid)
                log.info("message from unknown telegram user %s ignored", uid)
            return
        raw = (text or "").strip()
        if not raw:
            return
        low = raw.lower()
        cmd, _, arg = low.partition(" ")
        arg = raw[len(cmd):].strip()
        try:
            await self._dispatch(u, raw, cmd, arg)
        except Exception:
            log.exception("command %r from %s failed", raw, uid)

    async def _dispatch(self, u: UserState, raw: str, cmd: str, arg: str) -> None:
        in_call = u.active is not None and u.active.call.state == CallState.CONNECTED
        if cmd.startswith("dtmf:"):
            await self._dtmf(u, cmd[5:], quiet=True)   # keypad button: the bot already acknowledges the tap
        elif cmd in ("/start", "/help", "help", "помощь"):
            await self._notify(u, u.t("help"))
        elif cmd in ("/status", "status", "статус"):
            await self._notify(u, self._status_text(u))
        elif cmd in ("/history", "/log", "история"):
            await self._notify(u, self._history_text(u))
        elif cmd in ("/hangup", "/h", "/stop", "/end", "сброс", "hangup"):
            await self._hangup(u)
        elif cmd in ("/switch", "/s", "/answer"):
            await self._switch(u)
        elif cmd in ("/decline", "/reject"):
            await self._decline(u)
        elif cmd == "/decline-incoming":
            if u.tg_pending:
                await u.gw.engine.cancel(u.id, "missed")
        elif cmd in ("/transfer", "/tr", "/xfer"):
            await self._transfer(u, arg)
        elif cmd in ("/dnd", "/mute-calls"):
            u.dnd = not u.dnd if arg.lower() not in ("on", "off") else arg.lower() == "on"
            await self._notify(u, u.t("dnd_on" if u.dnd else "dnd_off"), [[(u.t("btn_dnd_off" if u.dnd else "btn_dnd_on"), "/dnd")]])
        elif cmd in ("/cb", "/callback"):
            number = self.history.last_caller(u.id)
            if number:
                await self._outgoing_from_text(u, number)
            else:
                await self._notify(u, u.t("history_empty"))
        elif cmd in ("/redial", "/r"):
            number = self.history.last_dialed(u.id)
            if number:
                await self._outgoing_from_text(u, number)
            else:
                await self._notify(u, u.t("history_empty"))
        elif cmd in ("/rec", "/record", "/запись"):
            await self._record(u)
        elif cmd in ("/conf", "/conference", "/конференция"):
            await self._conference(u, arg)
        elif cmd in ("/group", "/gc", "/групповой"):
            await self._group(u, arg)
        elif cmd in ("/schedule", "/sched", "/расписание"):
            await self._notify(u, self._schedule_text(u))
        elif cmd in ("/lang", "/language", "/язык"):
            await self._lang(u, arg)
        elif cmd == "/line":
            await self._line(u, arg)
        elif cmd == "/dtmf":
            await self._dtmf(u, arg)
        elif in_call and DTMF_RE.match(raw):
            await self._dtmf(u, raw)
        elif cmd in ("/call", "call", "позвони", "набери"):
            await self._outgoing_from_text(u, arg)
        elif NUMBER_RE.match(raw) or raw.startswith("sip:"):
            await self._outgoing_from_text(u, raw)
        else:
            await self._notify(u, u.t("unknown_command"))

    async def _dtmf(self, u: UserState, digits: str, quiet: bool = False) -> None:
        """Sends digits to the PBX. The Telegram user hears no side tone, so typed digits are echoed back."""
        digits = re.sub(r"[^0-9*#A-Da-d]", "", digits)
        a = u.active
        if not (a and a.call.state == CallState.CONNECTED):
            await self._notify(u, u.t("no_active_call"))
            return
        if not digits:
            return
        if u.bridge is not None:
            u.bridge.side_tone(tones.dtmf_pcm(digits, u.bridge.rate))
        await a.call.send_dtmf(digits)
        if not quiet:
            await self._notify(u, u.t("dtmf_sent", digits=digits))

    async def _outgoing_from_text(self, u: UserState, raw: str) -> None:
        rt: AccountRuntime | None = None
        if ":" in raw and not raw.startswith("sip:"):
            prefix, _, rest = raw.partition(":")
            for cand in u.accounts:
                if cand.name.lower() == prefix.strip().lower() or cand.cfg.username == prefix.strip():
                    rt, raw = cand, rest.strip()
                    break
        number = raw.strip() if raw.startswith("sip:") else self.apply_dial_rules(raw)
        if not number or (not number.startswith("sip:") and not re.fullmatch(r"\+?[0-9*#]{1,32}", number)):
            await self._notify(u, u.t("not_a_number", text=raw))
            return
        u.pending_number = (number, time.time())
        await self._dial(u, number, rt)

    async def dial_from_api(self, u: UserState, number: str, rt: AccountRuntime | None = None) -> None:
        """Click-to-call: same path as a number sent in the chat."""
        await self._outgoing_from_text(u, f"{rt.name}: {number}" if rt else number)

    async def hangup_user(self, u: UserState) -> None:
        await self._hangup(u)

    async def _hangup(self, u: UserState) -> None:
        if u.active is not None and u.active.call.active:
            await u.active.call.hangup()
            return
        if u.tg is not None or u.tg_pending:
            await self._end_everything(u, "hangup from chat")
            return
        session = self.group_calls.get(u.gw.name) if u.gw else None
        mine = [leg for leg in session.legs if leg.user is u] if session else []
        if mine:
            for leg in mine:
                if leg.call.active:
                    await leg.call.hangup()
            return
        await self._notify(u, u.t("no_active_call"))

    async def _switch(self, u: UserState) -> None:
        async with u.lock:
            if u.waiting is not None and u.tg is not None:
                w = u.waiting
                u.waiting = None
                if u.waiting_timer:
                    u.waiting_timer.cancel()
                    u.waiting_timer = None
                if u.active is not None:
                    self._stop_bridge(u, u.active.peer)
                    await u.active.call.hold()
                    u.held = u.active
                    u.active = None
                w.call.answer()
                self._attach(u, w)
                await self._notify(u, u.t("switched", peer=w.peer, held=u.held.peer if u.held else "-"))
            elif u.held is not None and u.tg is not None:
                a, h = u.active, u.held
                if a is not None:
                    self._stop_bridge(u, a.peer)
                    await a.call.hold()
                await h.call.unhold()
                u.held = a
                self._attach(u, h)
                await self._notify(u, u.t("switched", peer=h.peer, held=a.peer if a else "-"))
            else:
                await self._notify(u, u.t("nothing_to_switch"))
        await self._update_card(u)

    async def _decline(self, u: UserState) -> None:
        w = u.waiting
        if w is None:
            if u.tg_pending:
                await u.gw.engine.cancel(u.id, "missed")
                return
            await self._notify(u, u.t("no_waiting"))
            return
        w.result = "declined"
        w.call.reject(603)
        await self._notify(u, u.t("declined", caller=w.peer))

    async def _transfer(self, u: UserState, arg: str) -> None:
        if arg:
            number = arg if arg.startswith("sip:") else self.apply_dial_rules(arg)
            a = u.active
            if a is None or a.call.state != CallState.CONNECTED:
                await self._notify(u, u.t("no_active_call"))
                return
            await self._notify(u, u.t("transfer_started", peer=a.peer, number=fmt_number(number)))
            a.result = "transferred"
            ok, detail = await a.call.refer(number)
            if not ok:
                a.result = ""
                await self._notify(u, u.t("transfer_failed", reason=detail))
            return
        async with u.lock:
            a, h = u.active, u.held
            if a is None or h is None or a.call.state != CallState.CONNECTED:
                await self._notify(u, u.t("transfer_usage"))
                return
            self._stop_bridge(u, a.peer)
            u.active = u.held = None
            a.result = h.result = "transferred"
            tg = u.tg
            u.tg = None
        await h.call.unhold()
        bridge = SipSipBridge(a.call, h.call)
        bridge.start()
        entry = (bridge, a.call, h.call)
        self.sip_bridges.append(entry)
        for c in (a.call, h.call):
            c.on_state = None

        async def watch() -> None:
            await asyncio.wait([a.call.ended, h.call.ended], return_when=asyncio.FIRST_COMPLETED)
            bridge.stop()
            for c in (a.call, h.call):
                if c.active:
                    await c.hangup()
            if entry in self.sip_bridges:
                self.sip_bridges.remove(entry)

        self.loop.create_task(watch())
        if tg is not None:
            await tg.hangup("hangup")
        await self._notify(u, u.t("transfer_done", a=a.peer, b=h.peer))
        await self._update_card(u)

    async def _record(self, u: UserState) -> None:
        if self.calls.record == "off":
            await self._notify(u, u.t("rec_off"))
            return
        if u.bridge is None or u.active is None:
            await self._notify(u, u.t("no_active_call"))
            return
        if u.bridge.recording:
            self._stop_bridge_recording(u)
            await self._notify(u, u.t("rec_stopped"))
        else:
            try:
                u.bridge.start_recording(self.calls.record_max_minutes * 60)
            except RecordingUnavailable as e:
                await self._notify(u, u.t("rec_failed", reason=str(e)))
                return
            await self._notify(u, u.t("rec_started"))
        await self._update_card(u)

    def _stop_bridge_recording(self, u: UserState) -> None:
        """Stops recording without touching the call itself."""
        if u.bridge is None:
            return
        recorder = u.bridge.stop_recording()
        if recorder is not None:
            peer = u.active.peer if u.active else ""
            self.loop.create_task(self._send_recording(u, recorder, peer))

    async def _conference(self, u: UserState, arg: str) -> None:
        """Moves the current call (and the held one) into a ConfBridge room on the PBX and joins it."""
        room = self.calls.conference_extension
        if not room:
            await self._notify(u, u.t("conf_no_extension"))
            return
        a, h = u.active, u.held
        if a is None or a.call.state != CallState.CONNECTED:
            await self._notify(u, u.t("no_active_call"))
            return
        rt = a.account
        await self._notify(u, u.t("conf_started", room=room))
        moved: list[str] = []
        for leg in (h, a):
            if leg is None:
                continue
            ok, detail = await leg.call.refer(room)
            if not ok:
                await self._notify(u, u.t("conf_failed", reason=detail))
                return
            moved.append(leg.peer)
        async with u.lock:
            self._stop_bridge(u, a.peer)
            u.active = u.held = None
            for leg in (h, a):
                if leg is not None:
                    leg.result = "transferred"
        for peer in moved:
            await self._notify(u, u.t("conf_joined", peer=peer))
        if arg:
            u.pending_number = (self.apply_dial_rules(arg), time.time())
        await self._dial_leg(u, rt, room, consult=False)
        if arg:
            log.info("user %s: invite %s into conference room %s", u.id, arg, room)
            try:
                extra = await rt.sip.invite(self.apply_dial_rules(arg), headers={"X-TG-User-Id": str(u.id)})
                await asyncio.wait_for(extra.answered, self.calls.outgoing_ring_timeout)
                ok, detail = await extra.refer(room)
                if not ok:
                    await self._notify(u, u.t("conf_failed", reason=detail))
                    await extra.hangup()
            except Exception as e:
                await self._notify(u, u.t("conf_failed", reason=str(e)))

    def _group_of(self, leg: Leg) -> GroupSession | None:
        for session in self.group_calls.values():
            if leg in session.legs:
                return session
        return None

    async def _leave_group(self, session: GroupSession) -> None:
        session.bridge.stop()
        for leg in list(session.legs):
            if leg.call.active:
                await leg.call.hangup()
        session.legs.clear()
        self.group_calls.pop(session.gateway, None)
        await session.call.leave()

    def _group_text(self, u: UserState, session: GroupSession, key: str, **kw) -> str:
        if session.call.conference:
            return u.t(f"{key}_conference", **kw)
        return u.t(key, chat=session.call.title, **kw)

    async def _group(self, u: UserState, arg: str) -> None:
        """Moves the conversation into a Telegram group call: the voice chat of the configured group, or a
        conference call when no group is configured. Anyone invited can join, including people who have
        no extension on the PBX."""
        if u.gw is None:
            await self._notify(u, u.t("no_line"))
            return
        arg = arg.strip()
        session = self.group_calls.get(u.gw.name)
        if arg.lower() in ("stop", "off", "стоп", "выход"):
            if session is None:
                await self._notify(u, u.t("group_none"))
                return
            await self._leave_group(session)
            await self._notify(u, u.t("group_ended"))
            return
        if session is None:
            chat = self.calls.group_chat
            try:
                if chat:
                    await self._notify(u, u.t("group_joining", chat=chat))
                    call = await u.gw.engine.join_group(chat, self.calls.group_title)
                else:
                    await self._notify(u, u.t("conf_tg_starting"))
                    call = await u.gw.engine.create_conference(self.calls.group_title)
            except Exception as e:
                await self._notify(u, u.t("group_failed", reason=str(e)))
                return
            bridge = GroupBridge(call, self.calls.jitter_ms)
            bridge.start()
            session = GroupSession(call, bridge, u.gw.name)
            self.group_calls[u.gw.name] = session
            self.loop.create_task(self._watch_group(session))
        moved = [leg.peer for leg in await self._move_to_group(u, session)]
        if moved:
            await self._notify(u, self._group_text(u, session, "group_moved", peers=", ".join(moved)))
        await self._invite_to_group(u, session, arg)
        await self._update_card(u)

    async def _move_to_group(self, u: UserState, session: GroupSession) -> list[Leg]:
        """Takes the user's own legs out of their Telegram call and hands them to the voice chat."""
        moved: list[Leg] = []
        async with u.lock:
            for leg in (u.held, u.active):
                if leg is None or not leg.call.active:
                    continue
                if u.active is leg:
                    self._stop_bridge(u, leg.peer)
                    u.active = None
                else:
                    u.held = None
                moved.append(leg)
        for leg in moved:
            if leg.call.local_hold:
                await leg.call.unhold()
            session.legs.append(leg)
            session.bridge.add(leg.call)
            self._watch_leg(leg)
        if moved and u.tg is not None:
            await self._end_everything(u, "moved into the voice chat", notify=False)
        return moved

    async def _invite_to_group(self, u: UserState, session: GroupSession, arg: str) -> None:
        """The user always gets an invitation; an argument invites one more Telegram user or dials a number."""
        targets = [(u.input_user, u.name)]
        number = ""
        if arg:
            spec = "@" + arg[3:] if arg.lower().startswith("tg#") else arg
            if spec.startswith(("@", "+")):
                try:
                    targets.append((await u.gw.account.resolve_user(spec), spec))
                except Exception as e:
                    await self._notify(u, u.t("group_invite_failed", who=spec, reason=str(e)))
            else:
                number = self.apply_dial_rules(arg)
        failed = await session.call.invite([user for user, _ in targets])
        invited = [name for user, name in targets if str(user.user_id) not in failed]
        if invited:
            await self._notify(u, self._group_text(u, session, "group_invited", who=", ".join(invited)))
        else:
            await self._notify(u, u.t("group_invite_failed", who=", ".join(name for _, name in targets),
                                      reason="telegram"))
        if number:
            await self._dial_into_group(u, session, number)

    async def _dial_into_group(self, u: UserState, session: GroupSession, number: str) -> None:
        rt = self._outgoing_account(u)
        if rt is None:
            await self._notify(u, u.t("no_line"))
            return
        try:
            call = await rt.sip.invite(number, headers={"X-TG-User-Id": str(u.id)})
            await asyncio.wait_for(call.answered, self.calls.outgoing_ring_timeout)
        except Exception as e:
            await self._notify(u, u.t("group_dial_failed", number=fmt_number(number), reason=str(e)))
            return
        leg = Leg(call, rt, "out", fmt_number(number), number, u)
        call.tag = leg
        session.legs.append(leg)
        session.bridge.add(call)
        self._watch_leg(leg)
        await self._notify(u, self._group_text(u, session, "group_moved", peers=fmt_number(number)))

    async def _watch_group(self, session: GroupSession) -> None:
        await session.call.ended
        if self.group_calls.get(session.gateway) is session:
            session.bridge.stop()
            for leg in list(session.legs):
                if leg.call.active:
                    await leg.call.hangup()
            session.legs.clear()
            self.group_calls.pop(session.gateway, None)
            log.info("voice chat %s ended", session.call.title)

    async def _lang(self, u: UserState, arg: str) -> None:
        choice = arg.strip().lower()
        if not choice:
            await self._notify(u, u.t("lang_current"))
            return
        if choice not in LANGUAGES:
            await self._notify(u, u.t("lang_unknown"))
            return
        u.set_lang(choice)
        await self._notify(u, u.t("lang_switched"))
        await self._update_card(u)

    async def _line(self, u: UserState, arg: str) -> None:
        if not u.accounts:
            await self._notify(u, u.t("no_line"))
            return
        if arg:
            for rt in u.accounts:
                if rt.name.lower() == arg.lower() or rt.cfg.username == arg:
                    u.default_account = rt
                    await self._notify(u, u.t("line_current", account=rt.name))
                    return
            await self._notify(u, u.t("line_unknown", name=arg))
            return
        names = ", ".join(f"{rt.name}{' ✓' if rt is u.default_account else ''}" for rt in u.accounts)
        await self._notify(u, u.t("line_list", lines=names))

    # ---- texts ----

    def _status_text(self, u: UserState) -> str:
        me = u.gw.account.me if u.gw else None
        gw = (f"@{me.username}" if me and me.username else (me.first_name if me else "?"))
        lines = [u.t("status_header", gateway=gw)]
        for rt in (u.accounts or self.shared):
            state = u.t("registered") if rt.sip.registered else u.t("unregistered")
            lines.append(u.t("status_account", icon="🟢" if rt.sip.registered else "🔴", name=rt.name,
                           username=rt.cfg.username, domain=rt.cfg.domain, state=state))
        if u.dnd:
            lines.append(u.t("status_dnd"))
        if time_closed(u.rules):
            lines.append(u.t("status_filtered"))
        if u.active:
            c = u.active.call
            dur = fmt_duration(time.time() - c.connected_at) if c.connected_at else c.state.value
            direction = u.t("dir_in" if u.active.direction == "in" else "dir_out")
            lines.append(u.t("status_active", peer=u.active.peer, direction=direction, duration=dur))
        if u.held:
            lines.append(u.t("status_held", peer=u.held.peer))
        if u.waiting:
            lines.append(u.t("status_waiting", caller=u.waiting.peer))
        session = self.group_calls.get(u.gw.name) if u.gw else None
        if session is not None:
            lines.append(self._group_text(u, session, "status_group", n=len(session.legs)))
        if not u.legs():
            lines.append(u.t("status_idle"))
        return "\n".join(lines)

    def _schedule_text(self, u: UserState) -> str:
        r = u.rules
        if r.empty:
            return u.t("schedule_none")
        lines = [u.t("schedule_header")]
        if not r.work.empty:
            lines.append(u.t("schedule_work", windows=str(r.work)))
        if not r.quiet.empty:
            lines.append(u.t("schedule_quiet", windows=str(r.quiet)))
        if r.blacklist:
            lines.append(u.t("schedule_blacklist", numbers=", ".join(r.blacklist)))
        if r.whitelist:
            lines.append(u.t("schedule_whitelist", numbers=", ".join(r.whitelist)))
        lines.append(u.t("schedule_forward", number=fmt_number(r.forward)) if r.forward
                     else u.t("schedule_action", action=u.t(f"action_{r.action}")))
        lines.append(u.t("schedule_closed") if time_closed(r) else u.t("schedule_open"))
        return "\n".join(lines)

    def _history_text(self, u: UserState) -> str:
        items = self.history.last(u.id, 10)
        if not items:
            return u.t("history_empty")
        lines = [u.t("history_header")]
        for r in items:
            arrow = "⬅️" if r.direction == "in" else "➡️"
            res = t(f"res_{r.result}", u.lang) if r.result in RESULTS else r.result
            dur = f" {fmt_duration(r.duration)}" if r.duration else ""
            lines.append(f"{arrow} {fmt_time(r.ts)} `{fmt_number(r.peer)}`: {res}{dur}")
        return "\n".join(lines)
