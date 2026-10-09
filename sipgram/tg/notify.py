"""Where user-facing messages go: the bot (with buttons) when the user has started it,
otherwise the gateway account that serves that user."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..messages import default_language, t
from .account import TgAccount

if TYPE_CHECKING:
    from .bot import BotUi

log = logging.getLogger("sipgram.notify")

Buttons = list[list[tuple[str, str]]]


class Notifier:
    def __init__(self, account_of: Callable[[int], TgAccount], bot: BotUi | None = None,
                 lang_of: Callable[[int], str] | None = None):
        self.account_of = account_of
        self.bot = bot
        self.lang_of = lang_of or (lambda uid: default_language())
        self._hinted: set[int] = set()

    def via_bot(self, uid: int) -> bool:
        return self.bot is not None and self.bot.can_reach(uid)

    async def send(self, uid: int, text: str, buttons: Buttons | None = None) -> None:
        if self.via_bot(uid):
            assert self.bot is not None
            if await self.bot.send(uid, text, buttons):
                return
        await self.account_of(uid).send_text(uid, text)
        if self.bot is not None and uid not in self._hinted and self.bot.username:
            self._hinted.add(uid)
            await self.account_of(uid).send_text(uid, t("bot_hint", self.lang_of(uid), bot=f"@{self.bot.username}"))

    async def send_voice(self, uid: int, ogg: bytes, duration: int, caption: str = "") -> bool:
        """Sends a recording as a Telegram voice message; falls back to the gateway account."""
        if self.via_bot(uid):
            assert self.bot is not None
            if await self.bot.send_voice(uid, ogg, duration, caption):
                return True
        return await self.account_of(uid).send_voice(uid, ogg, duration, caption)

    async def card(self, uid: int, text: str, buttons: Buttons | None, keypad_ok: bool) -> bool:
        """Shows the live call card. Returns False when the user has no bot to show it in."""
        if self.via_bot(uid):
            assert self.bot is not None
            return await self.bot.card(uid, text, buttons, keypad_ok)
        return False

    async def close_card(self, uid: int, text: str, buttons: Buttons | None = None) -> bool:
        """Turns the live card into the last word about the call. False when there was no card."""
        if self.bot is not None:
            return await self.bot.close_card(uid, text, buttons)
        return False
