"""Optional Bot API companion: inline buttons (keypad, hold/switch, transfer, call back) and a live call card."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import platform
from collections.abc import Awaitable, Callable
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError, RPCError
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault, DocumentAttributeAudio

from ..config import TelegramAppConfig
from ..messages import LANGUAGES, default_language, t

log = logging.getLogger("sipgram.bot")

Buttons = list[list[tuple[str, str]]]
KEYPAD_ROWS = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"], ["*", "0", "#"]]


class BotUi:
    def __init__(self, app: TelegramAppConfig, state_dir: Path):
        self.app = app
        self.client = TelegramClient(
            str(app.sessions_dir / app.bot_session), app.api_id, app.api_hash,
            device_model=app.device_model, app_version=app.app_version,
            system_version=f"{platform.system()} {platform.release()}",
        )
        self.username = ""
        self.on_command: Callable[[int, str], Awaitable[None]] | None = None
        self.card_provider: Callable[[int], tuple[str, Buttons | None, bool] | None] | None = None
        self.lang_of: Callable[[int], str] = lambda uid: default_language()
        self._state_path = state_dir / "bot_users.json"
        self._ready: set[int] = set()
        self._cards: dict[int, int] = {}
        self._keypad: set[int] = set()
        self._load()

    # ---- lifecycle ----

    async def start(self) -> None:
        await self.client.start(bot_token=self.app.bot_token)
        me = await self.client.get_me()
        self.username = getattr(me, "username", "") or ""
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_callback, events.CallbackQuery())
        await self._publish_commands()
        log.info("bot @%s started, %d users known", self.username, len(self._ready))

    async def _publish_commands(self) -> None:
        """Fills the bot's "/" menu, in each language Telegram may ask for."""
        names = ["status", "hangup", "switch", "transfer", "conf", "group", "rec", "cb", "redial", "dnd",
                 "schedule", "line", "history", "lang", "help"]
        for lang in LANGUAGES:
            try:
                await self.client(SetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code="" if lang == default_language() else lang,
                    commands=[BotCommand(command=n, description=t(f"cmd_{n}", lang)[:256]) for n in names],
                ))
            except Exception as e:
                log.debug("could not publish the %s bot command list: %s", lang, e)

    async def stop(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            pass

    def can_reach(self, uid: int) -> bool:
        return uid in self._ready

    def _load(self) -> None:
        try:
            if self._state_path.exists():
                self._ready = {int(x) for x in json.loads(self._state_path.read_text(encoding="utf-8"))}
        except Exception as e:
            log.debug("bot state unreadable: %s", e)

    def _mark_ready(self, uid: int) -> None:
        if uid in self._ready:
            return
        self._ready.add(uid)
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(sorted(self._ready)), encoding="utf-8")
        except Exception as e:
            log.debug("bot state save failed: %s", e)

    # ---- inbound ----

    async def _on_message(self, event) -> None:
        if not event.is_private or event.sender_id is None:
            return
        uid = int(event.sender_id)
        self._mark_ready(uid)
        text = (event.raw_text or "").strip()
        if self.on_command:
            await self.on_command(uid, text or "/start")

    async def _on_callback(self, event) -> None:
        uid = int(event.sender_id)
        self._mark_ready(uid)
        data = (event.data or b"").decode("utf-8", "replace")
        try:
            await event.answer()
        except RPCError:
            pass
        if data == "keypad:toggle":
            if uid in self._keypad:
                self._keypad.discard(uid)
            else:
                self._keypad.add(uid)
            await self.refresh_card(uid)
            return
        if self.on_command:
            await self.on_command(uid, data)

    # ---- outbound ----

    @staticmethod
    def _markup(buttons: Buttons | None):
        if not buttons:
            return None
        rows = [[Button.inline(label, data.encode("utf-8")) for label, data in row] for row in buttons if row]
        return rows or None

    async def send(self, uid: int, text: str, buttons: Buttons | None = None) -> bool:
        try:
            await self.client.send_message(uid, text, buttons=self._markup(buttons))
            return True
        except RPCError as e:
            log.info("bot cannot message %s (%s); falling back to the gateway account", uid, e)
            self._ready.discard(uid)
            return False
        except Exception as e:
            log.warning("bot send failed: %s", e)
            return False

    async def send_voice(self, uid: int, ogg: bytes, duration: int, caption: str = "") -> bool:
        buf = io.BytesIO(ogg)
        buf.name = "call.ogg"
        try:
            await self.client.send_file(
                uid, buf, caption=caption or None, voice_note=True,
                attributes=[DocumentAttributeAudio(duration=duration, voice=True)],
            )
            return True
        except Exception as e:
            log.info("bot could not send the recording to %s: %s", uid, e)
            return False

    async def card(self, uid: int, text: str, buttons: Buttons | None, keypad_ok: bool) -> bool:
        rows: Buttons = [list(r) for r in (buttons or [])]
        if keypad_ok:
            lang = self.lang_of(uid)
            if uid in self._keypad:
                rows = [[(d, f"dtmf:{d}") for d in row] for row in KEYPAD_ROWS] + [[(t("btn_hide", lang), "keypad:toggle")]] + rows
            else:
                rows.append([(t("btn_keypad", lang), "keypad:toggle")])
        else:
            self._keypad.discard(uid)
        markup = self._markup(rows)
        msg_id = self._cards.get(uid)
        if msg_id:
            try:
                await self.client.edit_message(uid, msg_id, text, buttons=markup)
                return True
            except MessageNotModifiedError:
                return True
            except RPCError as e:
                log.debug("card edit failed (%s), sending a new one", e)
                self._cards.pop(uid, None)
        try:
            msg = await self.client.send_message(uid, text, buttons=markup)
            self._cards[uid] = msg.id
            return True
        except RPCError as e:
            log.info("bot cannot send card to %s: %s", uid, e)
            self._ready.discard(uid)
            return False

    async def refresh_card(self, uid: int) -> None:
        if self.card_provider is None:
            return
        spec = self.card_provider(uid)
        if spec is None:
            await self.close_card(uid, t("call_ended_short", self.lang_of(uid)))
            return
        text, buttons, keypad_ok = spec
        await self.card(uid, text, buttons, keypad_ok)

    async def close_card(self, uid: int, text: str, buttons: Buttons | None = None) -> bool:
        """Rewrites the card with the outcome of the call, so the chat keeps one message per call."""
        msg_id = self._cards.pop(uid, None)
        self._keypad.discard(uid)
        if not msg_id:
            return False
        try:
            await self.client.edit_message(uid, msg_id, text, buttons=self._markup(buttons))
            return True
        except MessageNotModifiedError:
            return True
        except Exception as e:
            log.debug("closing the card failed: %s", e)
            return False


async def bot_login_check(app: TelegramAppConfig) -> str:
    """Connects with the bot token once and returns @username (used by `sipgram whoami`)."""
    client = TelegramClient(str(app.sessions_dir / app.bot_session), app.api_id, app.api_hash)
    await client.start(bot_token=app.bot_token)
    me = await client.get_me()
    await client.disconnect()
    await asyncio.sleep(0)
    return getattr(me, "username", "") or str(getattr(me, "id", ""))
