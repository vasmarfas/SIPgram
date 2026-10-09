"""Telegram group calls: the voice chat of a group, or a conference call that needs no group.

`/conf` builds a conference on the PBX, which is the right place for internal numbers. A Telegram
group call is the other direction: it lets people who have no extension, and are only reachable in
Telegram, join the same conversation. The gateway joins the voice chat of the configured group, or
starts an E2E conference call when no group is configured, and mixes the SIP legs into it.

Voice chat:  create_call(chat_id) -> payload, set_stream_sources, phone.joinGroupCall(params=payload),
             UpdateGroupCallConnection.params -> connect(chat_id, params, False).
Conference:  create_p2p_call(key) -> init_conference -> phone.createConferenceCall(join, public_key,
             block, params) -> connect(key, params, False); see calls.py for the E2E chain.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

from telethon import utils
from telethon.errors import RPCError
from telethon.tl import functions, types

from .media import FrameSender

log = logging.getLogger("sipgram.tg.group")

GROUP_RATE = 48000


async def input_group_call(account, chat: str, create: bool = True, title: str = "") -> tuple[types.InputGroupCall, object, str]:
    """Returns the voice chat of `chat`, starting one when the group has none."""
    entity = await account.client.get_entity(chat)
    peer = utils.get_input_peer(entity)
    name = getattr(entity, "title", None) or str(chat)
    if isinstance(peer, types.InputPeerUser):
        raise ValueError(f"{chat} is a user, not a group: a voice chat needs a group or a channel")
    call = await _existing_call(account, entity)
    if call is not None:
        return call, peer, name
    if not create:
        raise ValueError(f"{name} has no active voice chat")
    result = await account.invoke(functions.phone.CreateGroupCallRequest(
        peer=peer, random_id=random.randint(1, 0x7FFFFFFE), title=title or None,
    ))
    for update in getattr(result, "updates", []):
        if isinstance(update, types.UpdateGroupCall) and isinstance(update.call, types.GroupCall):
            log.info("started a voice chat in %s", name)
            return types.InputGroupCall(id=update.call.id, access_hash=update.call.access_hash), peer, name
    raise ValueError(f"could not start a voice chat in {name}")


async def _existing_call(account, entity) -> types.InputGroupCall | None:
    if isinstance(entity, types.Channel):
        full = await account.invoke(functions.channels.GetFullChannelRequest(channel=entity))
    else:
        full = await account.invoke(functions.messages.GetFullChatRequest(chat_id=entity.id))
    call = getattr(full.full_chat, "call", None)
    return call if isinstance(call, types.InputGroupCall) else None


class TgGroupCall:
    """One group call the gateway takes part in. Audio is exchanged as 10 ms PCM16 at 48 kHz."""

    def __init__(self, engine, key: int, call: types.InputGroupCall | None, peer, title: str, conference: bool = False):
        self.engine = engine
        self.key = key
        self.call = call
        self.peer = peer
        self.title = title
        self.conference = conference
        self.sender = FrameSender(engine.ntg, key)
        self.sample_rate = GROUP_RATE
        self.joined = False
        self.created = time.time()
        self.ended: asyncio.Future = engine.loop.create_future()
        self.connected: asyncio.Future = engine.loop.create_future()
        self.on_audio: Callable[[int, bytes], None] | None = None
        self.participants = 0
        self.frames_in = 0
        self.frames_out = 0

    @property
    def active(self) -> bool:
        return not self.ended.done()

    def frame_bytes(self) -> int:
        return self.sample_rate * 2 // 100

    def send_audio(self, pcm10ms: bytes) -> None:
        if not self.joined or not self.active:
            return
        self.sender.push(pcm10ms)
        self.frames_out += 1

    async def invite(self, users: list[types.InputUser]) -> list[str]:
        """Rings the group call on those users' phones. Returns the ones Telegram refused."""
        failed: list[str] = []
        for user in users:
            if self.conference:
                request = functions.phone.InviteConferenceCallParticipantRequest(call=self.call, user_id=user, video=False)
            else:
                request = functions.phone.InviteToGroupCallRequest(call=self.call, users=[user])
            try:
                await self.engine.account.invoke(request)
            except RPCError as e:
                log.info("cannot invite %s into the group call: %s", user.user_id, e)
                failed.append(str(user.user_id))
        return failed

    async def leave(self, reason: str = "hangup") -> None:
        await self.engine.leave_group(self, reason)
