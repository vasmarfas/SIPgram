"""Telegram call engine against a fake NTgCalls and a scripted MTProto account (no network)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import ntgcalls
import numpy as np
import pytest
from telethon.tl import functions, types

from sipgram.tg import calls as tg_calls
from sipgram.tg.calls import CONFERENCE_KEY_BASE, TgCallEngine, TgCallState
from sipgram.tg.media import QUEUE_FRAMES, FrameSender, merge_frames

ME = 900
PEER = 111
CALL_ID = 5555
REAL_PROTOCOL = ntgcalls.NTgCalls.get_protocol()
PROTO = types.PhoneCallProtocol(min_layer=92, max_layer=92, library_versions=["13.0.0"], udp_p2p=True, udp_reflector=True)
JOIN = ntgcalls.ConferenceJoinParams('{"conference":1}', bytes(range(32)), b"join-block")


class FakeNtg:
    def __init__(self):
        self.ops: list[tuple] = []
        self.callbacks: dict[str, object] = {}
        self.fail: dict[str, Exception] = {}
        self.sent: list[tuple[int, bytes]] = []
        self.inflight: list[asyncio.Future] = []
        self.auto_complete = True

    @staticmethod
    def get_protocol():
        return REAL_PROTOCOL

    def __getattr__(self, name):
        if name.startswith("on_"):
            return lambda callback: self.callbacks.__setitem__(name, callback)
        raise AttributeError(name)

    def _op(self, *op):
        self.ops.append(op)
        error = self.fail.pop(op[0], None)
        if error is not None:
            raise error

    def done(self, name: str) -> list[tuple]:
        return [op for op in self.ops if op[0] == name]

    async def create_p2p_call(self, key):
        self._op("create_p2p_call", key)

    async def set_stream_sources(self, key, mode, media):
        self._op("set_stream_sources", key, mode, media.microphone.sample_rate)

    async def init_exchange(self, key, dh, ga_hash):
        self._op("init_exchange", key, ga_hash)
        return b"g_a_hash" if ga_hash is None else b"g_b"

    async def exchange_keys(self, key, g, fingerprint):
        self._op("exchange_keys", key, g, fingerprint)
        return SimpleNamespace(g_a_or_b=b"g_a", key_fingerprint=42)

    async def connect_p2p(self, key, servers, versions, p2p_allowed, custom):
        self._op("connect_p2p", key, list(versions), p2p_allowed, custom)

    async def send_signaling_data(self, key, data):
        self._op("send_signaling_data", key, data)

    async def stop(self, key):
        self._op("stop", key)

    async def init_conference(self, key, user_id, last_block):
        self._op("init_conference", key, user_id, last_block)
        return JOIN

    async def connect(self, key, params, presentation):
        self._op("connect", key, params)

    async def apply_blocks(self, key, subchain, next_offset, blocks, from_short_poll):
        self._op("apply_blocks", key, subchain, next_offset, list(blocks), from_short_poll)

    async def finish_subchain_request(self, key, subchain):
        self._op("finish_subchain_request", key, subchain)

    async def update_audio_ssrc_mappings(self, key, mappings):
        self._op("ssrc", key, sorted((m.user_id, m.ssrc) for m in mappings))

    def send_external_frame(self, key, device, data, frame_data):
        fut = asyncio.get_running_loop().create_future()
        self.sent.append((key, data))
        if self.auto_complete:
            fut.set_result(None)
        else:
            self.inflight.append(fut)
        return fut


class ScriptedAccount:
    """Answers MTProto requests from a table keyed by request type."""

    def __init__(self):
        self.me = SimpleNamespace(id=ME)
        self.requests: list[object] = []
        self.replies: dict[type, object] = {}
        self.handlers: list[object] = []

    def add_raw_handler(self, handler):
        self.handlers.append(handler)

    async def invoke(self, request):
        self.requests.append(request)
        reply = self.replies.get(type(request))
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, Exception):
            raise reply
        return reply if reply is not None else updates()

    def sent(self, cls) -> list:
        return [r for r in self.requests if isinstance(r, cls)]


def updates(*items) -> types.Updates:
    return types.Updates(updates=list(items), users=[], chats=[], date=None, seq=0)


def group_call(call_id: int) -> types.GroupCall:
    return types.GroupCall(id=call_id, access_hash=5, participants_count=2, unmuted_video_limit=0, version=1, conference=True)


def participant(uid: int, ssrc: int, left: bool = False) -> types.GroupCallParticipant:
    return types.GroupCallParticipant(peer=types.PeerUser(user_id=uid), date=None, source=ssrc, left=left, is_self=uid == ME)


def pcm(level: int, samples: int = 80) -> bytes:
    return np.full(samples, level, dtype="<i2").tobytes()


def make_engine(monkeypatch) -> tuple[TgCallEngine, ScriptedAccount]:
    monkeypatch.setattr(tg_calls.ntgcalls, "NTgCalls", FakeNtg)
    account = ScriptedAccount()
    return TgCallEngine(account), account


async def until(condition, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def script_conference(account: ScriptedAccount, ref, call_id: int = 77) -> None:
    account.replies[functions.phone.GetGroupCallChainBlocksRequest] = updates(
        types.UpdateGroupCallChainBlocks(call=ref, sub_chain_id=0, blocks=[b"b1", b"b2"], next_offset=2))
    account.replies[functions.phone.JoinGroupCallRequest] = updates(
        types.UpdateGroupCall(call=group_call(call_id)),
        types.UpdateGroupCallConnection(params=types.DataJSON(data='{"transport":"conf"}')),
        types.UpdateGroupCallChainBlocks(call=types.InputGroupCall(id=call_id, access_hash=5), sub_chain_id=1,
                                         blocks=[b"c1"], next_offset=1))
    account.replies[functions.phone.GetGroupParticipantsRequest] = types.phone.GroupParticipants(
        count=2, participants=[participant(PEER, 111), participant(ME, 222)], next_offset="", chats=[], users=[], version=1)


async def connected_call(engine: TgCallEngine, account: ScriptedAccount, versions=("13.0.0",), custom='{"web":true}'):
    """Runs an outgoing private call through the whole exchange, as Telegram would answer it."""
    account.replies[functions.messages.GetDhConfigRequest] = types.messages.DhConfig(g=3, p=b"p" * 256, version=1, random=b"r" * 256)
    account.replies[functions.phone.RequestCallRequest] = types.phone.PhoneCall(phone_call=types.PhoneCallWaiting(
        id=CALL_ID, access_hash=7, date=None, admin_id=ME, participant_id=PEER, protocol=PROTO), users=[])
    account.replies[functions.phone.ConfirmCallRequest] = types.phone.PhoneCall(phone_call=types.PhoneCall(
        id=CALL_ID, access_hash=7, date=None, admin_id=ME, participant_id=PEER, g_a_or_b=b"g_a", key_fingerprint=42,
        protocol=types.PhoneCallProtocol(min_layer=92, max_layer=92, library_versions=list(versions), udp_p2p=True, udp_reflector=True),
        connections=[], start_date=None, p2p_allowed=True, conference_supported=True,
        custom_parameters=types.DataJSON(data=custom) if custom else None), users=[])
    task = asyncio.ensure_future(engine.call(types.InputUser(user_id=PEER, access_hash=1), sample_rate=8000, ring_timeout=5))
    await until(lambda: account.sent(functions.phone.RequestCallRequest))
    await engine._on_raw_update(types.UpdatePhoneCall(phone_call=types.PhoneCallAccepted(
        id=CALL_ID, access_hash=7, date=None, admin_id=ME, participant_id=PEER, g_b=b"g_b", protocol=PROTO)))
    await until(lambda: engine.ntg.done("connect_p2p"))
    engine._connection_changed(PEER, "CONNECTED")
    return await asyncio.wait_for(task, 2)


# ---- media helpers -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_sender_keeps_frames_in_order():
    """NTgCalls 3 runs sends on a thread pool: the next frame may go only after the previous one is through."""
    ntg = FakeNtg()
    ntg.auto_complete = False
    sender = FrameSender(ntg, 1)
    for i in range(3):
        sender.push(bytes([i]))
    assert [d for _, d in ntg.sent] == [b"\x00"], "only one frame is in flight"
    for _ in range(2):
        ntg.inflight.pop(0).set_result(None)
        await asyncio.sleep(0)
    assert [d for _, d in ntg.sent] == [b"\x00", b"\x01", b"\x02"]
    for i in range(30):
        sender.push(bytes([100 + i]))
    assert sender.dropped == 30 - QUEUE_FRAMES, "a stuck send keeps only the newest frames"
    ntg.auto_complete = True
    ntg.inflight.pop(0).set_result(None)
    await until(lambda: len(ntg.sent) == 3 + QUEUE_FRAMES)
    assert [d[0] for _, d in ntg.sent[3:]] == list(range(130 - QUEUE_FRAMES, 130))


def test_merge_frames_sums_the_speakers_of_one_tick():
    frames = [SimpleNamespace(data=pcm(1000)), SimpleNamespace(data=pcm(-300)), SimpleNamespace(data=b"")]
    assert set(np.frombuffer(merge_frames(frames), dtype="<i2")) == {700}
    loud = [SimpleNamespace(data=pcm(30000)), SimpleNamespace(data=pcm(30000))]
    assert set(np.frombuffer(merge_frames(loud), dtype="<i2")) == {32767}, "the sum is clipped, not wrapped"
    assert merge_frames([]) == b""


# ---- private calls -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_call_offers_the_web_protocols_and_passes_custom_parameters(monkeypatch):
    engine, account = make_engine(monkeypatch)
    call = await connected_call(engine, account)
    offered = account.sent(functions.phone.RequestCallRequest)[0].protocol.library_versions
    assert {"12.0.0", "13.0.0"} <= set(offered), "Telegram Web speaks 12/13, ntgcalls 3 must offer them"
    assert engine.ntg.done("connect_p2p") == [("connect_p2p", PEER, ["13.0.0"], True, '{"web":true}')]
    assert [op[3] for op in engine.ntg.done("set_stream_sources")] == [8000, 8000]
    assert call.state == TgCallState.CONNECTED and call.library_version == "13.0.0"
    seen = []
    call.on_update = seen.append
    engine._on_emojis(PEER, "🐶🍕🚗🎸")
    await asyncio.sleep(0.01)
    assert call.emojis == "🐶🍕🚗🎸" and seen == [call]
    call.send_audio(pcm(10))
    assert engine.ntg.sent[-1] == (PEER, pcm(10))
    await call.hangup()
    assert account.sent(functions.phone.DiscardCallRequest) and engine.ntg.done("stop")


# ---- conferences ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_adding_people_moves_the_call_into_a_conference(monkeypatch):
    engine, account = make_engine(monkeypatch)
    call = await connected_call(engine, account)
    heard: list[bytes] = []
    call.on_audio = heard.append
    seen = []
    call.on_update = seen.append
    script_conference(account, types.InputGroupCallSlug(slug="abc"))

    await engine._on_raw_update(types.UpdatePhoneCall(phone_call=types.PhoneCallDiscarded(
        id=CALL_ID, reason=types.PhoneCallDiscardReasonMigrateConferenceCall(slug="abc"))))
    engine._connection_changed(PEER, "CLOSED")          # the private media goes down first
    await until(lambda: engine.ntg.done("connect"))
    assert call.active, "closing the private media must not end a call that is moving"
    join = account.sent(functions.phone.JoinGroupCallRequest)[0]
    assert join.call == types.InputGroupCallSlug(slug="abc") and isinstance(join.join_as, types.InputPeerSelf)
    assert join.block == b"join-block" and join.public_key == int.from_bytes(bytes(range(32)), "little", signed=True)
    assert engine.ntg.done("init_conference") == [("init_conference", PEER, ME, b"b2")], "joined from the last block"
    assert engine.ntg.done("connect") == [("connect", PEER, '{"transport":"conf"}')]
    assert ("apply_blocks", PEER, 1, 1, [b"c1"], False) in engine.ntg.ops
    assert engine.ntg.done("ssrc") == [("ssrc", PEER, [(PEER, 111), (ME, 222)])]
    engine._connection_changed(PEER, "CONNECTED")
    await until(lambda: seen)
    assert call.conference.call.id == 77 and call.others == 1
    assert not account.sent(functions.phone.DiscardCallRequest) and not engine.ntg.done("stop")
    await engine._on_raw_update(types.UpdatePhoneCall(phone_call=types.PhoneCallDiscarded(
        id=CALL_ID, reason=types.PhoneCallDiscardReasonMigrateConferenceCall(slug="abc"))))
    await asyncio.sleep(0.01)
    assert len(account.sent(functions.phone.JoinGroupCallRequest)) == 1 and call.active, "a repeated update changes nothing"

    engine._on_frames(PEER, ntgcalls.StreamMode.PLAYBACK, ntgcalls.StreamDevice.MICROPHONE,
                      [SimpleNamespace(data=pcm(1000), ssrc=111), SimpleNamespace(data=pcm(500), ssrc=333)])
    assert set(np.frombuffer(heard[-1], dtype="<i2")) == {1500}, "the SIP leg hears everyone at once"

    await engine._broadcast_block(PEER, b"out")
    assert account.sent(functions.phone.SendConferenceCallBroadcastRequest)[0].call.id == 77
    account.replies[functions.phone.GetGroupCallChainBlocksRequest] = updates(types.UpdateGroupCallChainBlocks(
        call=types.InputGroupCall(id=77, access_hash=5), sub_chain_id=1, blocks=[b"c2"], next_offset=2))
    await engine._fetch_subchain(PEER, 1, 1, 10)
    asked = account.sent(functions.phone.GetGroupCallChainBlocksRequest)[-1]
    assert (asked.sub_chain_id, asked.offset, asked.limit) == (1, 1, 10)
    assert engine.ntg.ops[-2:] == [("apply_blocks", PEER, 1, 2, [b"c2"], True), ("finish_subchain_request", PEER, 1)]
    await engine._on_raw_update(types.UpdateGroupCallChainBlocks(
        call=types.InputGroupCall(id=77, access_hash=5), sub_chain_id=0, blocks=[b"b3"], next_offset=3))
    assert engine.ntg.ops[-1] == ("apply_blocks", PEER, 0, 3, [b"b3"], False)

    await engine._on_raw_update(types.UpdateGroupCallParticipants(
        call=types.InputGroupCall(id=77, access_hash=5), participants=[participant(PEER, 111, left=True)], version=2))
    assert not call.active and call.end_reason == "hangup", "the gateway does not stay alone in the conference"
    assert account.sent(functions.phone.LeaveGroupCallRequest)[0].call.id == 77
    assert engine.ntg.done("stop") and PEER not in engine.conferences


@pytest.mark.asyncio
async def test_migration_starts_fresh_media_when_the_private_call_is_already_gone(monkeypatch):
    engine, account = make_engine(monkeypatch)
    call = await connected_call(engine, account)
    script_conference(account, types.InputGroupCallSlug(slug="abc"))
    engine.ntg.fail["init_conference"] = ntgcalls.ConnectionNotFound("removed")
    await engine._on_raw_update(types.UpdatePhoneCall(phone_call=types.PhoneCallDiscarded(
        id=CALL_ID, reason=types.PhoneCallDiscardReasonMigrateConferenceCall(slug="abc"))))
    await until(lambda: engine.ntg.done("connect"))
    names = [op[0] for op in engine.ntg.ops]
    first = names.index("init_conference")
    assert names[first + 1:first + 5] == ["create_p2p_call", "init_conference", "set_stream_sources", "set_stream_sources"]
    engine._connection_changed(PEER, "CONNECTED")
    await asyncio.sleep(0.01)
    assert call.active and call.conference is not None


@pytest.mark.asyncio
async def test_conference_invite_rings_like_a_call_and_joins_on_accept(monkeypatch):
    engine, account = make_engine(monkeypatch)
    offered = []
    engine.on_incoming = offered.append
    invite = types.MessageService(id=50, peer_id=types.PeerUser(user_id=PEER), date=None, out=False,
                                  action=types.MessageActionConferenceCall(call_id=77))
    await engine._on_raw_update(types.UpdateNewMessage(message=invite, pts=1, pts_count=1))
    assert len(offered) == 1
    call = offered[0]
    assert call.invite_msg_id == 50 and call.state == TgCallState.INCOMING
    ref = types.InputGroupCallInviteMessage(msg_id=50)
    script_conference(account, ref)
    engine.ntg.fail["init_conference"] = ntgcalls.ConnectionNotFound("no private call to take over")
    task = asyncio.ensure_future(call.accept())
    await until(lambda: engine.ntg.done("connect"))
    assert account.sent(functions.phone.JoinGroupCallRequest)[0].call == ref
    engine._connection_changed(PEER, "CONNECTED")
    await asyncio.wait_for(task, 2)
    assert call.state == TgCallState.CONNECTED and call.others == 1
    await call.hangup()
    assert account.sent(functions.phone.LeaveGroupCallRequest) and not account.sent(functions.phone.DeclineConferenceCallInviteRequest)


@pytest.mark.asyncio
async def test_unwanted_invites_are_declined_and_old_ones_ignored(monkeypatch):
    engine, account = make_engine(monkeypatch)
    for action in (types.MessageActionConferenceCall(call_id=1, missed=True),
                   types.MessageActionConferenceCall(call_id=2, duration=30)):
        old = types.MessageService(id=10, peer_id=types.PeerUser(user_id=PEER), date=None, action=action)
        await engine._on_raw_update(types.UpdateNewMessage(message=old, pts=1, pts_count=1))
    assert not engine.calls, "a finished or missed call in the history does not ring"
    invite = types.MessageService(id=51, peer_id=types.PeerUser(user_id=PEER), date=None,
                                  action=types.MessageActionConferenceCall(call_id=77))
    await engine._on_raw_update(types.UpdateNewMessage(message=invite, pts=2, pts_count=1))
    declined = account.sent(functions.phone.DeclineConferenceCallInviteRequest)
    assert [d.msg_id for d in declined] == [51] and not engine.calls


@pytest.mark.asyncio
async def test_withdrawn_invite_stops_ringing(monkeypatch):
    engine, account = make_engine(monkeypatch)
    offered = []
    engine.on_incoming = offered.append
    invite = types.MessageService(id=52, peer_id=types.PeerUser(user_id=PEER), date=None,
                                  action=types.MessageActionConferenceCall(call_id=77))
    await engine._on_raw_update(types.UpdateNewMessage(message=invite, pts=1, pts_count=1))
    invite.action = types.MessageActionConferenceCall(call_id=77, missed=True)
    await engine._on_raw_update(types.UpdateEditMessage(message=invite, pts=2, pts_count=1))
    assert not offered[0].active and offered[0].end_reason == "missed"


@pytest.mark.asyncio
async def test_gateway_starts_a_conference_call_for_a_group_call_without_a_group(monkeypatch):
    engine, account = make_engine(monkeypatch)
    account.replies[functions.phone.CreateConferenceCallRequest] = updates(
        types.UpdateGroupCall(call=group_call(88)),
        types.UpdateGroupCallConnection(params=types.DataJSON(data='{"transport":"new"}')))
    task = asyncio.ensure_future(engine.create_conference("SIPgram"))
    key = CONFERENCE_KEY_BASE - 1
    await until(lambda: engine.ntg.done("connect"))
    engine._connection_changed(key, "CONNECTED")
    group = await asyncio.wait_for(task, 2)
    assert group.conference and group.call.id == 88 and group.key == key
    assert [op[0] for op in engine.ntg.ops[:3]] == ["create_p2p_call", "init_conference", "set_stream_sources"]
    assert engine.ntg.done("init_conference") == [("init_conference", key, ME, None)], "a new chain starts here"
    assert {op[3] for op in engine.ntg.done("set_stream_sources")} == {48000}
    created = account.sent(functions.phone.CreateConferenceCallRequest)[0]
    assert created.join and created.block == b"join-block" and created.params.data == '{"conference":1}'
    await group.invite([types.InputUser(user_id=PEER, access_hash=1)])
    invited = account.sent(functions.phone.InviteConferenceCallParticipantRequest)
    assert invited[0].call.id == 88 and invited[0].user_id.user_id == PEER
    await engine.leave_group(group)
    assert account.sent(functions.phone.LeaveGroupCallRequest)[0].call.id == 88
    assert not engine.groups and not engine.conferences


@pytest.mark.asyncio
async def test_failed_move_into_the_conference_ends_the_call_without_leaving_anything_behind(monkeypatch):
    engine, account = make_engine(monkeypatch)
    call = await connected_call(engine, account)
    account.replies[functions.phone.GetGroupCallChainBlocksRequest] = updates()
    await engine._on_raw_update(types.UpdatePhoneCall(phone_call=types.PhoneCallDiscarded(
        id=CALL_ID, reason=types.PhoneCallDiscardReasonMigrateConferenceCall(slug="abc"))))
    await until(lambda: not call.active)
    assert call.end_reason == "conference failed" and PEER not in engine.conferences
    assert not account.sent(functions.phone.JoinGroupCallRequest) and not account.sent(functions.phone.LeaveGroupCallRequest)
