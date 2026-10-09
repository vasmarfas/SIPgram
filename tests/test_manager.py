"""Call manager with fake SIP and Telegram legs (no network)."""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest
from telethon.tl import types

from sipgram.bridge import GroupBridge
from sipgram.config import (
    AccountConfig,
    ApiConfig,
    CallsConfig,
    Config,
    GatewayConfig,
    ScheduleConfig,
    TelegramAppConfig,
    UserConfig,
)
from sipgram.history import CallHistory
from sipgram.manager import AccountRuntime, CallManager, GatewayRuntime, UserState
from sipgram.messages import set_language
from sipgram.prefs import UserPrefs
from sipgram.sip.account import CallError, CallState
from sipgram.tg.calls import TgCallError, TgCallState

John = 111111
PETYA = 222222
STRANGER = 333333

set_language("ru")


class FakeSipCall:
    def __init__(self, loop, incoming: bool, dest: str = "", caller: str = "100"):
        self.incoming = incoming
        self.state = CallState.NEW
        self.answered = loop.create_future()
        self.ended = loop.create_future()
        self.on_state = None
        self.on_audio = None
        self.on_dtmf = None
        self.caller_number = caller
        self.caller_name = "PBX Test"
        self.dialed = "491"
        self.dest = dest
        self.rate = 8000
        self.connected_at = 0.0
        self.end_reason = ""
        self.end_code = 0
        self.rejected: int | None = None
        self.dtmf: list[str] = []
        self.ring_sent = False
        self.cancelled = False
        self.local_hold = False
        self.holds: list[str] = []
        self.refers: list[str] = []
        self.tag = None
        self.redirected = ""
        self.received = bytearray()
        self.headers: dict = {}
        self.call_id = f"fake{id(self):x}"

    @property
    def active(self):
        return self.state != CallState.TERMINATED

    def _set(self, st):
        self.state = st
        if st == CallState.CONNECTED and not self.connected_at:
            self.connected_at = time.time()
        if self.on_state:
            self.on_state(self, st)

    def ringing(self):
        self.ring_sent = True
        self._set(CallState.RINGING)

    def answer(self):
        self._set(CallState.CONNECTED)

    def reject(self, code=486, reason=""):
        self.rejected = code
        self.terminate(code, reason or "rejected")

    def terminate(self, code=0, reason="remote hangup"):
        if not self.active:
            return
        self.end_code, self.end_reason = code, reason
        if not self.answered.done():
            self.answered.set_exception(CallError(code or 487, reason))
            self.answered.exception()
        self._set(CallState.TERMINATED)
        self.ended.set_result(code)

    def remote_answer(self):
        self._set(CallState.CONNECTED)
        self.answered.set_result(True)

    async def hangup(self, code=0, reason=""):
        self.terminate(0, reason or "local hangup")

    async def hold(self):
        self.holds.append("hold")
        self.local_hold = True
        return True

    async def unhold(self):
        self.holds.append("unhold")
        self.local_hold = False
        return True

    async def refer(self, target, timeout=15.0):
        self.refers.append(target)
        return True, "202"

    async def send_dtmf(self, digits):
        self.dtmf.append(digits)

    def redirect(self, target):
        self.redirected = target
        self.terminate(302, "moved temporarily")

    def send_pcm(self, pcm):
        self.received.extend(pcm)

    def send_silence(self, n):
        pass


class FakeSipAccount:
    def __init__(self, loop, cfg):
        self.loop = loop
        self.cfg = cfg
        self.registered = True
        self.invites: list[FakeSipCall] = []
        self.on_incoming_call = None

    async def invite(self, dest, headers=None, display_name=None):
        call = FakeSipCall(self.loop, incoming=False, dest=dest)
        call.headers = headers or {}
        call._set(CallState.CALLING)
        self.invites.append(call)
        return call

    async def stop(self):
        pass


class FakeTgCall:
    def __init__(self, loop, uid, outgoing, rate=8000):
        self.user_id = uid
        self.outgoing = outgoing
        self.sample_rate = rate
        self.state = TgCallState.NEW
        self.ended = loop.create_future()
        self.on_audio = None
        self.on_state = None
        self.frames_in = self.frames_out = 0
        self.end_reason = ""
        self.hangups: list[str] = []
        self.accepted = False
        self.sent = bytearray()
        self.conference = None
        self.invite_msg_id = None
        self.emojis = ""
        self.others = 1
        self.on_update = None

    @property
    def active(self):
        return self.state != TgCallState.ENDED

    def send_audio(self, frame):
        self.frames_out += 1
        self.sent.extend(frame)

    async def set_sample_rate(self, rate):
        self.sample_rate = rate

    def end(self, reason="hangup"):
        if not self.active:
            return
        self.end_reason = reason
        self.state = TgCallState.ENDED
        self.ended.set_result(reason)

    async def hangup(self, reason="hangup"):
        self.hangups.append(reason)
        self.end(reason)

    async def accept(self):
        self.accepted = True
        self.state = TgCallState.CONNECTED


class FakeGroupCall:
    def __init__(self, loop, title, conference=False):
        self.title = title
        self.conference = conference
        self.sample_rate = 48000
        self.ended = loop.create_future()
        self.on_audio = None
        self.invited: list[int] = []
        self.left = ""
        self.sent = bytearray()
        self.frames_in = self.frames_out = 0

    @property
    def active(self):
        return not self.ended.done()

    def send_audio(self, frame):
        self.frames_out += 1
        self.sent.extend(frame)

    async def invite(self, users):
        self.invited.extend(u.user_id for u in users)
        return []

    async def leave(self, reason="hangup"):
        self.left = reason
        if not self.ended.done():
            self.ended.set_result(reason)


class FakeEngine:
    def __init__(self, loop):
        self.loop = loop
        self.calls: dict[int, FakeTgCall] = {}
        self.groups: dict[str, FakeGroupCall] = {}
        self.library_versions = ["8.0.0", "9.0.0"]
        self.on_incoming = None
        self.behaviour: dict[int, str] = {}
        self.cancelled: list[tuple[int, str]] = []
        self.pending: dict[int, asyncio.Future] = {}
        self.placed: list[int] = []
        self.default_rate = 8000

    async def call(self, input_user, sample_rate=0, ring_timeout=45.0):
        uid = input_user.user_id
        self.placed.append(uid)
        mode = self.behaviour.get(uid, "answer")
        if mode == "busy":
            raise TgCallError("busy", 486)
        if mode == "privacy":
            raise TgCallError("user's privacy settings do not allow calls from the gateway account", 403)
        call = FakeTgCall(self.loop, uid, True, sample_rate or 8000)
        self.calls[uid] = call
        if mode == "wait":
            fut = self.loop.create_future()
            self.pending[uid] = fut
            try:
                await fut
            except TgCallError:
                self.calls.pop(uid, None)
                raise
        call.state = TgCallState.CONNECTED
        return call

    async def join_group(self, chat, title=""):
        group = self.groups.get(chat)
        if group is None or not group.active:
            group = self.groups[chat] = FakeGroupCall(self.loop, title or chat)
        return group

    async def create_conference(self, title=""):
        group = self.groups["conference"] = FakeGroupCall(self.loop, title or "Telegram", conference=True)
        return group

    async def cancel(self, uid, reason="missed"):
        self.cancelled.append((uid, reason))
        fut = self.pending.pop(uid, None)
        if fut and not fut.done():
            fut.set_exception(TgCallError(reason))
        self.calls.pop(uid, None)

    def release(self, uid):
        fut = self.pending.pop(uid, None)
        if fut and not fut.done():
            fut.set_result(True)


class FakeAccount:
    def __init__(self):
        self.texts: list[tuple[int, str]] = []
        self.me = None

    async def send_text(self, uid, text):
        self.texts.append((uid, text))

    async def resolve_user(self, spec):
        s = str(spec)
        if s == "@petya":
            return types.InputUser(user_id=PETYA, access_hash=1)
        return types.InputUser(user_id=int(s.lstrip("+")), access_hash=1)

    async def ensure_contact(self, user, name=""):
        pass

    async def disconnect(self):
        pass


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []
        self.cards: list[tuple[int, str]] = []
        self.voices: list[tuple[int, bytes, int, str]] = []
        self.cleared: list[int] = []

    async def send(self, uid, text, buttons=None):
        self.sent.append((uid, text, buttons))

    async def card(self, uid, text, buttons, keypad_ok):
        self.cards.append((uid, text))
        return False

    async def send_voice(self, uid, ogg, duration, caption=""):
        self.voices.append((uid, ogg, duration, caption))
        return True

    async def close_card(self, uid, text, buttons=None):
        self.cleared.append(uid)
        return False

    def texts(self, uid=None):
        return [t for u, t, _ in self.sent if uid is None or u == uid]


class BotNotifier(FakeNotifier):
    """Users who started the bot: the call card is one message edited in place."""

    def __init__(self):
        super().__init__()
        self.card_text: dict[int, str] = {}
        self.closed: dict[int, list[tuple[str, object]]] = {}

    async def card(self, uid, text, buttons, keypad_ok):
        self.cards.append((uid, text))
        self.card_text[uid] = text
        return True

    async def close_card(self, uid, text, buttons=None):
        if self.card_text.pop(uid, None) is None:
            return False
        self.closed.setdefault(uid, []).append((text, buttons))
        return True


def make_manager(loop, *, ring_all=False, shared=False, max_calls=10, call_waiting=True, mode="callback",
                 default_destination="", reconnect=60, prefs=None, record="off", group_chat=""):
    users = [UserConfig(id=str(John), name="John"), UserConfig(id=str(PETYA), name="Petya")]
    acc_users = [str(John), str(PETYA)] if ring_all else [str(John)]
    accounts = [AccountConfig(server="pbx", username="491", password="x", users=acc_users, ring_all=ring_all,
                              ring_timeout=5, default_destination=default_destination)]
    if shared:
        accounts.append(AccountConfig(server="pbx", username="trunk", password="x", shared=True, local_port=5071))
    cfg = Config(telegram=TelegramAppConfig(api_id=1, api_hash="h", max_calls=max_calls), users=users, accounts=accounts,
                 calls=CallsConfig(outgoing_mode=mode, call_waiting=call_waiting, reconnect_timeout=reconnect,
                                   record=record, conference_extension="8000", group_chat=group_chat),
                 api=ApiConfig())
    m = CallManager.__new__(CallManager)
    m.cfg = cfg
    m.calls = cfg.calls
    gw = GatewayRuntime(GatewayConfig(name="main", session="s", max_calls=max_calls), FakeAccount(), FakeEngine(loop))
    m.gateways = {gw.name: gw}
    m.account = gw.account          # the tests reach for these directly
    m.engine = gw.engine
    m.notifier = FakeNotifier()
    m.history = CallHistory(None, 20)
    m.prefs = prefs or UserPrefs(None)
    m.alerts = None
    m.loop = loop
    m.users = {}
    m.accounts = []
    m.shared = []
    m.sip_bridges = []
    m.group_calls = {}
    m._unknown_warned = set()
    states = {}
    for ucfg in users:
        st = UserState(ucfg, types.InputUser(user_id=int(ucfg.id), access_hash=1), gw, m.prefs)
        m.users[st.id] = st
        states[ucfg.id] = st
    for acfg in accounts:
        rt = AccountRuntime(acfg, FakeSipAccount(loop, acfg))
        for spec in acfg.users:
            st = states[spec]
            rt.owners.append(st)
            st.accounts.append(rt)
            if st.default_account is None:
                st.default_account = rt
        if acfg.shared:
            m.shared.append(rt)
        m.accounts.append(rt)
    return m


async def settle(n: int = 3):
    for _ in range(n):
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_incoming_sip_to_telegram_and_hangup_from_pbx():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(rt, call)
    await settle()
    u = m.users[John]
    assert call.ring_sent and call.state == CallState.CONNECTED
    assert u.tg is not None and u.active is not None and u.bridge is not None
    assert any("Входящий" in t for t in m.notifier.texts(John))
    tg = u.tg
    call.terminate(0, "remote hangup")
    await settle()
    assert tg.hangups == ["hangup"] and u.tg is None and u.active is None
    assert any("завершён" in t for t in m.notifier.texts(John))
    hist = m.history.last(John)
    assert hist and hist[0].direction == "in" and hist[0].result == "answered"


@pytest.mark.asyncio
async def test_incoming_busy_user_rejects_486_and_missed_on_no_answer():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    m.engine.behaviour[John] = "busy"
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    assert call.rejected == 486
    m.engine.behaviour[John] = "wait"
    call2 = FakeSipCall(loop, incoming=True)
    task = asyncio.ensure_future(m._sip_incoming(m.accounts[0], call2))
    await settle()
    call2.terminate(487, "cancelled by caller")
    await asyncio.wait_for(task, 2)
    assert (John, "missed") in m.engine.cancelled
    assert any("Пропущенный" in t for t in m.notifier.texts(John))
    assert m.history.last(John)[0].result == "missed"


@pytest.mark.asyncio
async def test_call_waiting_switch_and_resume():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    first = FakeSipCall(loop, incoming=True, caller="100")
    await m._sip_incoming(rt, first)
    await settle()
    u = m.users[John]
    second = FakeSipCall(loop, incoming=True, caller="200")
    await m._sip_incoming(rt, second)
    await settle()
    assert u.waiting is not None and second.ring_sent and second.state == CallState.RINGING
    assert any("Второй входящий" in t for t in m.notifier.texts(John))
    await m.handle_text(John, "/switch")
    await settle()
    assert second.state == CallState.CONNECTED and u.active.call is second
    assert u.held.call is first and first.holds == ["hold"]
    await m.handle_text(John, "/switch")
    await settle()
    assert u.active.call is first and first.holds == ["hold", "unhold"] and second.holds == ["hold"]
    await m.handle_text(John, "/hangup")
    await settle()
    assert first.state == CallState.TERMINATED
    assert u.active.call is second and second.holds == ["hold", "unhold"]
    assert u.tg is not None and u.tg.active
    second.terminate()
    await settle()
    assert u.tg is None and u.active is None


@pytest.mark.asyncio
async def test_waiting_call_decline_and_timeout():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    first = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(rt, first)
    await settle()
    second = FakeSipCall(loop, incoming=True, caller="200")
    await m._sip_incoming(rt, second)
    await settle()
    await m.handle_text(John, "/decline")
    await settle()
    assert second.rejected == 603 and m.users[John].waiting is None
    rt.cfg.ring_timeout = 0.05
    third = FakeSipCall(loop, incoming=True, caller="300")
    await m._sip_incoming(rt, third)
    await asyncio.sleep(0.2)
    assert third.rejected == 480 and m.users[John].waiting is None
    assert any("не принят" in t for t in m.notifier.texts(John))


@pytest.mark.asyncio
async def test_call_waiting_disabled_gives_busy():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, call_waiting=False)
    rt = m.accounts[0]
    first = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(rt, first)
    await settle()
    second = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(rt, second)
    assert second.rejected == 486


@pytest.mark.asyncio
async def test_outgoing_callback_dtmf_and_hangup():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    await m.handle_text(John, "+7 (999) 123-45-67")
    await settle()
    sip = m.accounts[0].sip.invites[0]
    assert sip.dest == "79991234567" and sip.headers["X-TG-User-Id"] == str(John)
    u = m.users[John]
    assert u.tg is not None and u.bridge is not None and u.bridge.ringback is not None
    sip.remote_answer()
    await settle()
    await m.handle_text(John, "123#")
    assert sip.dtmf == ["123#"]
    await m.handle_text(John, "dtmf:5")
    assert sip.dtmf == ["123#", "5"]
    await m.handle_text(John, "/hangup")
    await settle()
    assert sip.state == CallState.TERMINATED and u.tg is None
    assert m.history.last_dialed(John) == "79991234567"


@pytest.mark.asyncio
async def test_feature_codes_dial_when_idle_and_are_dtmf_in_call():
    """*97 with no call is a number to dial; the same digits during a call go to the PBX as DTMF."""
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    await m.handle_text(John, "*97")
    await settle()
    assert m.accounts[0].sip.invites[0].dest == "*97"
    await m.handle_text(John, "/hangup")
    await settle()
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    await m.handle_text(John, "*100")
    assert call.dtmf == ["*100"] and len(m.accounts[0].sip.invites) == 1
    assert any("*100" in t for t in m.notifier.texts(John)), "typed digits are echoed back"
    bridge, tg = m.users[John].bridge, m.users[John].tg
    before = len(tg.sent)
    bridge._from_sip(bytes(bridge.rate // 50 * 2))     # 20 ms of silence from the PBX
    heard = np.frombuffer(bytes(tg.sent[before:]), dtype="<i2")
    assert len(heard) and np.abs(heard).max() > 1000, "keypad tone must be audible to the Telegram user"
    await m.handle_text(John, "dtmf:5")
    assert call.dtmf == ["*100", "5"]
    assert sum(1 for t in m.notifier.texts(John) if t.startswith("⌨️")) == 1, "keypad presses are not echoed"


@pytest.mark.asyncio
async def test_outgoing_busy_plays_tone_and_reports():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    await m.handle_text(John, "8 903 000 00 00")
    await settle()
    sip = m.accounts[0].sip.invites[0]
    assert sip.dest == "79030000000"
    tg = m.users[John].tg
    sip.terminate(486, "Busy Here")
    await asyncio.sleep(2.3)
    assert any("занято (486)" in t for t in m.notifier.texts(John)), "the SIP code comes with its meaning"
    assert tg.hangups == ["hangup"] and m.users[John].tg is None
    assert m.history.last(John)[0].result == "busy"


@pytest.mark.asyncio
async def test_consultation_call_and_attended_transfer():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    first = FakeSipCall(loop, incoming=True, caller="100")
    await m._sip_incoming(rt, first)
    await settle()
    u = m.users[John]
    await m.handle_text(John, "102")          # digits during a call are DTMF, not a new call
    assert first.dtmf == ["102"]
    await m.handle_text(John, "/call 102")
    await settle()
    assert first.holds == ["hold"] and u.held.call is first
    second = rt.sip.invites[0]
    assert second.dest == "102" and u.active.call is second
    second.remote_answer()
    await settle()
    tg = u.tg
    await m.handle_text(John, "/transfer")
    await settle()
    assert first.holds == ["hold", "unhold"]
    assert tg.hangups == ["hangup"] and u.tg is None and u.active is None and u.held is None
    assert len(m.sip_bridges) == 1 and first.on_audio is not None and second.on_audio is not None
    assert any("соединены" in t for t in m.notifier.texts(John))
    first.terminate()
    await settle()
    assert second.state == CallState.TERMINATED and not m.sip_bridges


@pytest.mark.asyncio
async def test_blind_transfer_uses_refer():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(rt, call)
    await settle()
    await m.handle_text(John, "/transfer 8 903 111 22 33")
    assert call.refers == ["79031112233"]


@pytest.mark.asyncio
async def test_reconnect_after_telegram_drop():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(rt, call)
    await settle()
    u = m.users[John]
    old_tg = u.tg
    m.engine.behaviour[John] = "wait"
    old_tg.end("disconnect")
    await settle()
    assert call.holds == ["hold"] and u.reconnect_task is not None
    assert any("прервалась" in t for t in m.notifier.texts(John))
    m.engine.release(John)
    await settle()
    assert u.tg is not None and u.tg is not old_tg and call.holds == ["hold", "unhold"]
    assert u.bridge is not None and call.state == CallState.CONNECTED
    u.tg.end("hangup")
    await settle()
    assert call.state == CallState.TERMINATED


@pytest.mark.asyncio
async def test_telegram_hangup_ends_all_legs():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    first = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(rt, first)
    await settle()
    second = FakeSipCall(loop, incoming=True, caller="200")
    await m._sip_incoming(rt, second)
    await settle()
    m.users[John].tg.end("hangup")
    await settle()
    assert first.state == CallState.TERMINATED and second.rejected == 603


@pytest.mark.asyncio
async def test_ring_all_first_answer_wins():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, ring_all=True)
    m.engine.behaviour[John] = "wait"
    m.engine.behaviour[PETYA] = "wait"
    call = FakeSipCall(loop, incoming=True)
    task = asyncio.ensure_future(m._sip_incoming(m.accounts[0], call))
    await settle()
    assert sorted(m.engine.placed) == [John, PETYA]
    m.engine.release(PETYA)
    await asyncio.wait_for(task, 2)
    await settle()
    assert m.users[PETYA].tg is not None and call.state == CallState.CONNECTED
    assert (John, "missed") in m.engine.cancelled and m.users[John].tg is None


@pytest.mark.asyncio
async def test_dnd_and_unknown_users():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    await m.handle_text(John, "/dnd")
    assert m.users[John].dnd
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    assert call.rejected == 486
    await m.handle_text(STRANGER, "+79990000000")
    assert not m.accounts[0].sip.invites
    tg = FakeTgCall(loop, STRANGER, outgoing=False)
    tg.state = TgCallState.INCOMING
    await m._tg_incoming(tg)
    assert tg.hangups == ["busy"]


@pytest.mark.asyncio
async def test_shared_account_routes_by_dialed_number():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, shared=True)
    trunk = m.shared[0]
    call = FakeSipCall(loop, incoming=True)
    call.dialed = str(PETYA)
    await m._sip_incoming(trunk, call)
    await settle()
    assert m.users[PETYA].tg is not None and call.state == CallState.CONNECTED
    unknown = FakeSipCall(loop, incoming=True)
    unknown.dialed = "tg#nobody"
    m.account.resolve_user = _fail_resolve
    await m._sip_incoming(trunk, unknown)
    assert unknown.rejected == 404


async def _fail_resolve(spec):
    raise ValueError("no such user")


@pytest.mark.asyncio
async def test_capacity_limit():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, ring_all=True, max_calls=1)
    m.engine.calls[999] = FakeTgCall(loop, 999, True)
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    assert call.rejected == 486


@pytest.mark.asyncio
async def test_user_calls_gateway_with_pending_number_and_default():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, default_destination="100")
    tg = FakeTgCall(loop, John, outgoing=False)
    tg.state = TgCallState.INCOMING
    await m._tg_incoming(tg)
    await settle()
    assert tg.accepted and m.accounts[0].sip.invites[0].dest == "100"
    tg.end("hangup")
    await settle()
    m.users[John].pending_number = ("74950000000", time.time())
    tg2 = FakeTgCall(loop, John, outgoing=False)
    tg2.state = TgCallState.INCOMING
    await m._tg_incoming(tg2)
    await settle()
    assert m.accounts[0].sip.invites[1].dest == "74950000000"


@pytest.mark.asyncio
async def test_callback_redial_status_history_help():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    call = FakeSipCall(loop, incoming=True, caller="79001112233")
    await m._sip_incoming(rt, call)
    await settle()
    call.terminate()
    await settle()
    await m.handle_text(John, "/cb")
    await settle()
    assert rt.sip.invites[-1].dest == "79001112233"
    await m.handle_text(John, "/hangup")
    await settle()
    await m.handle_text(John, "/redial")
    await settle()
    assert rt.sip.invites[-1].dest == "79001112233"
    await m.handle_text(John, "/hangup")
    await settle()
    await m.handle_text(John, "/status")
    await m.handle_text(John, "/history")
    await m.handle_text(John, "/help")
    await m.handle_text(John, "/line")
    texts = m.notifier.texts(John)
    assert any("491@pbx" in t for t in texts)
    assert any("Последние звонки" in t for t in texts)
    assert any("/switch" in t for t in texts)
    assert any("491 ✓" in t for t in texts)


@pytest.mark.asyncio
async def test_language_is_per_user_and_persisted(tmp_path):
    """/lang switches only the caller's language and survives a restart."""
    prefs = UserPrefs(tmp_path / "prefs.json")
    loop = asyncio.get_running_loop()
    m = make_manager(loop, ring_all=True, prefs=prefs)
    await m.handle_text(John, "/help")
    assert "Шлюз между вашей АТС" in m.notifier.texts(John)[-1]

    await m.handle_text(John, "/lang en")
    assert "English" in m.notifier.texts(John)[-1]
    await m.handle_text(John, "/help")
    assert "Gateway between your PBX" in m.notifier.texts(John)[-1]
    await m.handle_text(PETYA, "/help")
    assert "Шлюз между вашей АТС" in m.notifier.texts(PETYA)[-1], "the other user keeps his language"

    await m.handle_text(John, "/lang de")
    assert "ru" in m.notifier.texts(John)[-1] and "en" in m.notifier.texts(John)[-1]
    await m.handle_text(John, "/lang")
    assert "English" in m.notifier.texts(John)[-1]

    # notifications and buttons follow the user's own language
    m.engine.behaviour[PETYA] = "wait"          # so the call lands on John deterministically
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    en_texts = m.notifier.texts(John)
    assert any("Incoming" in t for t in en_texts) and any("Входящий" in t for t in m.notifier.texts(PETYA))
    label = m.card_for(John)[1][-1][0][0]
    assert "Hang up" in label
    call.terminate()
    await settle()

    restarted = make_manager(loop, prefs=UserPrefs(tmp_path / "prefs.json"))
    await restarted.handle_text(John, "/help")
    assert "Gateway between your PBX" in restarted.notifier.texts(John)[-1], "language was not persisted"


@pytest.mark.asyncio
async def test_dnd_is_persisted(tmp_path):
    prefs = UserPrefs(tmp_path / "prefs.json")
    loop = asyncio.get_running_loop()
    m = make_manager(loop, prefs=prefs)
    await m.handle_text(John, "/dnd")
    assert m.users[John].dnd
    restarted = make_manager(loop, prefs=UserPrefs(tmp_path / "prefs.json"))
    assert restarted.users[John].dnd, "do-not-disturb was not persisted"


@pytest.mark.asyncio
async def test_call_is_recorded_and_sent_as_a_voice_message():
    """calls.record: all, the mixed audio goes to the chat when the call ends, nothing is stored."""
    pytest.importorskip("sipgram.sip.opus")
    from sipgram.sip import opus
    if not opus.available():
        pytest.skip("libopus is not installed")
    loop = asyncio.get_running_loop()
    m = make_manager(loop, record="all")
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    u = m.users[John]
    assert u.bridge is not None and u.bridge.recording
    tone = np.sin(np.arange(8000) * 2 * np.pi * 440 / 8000) * 8000
    frame = tone.astype("<i2").tobytes()
    for i in range(0, len(frame), 320):
        u.bridge._from_sip(frame[i:i + 320])
        u.bridge._record_tick(frame[i:i + 320])
    call.terminate()
    await settle(6)
    assert m.notifier.voices, "no voice message was sent"
    uid, ogg, duration, caption = m.notifier.voices[-1]
    assert uid == John and ogg[:4] == b"OggS" and b"OpusHead" in ogg
    assert duration >= 1 and "Запись" in caption


@pytest.mark.asyncio
async def test_record_command_needs_recording_enabled():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, record="off")
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    await m.handle_text(John, "/rec")
    assert any("calls.record" in t for t in m.notifier.texts(John))
    assert not m.users[John].bridge.recording


@pytest.mark.asyncio
async def test_conference_moves_both_legs_into_the_room():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    rt = m.accounts[0]
    first = FakeSipCall(loop, incoming=True, caller="100")
    await m._sip_incoming(rt, first)
    await settle()
    await m.handle_text(John, "/call 102")
    await settle()
    second = rt.sip.invites[0]
    second.remote_answer()
    await settle()
    await m.handle_text(John, "/conf")
    await settle()
    assert first.refers == ["8000"] and second.refers == ["8000"]
    assert rt.sip.invites[-1].dest == "8000", "the gateway itself joins the room"
    assert any("8000" in t for t in m.notifier.texts(John))


@pytest.mark.asyncio
async def test_api_click_to_call_and_control():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    u = m.users[John]
    await m.dial_from_api(u, "8 903 000 00 00")
    await settle()
    sip = m.accounts[0].sip.invites[0]
    assert sip.dest == "79030000000"
    sip.remote_answer()
    await settle()
    await u.active.call.send_dtmf("55")
    assert sip.dtmf == ["55"]
    await m.hangup_user(u)
    await settle()
    assert sip.state == CallState.TERMINATED


# ---- schedule and lists ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_hours_answer_for_the_user_without_ringing_telegram():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    u = m.users[John]
    u.rules = ScheduleConfig(quiet_hours=["all 00:00-23:59"], action="busy")
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    assert call.rejected == 486 and not m.engine.placed, "the Telegram side is never touched"
    assert any("тихие часы" in t for t in m.notifier.texts(John))
    assert m.history.last(John)[0].result == "blocked"


@pytest.mark.asyncio
async def test_blacklisted_caller_is_forwarded_to_another_extension():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    m.users[John].rules = ScheduleConfig(blacklist=["7495*", "anonymous"], forward="*97")
    call = FakeSipCall(loop, incoming=True, caller="74951234567")
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    assert call.redirected == "*97" and call.rejected is None
    assert any("*97" in t for t in m.notifier.texts(John))


@pytest.mark.asyncio
async def test_hidden_number_matches_anonymous_and_a_whitelist_lets_the_rest_through():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    m.users[John].rules = ScheduleConfig(blacklist=["anonymous"])
    hidden = FakeSipCall(loop, incoming=True, caller="")
    await m._sip_incoming(m.accounts[0], hidden)
    await settle()
    assert hidden.rejected == 480, "the default action is 480, so follow-me on the PBX still runs"
    known = FakeSipCall(loop, incoming=True, caller="101")
    await m._sip_incoming(m.accounts[0], known)
    await settle()
    assert known.state == CallState.CONNECTED


@pytest.mark.asyncio
async def test_ring_all_rings_only_the_users_whose_schedule_is_open():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, ring_all=True)
    m.users[John].rules = ScheduleConfig(quiet_hours=["all 00:00-23:59"])
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    assert m.engine.placed == [PETYA] and call.state == CallState.CONNECTED
    assert m.users[PETYA].active is not None and m.users[John].active is None


@pytest.mark.asyncio
async def test_schedule_command_describes_the_rules():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    u = m.users[John]
    await m.handle_text(John, "/schedule")
    assert "не задано" in m.notifier.texts(John)[-1]
    u.rules = ScheduleConfig(work_hours=["mon-fri 09:00-18:00"], blacklist=["7495*"], forward="*97")
    await m.handle_text(John, "/schedule")
    text = m.notifier.texts(John)[-1]
    assert "mon,tue,wed,thu,fri 09:00-18:00" in text and "7495*" in text and "*97" in text
    u.rules = ScheduleConfig(quiet_hours=["all 23:00-07:00"], action="busy")
    await m.handle_text(John, "/schedule")
    assert "«занято» (486)" in m.notifier.texts(John)[-1]


# ---- Telegram voice chats -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_call_takes_the_leg_out_of_the_private_call():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, group_chat="@team")
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    u = m.users[John]
    tg = u.tg
    await m.handle_text(John, "/group")
    await settle()
    session = m.group_calls["main"]
    assert session.legs and session.legs[0].call is call
    assert u.active is None and u.tg is None and tg.hangups == ["hangup"], "the private call ends, the SIP leg lives on"
    assert session.call.invited == [John], "the user is invited into the voice chat"
    assert call.state == CallState.CONNECTED
    call.terminate(0, "remote hangup")
    await settle()
    assert "main" not in m.group_calls and session.call.left == "hangup"


@pytest.mark.asyncio
async def test_group_call_invites_another_telegram_user_and_stops_on_command():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, group_chat="@team")
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    await m.handle_text(John, "/group @petya")
    await settle()
    session = m.group_calls["main"]
    assert session.call.invited == [John, PETYA]
    await m.handle_text(John, "/group stop")
    await settle()
    assert "main" not in m.group_calls and not call.active


@pytest.mark.asyncio
async def test_hangup_ends_your_own_lines_in_the_voice_chat():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, group_chat="@team")
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    await m.handle_text(John, "/group")
    await settle()
    await m.handle_text(John, "/hangup")
    await settle()
    assert not call.active and "main" not in m.group_calls, "the last line leaving closes the voice chat"


@pytest.mark.asyncio
async def test_group_call_without_a_chat_starts_a_conference_call():
    """No group configured: /group starts a Telegram conference call instead of a group's voice chat."""
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    await m.handle_text(John, "/group @petya")
    await settle()
    session = m.group_calls["main"]
    assert session.call.conference and session.legs[0].call is call
    assert session.call.invited == [John, PETYA]
    texts = m.notifier.texts(John)
    assert any("групповом звонке Telegram" in t for t in texts) and not any("голосовом чате" in t for t in texts)
    await m.handle_text(John, "/status")
    assert "Групповой звонок Telegram: линий 1" in m.notifier.texts(John)[-1]


@pytest.mark.asyncio
async def test_group_bridge_gives_everyone_the_mix_without_their_own_voice():
    loop = asyncio.get_running_loop()
    group = FakeGroupCall(loop, "team")
    bridge = GroupBridge(group)
    legs = []
    for level in (100, 200):
        leg = FakeSipCall(loop, incoming=False)
        leg.rate = group.sample_rate
        leg._set(CallState.CONNECTED)
        bridge.add(leg)
        bridge._from_leg(leg, tone(level, group.sample_rate))
        legs.append(leg)
    bridge._from_group(1, tone(50, group.sample_rate))
    bridge._pump()
    assert samples(group.sent)[0] == 300, "the voice chat hears both legs"
    assert samples(legs[0].received)[0] == 250, "a leg hears the chat and the other leg, not itself"
    assert samples(legs[1].received)[0] == 150
    bridge.remove(legs[1])
    bridge._from_leg(legs[0], tone(100, group.sample_rate))
    bridge._from_group(1, tone(50, group.sample_rate))
    bridge._pump()
    assert samples(legs[0].received)[-1] == 50, "with one leg left only the voice chat is audible"
    bridge.stop()


def tone(level: int, rate: int) -> bytes:
    return np.full(rate // 50, level, dtype="<i2").tobytes()


def samples(buf: bytes):
    return np.frombuffer(bytes(buf), dtype="<i2")


# ---- the call card and what the user is told ---------------------------------------------------


@pytest.mark.asyncio
async def test_bot_keeps_one_message_per_call():
    """With the bot the incoming notice is the live card, and the outcome replaces it when the call ends."""
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    m.notifier = BotNotifier()
    m.engine.behaviour[John] = "wait"
    call = FakeSipCall(loop, incoming=True)
    task = asyncio.ensure_future(m._sip_incoming(m.accounts[0], call))
    await settle()
    assert "Входящий" in m.notifier.card_text[John] and not m.notifier.texts(John)
    m.engine.release(John)
    await asyncio.wait_for(task, 2)
    await settle()
    assert "Разговор с" in m.notifier.card_text[John]
    call.terminate()
    await settle()
    text, buttons = m.notifier.closed[John][-1]
    assert "завершён" in text and buttons == [[("📲 Перезвонить", "/cb")]]
    assert not m.notifier.texts(John), "no separate message for the end of the call"


@pytest.mark.asyncio
async def test_missed_call_turns_the_card_into_a_missed_notice():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    m.notifier = BotNotifier()
    m.engine.behaviour[John] = "wait"
    call = FakeSipCall(loop, incoming=True)
    task = asyncio.ensure_future(m._sip_incoming(m.accounts[0], call))
    await settle()
    call.terminate(487, "cancelled by caller")
    await asyncio.wait_for(task, 2)
    text, buttons = m.notifier.closed[John][-1]
    assert "Пропущенный" in text and buttons == [[("📲 Перезвонить", "/cb")]]
    assert len(m.history.last(John)) == 1, "one history entry per missed call"


@pytest.mark.asyncio
async def test_ring_all_tells_the_others_who_took_the_call():
    loop = asyncio.get_running_loop()
    m = make_manager(loop, ring_all=True)
    m.notifier = BotNotifier()
    m.engine.behaviour[John] = "wait"
    m.engine.behaviour[PETYA] = "wait"
    call = FakeSipCall(loop, incoming=True)
    task = asyncio.ensure_future(m._sip_incoming(m.accounts[0], call))
    await settle()
    m.engine.release(PETYA)
    await asyncio.wait_for(task, 2)
    await settle()
    assert "принял Petya" in m.notifier.closed[John][-1][0]
    assert "Разговор с" in m.notifier.card_text[PETYA]


@pytest.mark.asyncio
async def test_privacy_settings_that_block_the_gateway_are_explained():
    """The user cannot be reached in Telegram: say why, instead of a silent miss."""
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    m.engine.behaviour[John] = "privacy"
    await m.handle_text(John, "101")
    await settle()
    assert "конфиденциальности" in m.notifier.texts(John)[-1] and not m.accounts[0].sip.invites
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    missed = m.notifier.texts(John)[-1]
    assert "Пропущенный" in missed and "конфиденциальности" in missed and call.rejected == 480


@pytest.mark.asyncio
async def test_card_shows_the_conference_and_the_key():
    """A peer adding people in Telegram turns the call into a conference; the SIP leg stays in it."""
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    tg = m.users[John].tg
    tg.emojis = "🐶🍕🚗🎸"
    tg.conference = object()
    tg.others = 2
    tg.on_update(tg)
    await settle()
    tg.on_update(tg)
    await settle()
    assert sum("стал групповым" in t for t in m.notifier.texts(John)) == 1
    text = m.card_for(John)[0]
    assert "участников: 2" in text and "🔐 Ключ: 🐶🍕🚗🎸" in text
    assert call.state == CallState.CONNECTED and m.users[John].tg is tg


@pytest.mark.asyncio
async def test_failed_move_into_a_telegram_group_call_is_explained():
    loop = asyncio.get_running_loop()
    m = make_manager(loop)
    call = FakeSipCall(loop, incoming=True)
    await m._sip_incoming(m.accounts[0], call)
    await settle()
    m.users[John].tg.end("conference failed")
    await settle()
    assert any("Не удалось перейти в групповой звонок" in t for t in m.notifier.texts(John))
    assert call.state == CallState.TERMINATED and m.users[John].reconnect_task is None
