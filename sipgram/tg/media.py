"""PCM in and out of NTgCalls: the external audio description, ordered frame delivery, mixing."""
from __future__ import annotations

import logging
from collections import deque
from functools import reduce

import ntgcalls

from ..audio.resample import mix

log = logging.getLogger("sipgram.tg.media")

QUEUE_FRAMES = 10


def external_audio(rate: int) -> ntgcalls.MediaDescription:
    """Raw PCM16 mono in both directions.

    NTgCalls delivers incoming frames to on_frames only when the playback source is declared as the
    microphone, so the same description serves capture and playback.
    """
    desc = ntgcalls.AudioDescription(ntgcalls.MediaSource.EXTERNAL, rate, 1, "", False)
    return ntgcalls.MediaDescription(microphone=desc, speaker=None, camera=None, screen=None)


def merge_frames(frames) -> bytes:
    """One 10 ms frame out of what NTgCalls hands over for a tick: one frame per speaking participant."""
    data = [bytes(f.data) for f in frames if f.data]
    return reduce(mix, data) if data else b""


class FrameSender:
    """Hands 10 ms frames to NTgCalls one at a time.

    NTgCalls 3 runs every call on a thread pool, so two frames sent back to back can overtake each
    other on the way into WebRTC. The next frame goes out once the previous one is through; while a
    send is stuck the queue keeps the newest QUEUE_FRAMES frames.
    """

    def __init__(self, ntg: ntgcalls.NTgCalls, key: int):
        self.ntg = ntg
        self.key = key
        self.dropped = 0
        self._queue: deque[bytes] = deque()
        self._busy = False

    def push(self, frame: bytes) -> None:
        if self._busy:
            if len(self._queue) >= QUEUE_FRAMES:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(frame)
            return
        self._send(frame)

    def _send(self, frame: bytes) -> None:
        try:
            fut = self.ntg.send_external_frame(self.key, ntgcalls.StreamDevice.MICROPHONE, frame, ntgcalls.FrameData())
        except Exception as e:
            log.debug("send_external_frame failed: %s", e)
            self._busy = False
            self._queue.clear()
            return
        self._busy = True
        fut.add_done_callback(self._done)

    def _done(self, fut) -> None:
        if not fut.cancelled() and fut.exception() is not None:
            log.debug("external frame error: %s", fut.exception())
        if self._queue:
            self._send(self._queue.popleft())
        else:
            self._busy = False
