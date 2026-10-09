"""Two real NTgCalls instances call each other inside the process, with no Telegram servers.

This pins down what SIPgram relies on in the library and breaks first on an upgrade: the P2P key
exchange, the emoji fingerprint, external PCM in both directions, the protocol versions Telegram Web
speaks, and frames arriving in the order FrameSender hands them over.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import ntgcalls
import numpy as np
import pytest

from sipgram.tg.media import FrameSender, external_audio, merge_frames

pytestmark = pytest.mark.skipif(sys.platform == "win32" and not os.environ.get("SIPGRAM_LOOPBACK"),
                                reason="NTgCalls keeps the interpreter from exiting on Windows")

SAFE_PRIME = bytes.fromhex(
    "caf3feb934be6854e980c4da03ab5117c49616924cade4dfe783809f4688c9c16bb6cc6b17669907d96e44b16dc0e06e"
    "76dbaac756496438afea1ad2e2ae9bbf1a6504ef5acdf074eeac53f63891390aa0fc87ad01849c7e4f7d3be76c3e48f1"
    "5dd0a4f999b16dc67da4a72115dcf6a524283c3cfd2772fb19f07477b4e4bd0e671f729e20e12d0f6010ddd498d53ee8"
    "0b8c24406ffd3fc68bb787e4ac027d3fc1177fd327d8832d02a56e1f377f80fb1c529f22f124d11fe8b73f5e457cc020"
    "52f5bdd8c264b533dddec57991def514fef809be2380045f9fefb27b088f0c3921a00158fb022f629af60a617c9569ca"
    "656b6c45077254782ee3383a1270296f"
)
RATE = 8000
FRAMES = 200


def tone_frame(index: int) -> bytes:
    """Frame `index` carries one of eight tones, so the receiver can tell the order of frames by ear."""
    n = RATE // 100
    t = (np.arange(n) + index * n) / RATE
    return (8000 * np.sin(2 * np.pi * (500 + index % 8 * 400) * t)).astype("<i2").tobytes()


def tone_index(frame: bytes) -> int | None:
    x = np.frombuffer(frame, dtype="<i2").astype(np.float64)
    if len(x) == 0 or np.sqrt(np.mean(x ** 2)) < 500:
        return None
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freq = np.fft.rfftfreq(len(x), 1 / RATE)[np.argmax(spectrum[1:]) + 1]
    return int(round((freq - 500) / 400)) % 8


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["9.0.0", "13.0.0"])
async def test_private_call_between_two_instances(version):
    loop = asyncio.get_running_loop()
    a, b = ntgcalls.NTgCalls(), ntgcalls.NTgCalls()
    key_a, key_b = 1, 2                      # each side keys the call by the other one's id
    connected = {key_a: loop.create_future(), key_b: loop.create_future()}
    heard: list[bytes] = []

    def relay(target, key):
        return lambda chat_id, data: loop.call_soon_threadsafe(target.send_signaling_data, key, bytes(data))

    def on_connection(key):
        def callback(chat_id, info):
            if info.state == ntgcalls.ConnectionState.CONNECTED:
                loop.call_soon_threadsafe(lambda: connected[key].done() or connected[key].set_result(True))
        return callback

    def on_frames(chat_id, mode, device, frames):
        if mode == ntgcalls.StreamMode.PLAYBACK:
            heard.append(merge_frames(frames))

    a.on_signaling_data(relay(b, key_a))
    b.on_signaling_data(relay(a, key_b))
    a.on_connection_change(on_connection(key_b))
    b.on_connection_change(on_connection(key_a))
    b.on_frames(on_frames)
    try:
        for side, key in ((a, key_b), (b, key_a)):
            await side.create_p2p_call(key)
            await side.set_stream_sources(key, ntgcalls.StreamMode.CAPTURE, external_audio(RATE))
            await side.set_stream_sources(key, ntgcalls.StreamMode.PLAYBACK, external_audio(RATE))
        g_a_hash = await a.init_exchange(key_b, ntgcalls.DhConfig(2, SAFE_PRIME, os.urandom(256)), None)
        g_b = await b.init_exchange(key_a, ntgcalls.DhConfig(2, SAFE_PRIME, os.urandom(256)), g_a_hash)
        auth = await a.exchange_keys(key_b, g_b, 0)
        await b.exchange_keys(key_a, auth.g_a_or_b, auth.key_fingerprint)
        emojis = await a.get_emojis_fingerprint(key_b)
        assert emojis and emojis == await b.get_emojis_fingerprint(key_a), "both ends show the same key"
        assert version in ntgcalls.NTgCalls.get_protocol().library_versions
        await a.connect_p2p(key_b, [], [version], True, None)
        await b.connect_p2p(key_a, [], [version], True, None)
        await asyncio.wait_for(asyncio.gather(*connected.values()), 20)

        sender = FrameSender(a, key_b)
        start = time.monotonic()
        for i in range(0, FRAMES, 2):
            await asyncio.sleep(max(0.0, start + i / 100 - time.monotonic()))
            sender.push(tone_frame(i))             # two 10 ms frames per 20 ms, as an RTP packet brings them
            sender.push(tone_frame(i + 1))
        await asyncio.sleep(0.5)

        order = [i for i in map(tone_index, heard) if i is not None]
        assert len(order) > FRAMES * 0.8, f"only {len(order)} of {FRAMES} frames arrived"
        backwards = sum(1 for x, y in zip(order, order[1:], strict=False) if (y - x) % 8 == 7)
        assert backwards <= 2, f"{backwards} frames came out of order"
        assert sender.dropped == 0
    finally:
        await a.stop(key_b)
        await b.stop(key_a)
