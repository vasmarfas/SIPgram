"""User-facing texts (Russian / English) and small formatting helpers.

Telegram renders these as Markdown (Telethon's default parse mode). Commands are wrapped in
backticks: a chat with a user account has no clickable /commands, but a code span is tap-to-copy.
Values substituted into a template have their backticks removed so a caller name cannot break it.
"""
from __future__ import annotations

import re
import time

_LANG = "ru"

STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "incoming": "📞 **Входящий:** {caller}\nЛиния {account}",
        "incoming_waiting": "📞 **Второй входящий:** {caller}\n`/switch` — принять, текущий на удержание\n`/decline` — отклонить",
        "missed": "📵 **Пропущенный:** {caller}\nЛиния {account}. Перезвонить: `/cb`",
        "waiting_missed": "📵 Второй вызов от {caller} не принят",
        "call_ended": "📴 Разговор с {peer} завершён, **{duration}**",
        "call_ended_short": "📴 Завершено",
        "dialing_callback": "📲 Звоню вам, после ответа набираю `{number}`",
        "dialing_direct": "📞 Набираю `{number}`, сейчас перезвоню вам",
        "dialing_consult": "📞 Набираю `{number}`, {held} на удержании.\n`/switch` — вернуться, `/transfer` — соединить их между собой",
        "dial_failed": "❌ `{number}`: {reason}",
        "dial_error": "❌ Не удалось начать вызов: {reason}",
        "not_a_number": "Не похоже на номер: {text}",
        "need_number": "Сначала отправьте номер сообщением, потом позвоните.\nИли задайте `default_destination` для линии.",
        "no_active_call": "Нет активного звонка.",
        "no_waiting": "Нет ожидающего вызова.",
        "nothing_to_switch": "Переключаться не на что: нет второго вызова.",
        "switched": "🔁 Говорите с {peer}. На удержании: {held}",
        "resumed": "▶️ Продолжаете разговор с {peer}",
        "declined": "Вызов от {caller} отклонён",
        "transfer_done": "✅ {a} и {b} соединены, вы вышли из разговора",
        "transfer_started": "↪️ Перевожу {peer} на `{number}`…",
        "transfer_failed": "❌ Перевод не удался ({reason}), разговор продолжается",
        "transfer_usage": "`/transfer <номер>` — перевести собеседника.\n`/transfer` без номера — соединить активный и удерживаемый вызовы.",
        "dnd_on": "🔕 **Не беспокоить включено:** входящие с АТС получают «занято». Выключить: `/dnd`",
        "dnd_off": "🔔 **Не беспокоить выключено**",
        "busy_line": "⛔ Вы уже в разговоре. Отправьте номер — наберу его, текущий уйдёт на удержание.",
        "busy_gateway": "⛔ Все каналы шлюза заняты ({n}), попробуйте позже",
        "reconnecting": "⚠️ Связь с Telegram прервалась, собеседник на удержании. Перезваниваю вам…",
        "reconnect_failed": "❌ Восстановить разговор не удалось, вызов завершён",
        "reconnected": "✅ Разговор восстановлен",
        "line_current": "Линия для исходящих: **{account}**",
        "line_list": "Ваши линии: {lines}\nВыбрать: `/line <название>`",
        "line_unknown": "Нет такой линии: {name}",
        "no_line": "У вас нет линии для исходящих звонков",
        "dtmf_sent": "⌨️ `{digits}`",
        "history_empty": "Звонков ещё не было",
        "history_header": "**Последние звонки**",
        "status_header": "**Шлюз:** {gateway}",
        "status_account": "{icon} {name} (`{username}@{domain}`): {state}",
        "status_active": "🟢 Разговор с {peer} ({direction}), **{duration}**",
        "status_held": "⏸ На удержании: {peer}",
        "status_waiting": "📞 Ожидает ответа: {caller}",
        "status_idle": "Активных звонков нет",
        "status_dnd": "🔕 Не беспокоить включено",
        "registered": "зарегистрирован",
        "unregistered": "**НЕ зарегистрирован**",
        "unknown_command": "Не понял. `/help` — список команд",
        "bot_hint": "Кнопки управления звонком есть в боте {bot} — нажмите там Start.",
        "help": (
            "**Шлюз между вашей АТС и Telegram**\n"
            "\n"
            "**Позвонить:** пришлите номер сообщением (`+7 999 123-45-67`, `101`, `*97`) — перезвоню вам "
            "и соединю. Или позвоните мне: наберу номер по умолчанию либо последний присланный.\n"
            "\n"
            "**В разговоре:** цифры, `*` и `#` уходят как DTMF (и звучат в трубке). "
            "Ещё один номер — второй вызов, текущий уйдёт на удержание. "
            "Добавить людей можно прямо в Telegram: звонок станет групповым, абонент АТС останется в нём.\n"
            "\n"
            "**Команды**\n"
            "`/hangup` — завершить текущий вызов\n"
            "`/switch` — переключиться между вызовами\n"
            "`/transfer` — соединить два вызова, `/transfer 101` — перевести на номер\n"
            "`/conf` — конференция на АТС, `/group` — групповой звонок в Telegram\n"
            "`/cb` — перезвонить последнему звонившему\n"
            "`/redial` — повторить последний набор\n"
            "`/dnd` — не беспокоить\n"
            "`/schedule` — расписание входящих\n"
            "`/line` — выбор линии\n"
            "`/history` — история звонков\n"
            "`/status` — состояние линий\n"
            "`/lang en` — switch to English"
        ),
        "alert_reg_down": "🔴 **{account}**: регистрация на АТС потеряна более {seconds} с\n{reason}",
        "alert_reg_ok": "🟢 **{account}**: регистрация на АТС восстановлена",
        "alert_telegram": "⚠️ Проблема Telegram ({what}): {reason}",
        "alert_call_failed": "⚠️ Звонок {user} → {peer} не состоялся: {reason}",
        "alert_started": "🚀 Шлюз запущен: {users} польз., {accounts} SIP-номер(ов)",
        "alert_stopped": "🛑 Шлюз остановлен",
        "rec_started": "🔴 Идёт запись разговора",
        "rec_stopped": "⏹ Запись остановлена",
        "rec_caption": "🎙 Запись разговора с {peer}, {duration}",
        "rec_failed": "❌ Не удалось отправить запись: {reason}",
        "rec_off": "Запись выключена в настройках линии (`calls.record`)",
        "conf_no_extension": "Конференция не настроена: задайте `calls.conference_extension`",
        "conf_started": "👥 Собираю конференцию в комнате `{room}`",
        "conf_joined": "👥 {peer} переведён в конференцию",
        "conf_failed": "❌ Не удалось собрать конференцию: {reason}",
        "group_joining": "👥 Подключаюсь к голосовому чату {chat}…",
        "conf_tg_starting": "👥 Создаю групповой звонок в Telegram…",
        "group_failed": "❌ Групповой звонок не удался: {reason}",
        "group_moved": "👥 В голосовом чате «{chat}»: {peers}",
        "group_moved_conference": "👥 В групповом звонке Telegram: {peers}",
        "group_invited": "📨 Приглашение в голосовой чат «{chat}» отправлено: {who}\nПримите звонок Telegram или войдите в чат группы.",
        "group_invited_conference": "📨 Позвал в групповой звонок Telegram: {who}\nПримите звонок в Telegram.",
        "group_invite_failed": "❌ Не удалось пригласить {who}: {reason}",
        "group_dial_failed": "❌ `{number}` в групповой звонок не завести: {reason}",
        "group_left_by": "👥 {peer} отключился от группового звонка",
        "group_ended": "👥 Групповой звонок завершён",
        "group_none": "Групповой звонок не запущен",
        "status_group": "👥 Голосовой чат «{chat}»: линий {n}",
        "status_group_conference": "👥 Групповой звонок Telegram: линий {n}",
        "cmd_group": "групповой звонок в Telegram",
        "filtered": "🔕 Звонок от {caller} не пропущен: {reason}",
        "filtered_forwarded": "↪️ Звонок от {caller} переведён на `{number}`: {reason}",
        "reason_quiet": "тихие часы",
        "reason_off_hours": "нерабочее время",
        "reason_blacklist": "номер в чёрном списке",
        "reason_whitelist": "номера нет в белом списке",
        "status_filtered": "🕑 Сейчас нерабочее время, звонки с АТС не приходят",
        "schedule_none": "Расписание не задано: звонки приходят круглосуточно.\nНастраивается в конфиге, раздел `schedule`.",
        "schedule_header": "**Расписание входящих**",
        "schedule_work": "🕘 Рабочие часы: `{windows}`",
        "schedule_quiet": "🌙 Тихие часы: `{windows}`",
        "schedule_blacklist": "⛔ Чёрный список: `{numbers}`",
        "schedule_whitelist": "✅ Белый список: `{numbers}`",
        "schedule_action": "Отфильтрованным отвечаем: {action}",
        "schedule_forward": "Отфильтрованные уходят на `{number}`",
        "schedule_open": "🟢 Сейчас звонки приходят",
        "schedule_closed": "🔕 Сейчас звонки не приходят",
        "cmd_schedule": "расписание входящих",
        "cmd_rec": "запись разговора",
        "cmd_conf": "конференция",
        "lang_name": "русский",
        "lang_current": "Язык: **русский**. Переключить: `/lang en`",
        "lang_switched": "✅ Язык интерфейса: **русский**",
        "lang_unknown": "Доступные языки: `ru`, `en`",
        "cmd_lang": "язык интерфейса",
        "cmd_status": "состояние линий и звонка",
        "cmd_hangup": "завершить текущий вызов",
        "cmd_switch": "переключиться между вызовами",
        "cmd_transfer": "перевести вызов",
        "cmd_cb": "перезвонить последнему звонившему",
        "cmd_redial": "повторить последний набор",
        "cmd_dnd": "не беспокоить",
        "cmd_line": "выбрать линию",
        "cmd_history": "история звонков",
        "cmd_help": "как пользоваться",
        "dir_in": "входящий",
        "dir_out": "исходящий",
        "res_answered": "разговор",
        "res_missed": "пропущен",
        "res_busy": "занято",
        "res_failed": "не удался",
        "res_declined": "отклонён",
        "res_transferred": "переведён",
        "res_blocked": "не пропущен",
        "btn_decline": "⛔ Отклонить",
        "btn_hangup": "📴 Завершить",
        "btn_switch": "🔁 Переключить",
        "btn_accept_waiting": "📞 Принять (удержать текущий)",
        "btn_transfer": "↪️ Соединить их",
        "btn_callback": "📲 Перезвонить",
        "btn_redial": "🔁 Повторить",
        "btn_dnd_on": "🔕 Не беспокоить",
        "btn_dnd_off": "🔔 Принимать звонки",
        "btn_keypad": "⌨️ Клавиатура",
        "btn_hide": "▲ Скрыть",
        "card_in_call": "🟢 Разговор с {peer}, **{duration}**",
        "card_dialing": "📞 Набираю `{number}`…",
        "card_ringing": "📞 Входящий: {caller}",
        "card_held": "⏸ На удержании: {peer}",
        "card_waiting": "📞 Ожидает: {caller}",
        "card_conference": "👥 Групповой звонок Telegram, участников: {n}",
        "card_key": "🔐 Ключ: {emojis}",
        "tg_conference": "👥 Звонок стал групповым. Абонент АТС остаётся в нём и слышит всех, команды работают как прежде.",
        "tg_conference_failed": "❌ Не удалось перейти в групповой звонок, абонент АТС отключён",
        "taken_by": "📞 Вызов от {caller} принял {name}",
        "callback_failed": "❌ Не смог позвонить вам в Telegram: {reason}",
        "missed_reason": "Не дозвонился: {reason}",
        "tg_privacy": "звонки от аккаунта шлюза запрещены в настройках конфиденциальности "
                      "(Настройки → Конфиденциальность → Звонки → Исключения)",
        "tg_blocked": "аккаунт шлюза у вас в чёрном списке",
        "tg_no_media": "звонок не соединился, серверы звонков Telegram недоступны",
        "tg_flood": "Telegram временно ограничил звонки с аккаунта шлюза",
        "action_unavailable": "«нет ответа» (480)",
        "action_busy": "«занято» (486)",
        "action_reject": "«отказ» (603)",
        "sip_403": "станция запретила вызов",
        "sip_404": "номер не найден",
        "sip_408": "нет ответа",
        "sip_410": "номер больше не обслуживается",
        "sip_480": "абонент недоступен",
        "sip_484": "номер набран не полностью",
        "sip_486": "занято",
        "sip_487": "вызов отменён",
        "sip_488": "нет общего кодека",
        "sip_503": "линия недоступна",
        "sip_600": "занято",
        "sip_603": "абонент отклонил вызов",
        "sip_604": "номер не существует",
    },
    "en": {
        "incoming": "📞 **Incoming:** {caller}\nLine {account}",
        "incoming_waiting": "📞 **Second incoming call:** {caller}\n`/switch` accepts it, current call on hold\n`/decline` rejects it",
        "missed": "📵 **Missed:** {caller}\nLine {account}. Call back: `/cb`",
        "waiting_missed": "📵 The second call from {caller} was not accepted",
        "call_ended": "📴 Call with {peer} ended, **{duration}**",
        "call_ended_short": "📴 Ended",
        "dialing_callback": "📲 Calling you; after you answer I dial `{number}`",
        "dialing_direct": "📞 Dialing `{number}`, calling you back now",
        "dialing_consult": "📞 Dialing `{number}`, {held} is on hold.\n`/switch` goes back, `/transfer` connects them together",
        "dial_failed": "❌ `{number}`: {reason}",
        "dial_error": "❌ Could not start the call: {reason}",
        "not_a_number": "That does not look like a number: {text}",
        "need_number": "Send a number first, then call me.\nOr set `default_destination` for your line.",
        "no_active_call": "No active call.",
        "no_waiting": "No waiting call.",
        "nothing_to_switch": "Nothing to switch to: there is no second call.",
        "switched": "🔁 Talking to {peer}. On hold: {held}",
        "resumed": "▶️ Resumed the call with {peer}",
        "declined": "Call from {caller} declined",
        "transfer_done": "✅ {a} and {b} are connected, you left the call",
        "transfer_started": "↪️ Transferring {peer} to `{number}`…",
        "transfer_failed": "❌ Transfer failed ({reason}), the call continues",
        "transfer_usage": "`/transfer <number>` transfers the other party.\n`/transfer` with no number connects the active and held calls.",
        "dnd_on": "🔕 **Do not disturb on:** PBX calls get busy. Turn off: `/dnd`",
        "dnd_off": "🔔 **Do not disturb off**",
        "busy_line": "⛔ You are already in a call. Send a number and I dial it, holding the current call.",
        "busy_gateway": "⛔ All gateway channels are busy ({n}), try again later",
        "reconnecting": "⚠️ Telegram connection dropped, the other party is on hold. Calling you back…",
        "reconnect_failed": "❌ Could not restore the call, it was ended",
        "reconnected": "✅ Call restored",
        "line_current": "Line for outgoing calls: **{account}**",
        "line_list": "Your lines: {lines}\nChoose: `/line <name>`",
        "line_unknown": "No such line: {name}",
        "no_line": "You have no line for outgoing calls",
        "dtmf_sent": "⌨️ `{digits}`",
        "history_empty": "No calls yet",
        "history_header": "**Recent calls**",
        "status_header": "**Gateway:** {gateway}",
        "status_account": "{icon} {name} (`{username}@{domain}`): {state}",
        "status_active": "🟢 In call with {peer} ({direction}), **{duration}**",
        "status_held": "⏸ On hold: {peer}",
        "status_waiting": "📞 Waiting: {caller}",
        "status_idle": "No active calls",
        "status_dnd": "🔕 Do not disturb is on",
        "registered": "registered",
        "unregistered": "**NOT registered**",
        "unknown_command": "Unknown command. `/help` lists them",
        "bot_hint": "Call control buttons live in the bot {bot}, press Start there.",
        "help": (
            "**Gateway between your PBX and Telegram**\n"
            "\n"
            "**To call:** send a number (`+1 555 0100`, `101`, `*97`) and I call you back and connect you. "
            "Or call me: I dial the default destination or the last number you sent.\n"
            "\n"
            "**During a call:** digits, `*` and `#` are sent as DTMF (and you hear them). "
            "Another number starts a second call and holds the current one. "
            "You can add people right in Telegram: the call becomes a group call and the PBX party stays in it.\n"
            "\n"
            "**Commands**\n"
            "`/hangup` ends the current call\n"
            "`/switch` toggles between calls\n"
            "`/transfer` connects the two calls, `/transfer 101` transfers to a number\n"
            "`/conf` starts a conference on the PBX, `/group` a Telegram group call\n"
            "`/cb` calls back the last caller\n"
            "`/redial` repeats the last number\n"
            "`/dnd` toggles do not disturb\n"
            "`/schedule` shows the incoming call schedule\n"
            "`/line` chooses a line\n"
            "`/history` shows the call log\n"
            "`/status` shows the state of the lines\n"
            "`/lang ru` переключает на русский"
        ),
        "alert_reg_down": "🔴 **{account}**: SIP registration lost for more than {seconds}s\n{reason}",
        "alert_reg_ok": "🟢 **{account}**: SIP registration is back",
        "alert_telegram": "⚠️ Telegram problem ({what}): {reason}",
        "alert_call_failed": "⚠️ Call {user} → {peer} failed: {reason}",
        "alert_started": "🚀 Gateway started: {users} user(s), {accounts} SIP account(s)",
        "alert_stopped": "🛑 Gateway stopped",
        "rec_started": "🔴 Recording this call",
        "rec_stopped": "⏹ Recording stopped",
        "rec_caption": "🎙 Recording of the call with {peer}, {duration}",
        "rec_failed": "❌ Could not send the recording: {reason}",
        "rec_off": "Recording is disabled for this line (`calls.record`)",
        "conf_no_extension": "Conferencing is not configured: set `calls.conference_extension`",
        "conf_started": "👥 Building a conference in room `{room}`",
        "conf_joined": "👥 {peer} moved into the conference",
        "conf_failed": "❌ Could not build the conference: {reason}",
        "group_joining": "👥 Joining the voice chat of {chat}…",
        "conf_tg_starting": "👥 Starting a Telegram group call…",
        "group_failed": "❌ The group call failed: {reason}",
        "group_moved": "👥 In the voice chat \"{chat}\": {peers}",
        "group_moved_conference": "👥 In the Telegram group call: {peers}",
        "group_invited": "📨 Invitation to the voice chat \"{chat}\" sent to: {who}\nAccept the Telegram call or open the group.",
        "group_invited_conference": "📨 Invited to the Telegram group call: {who}\nAccept the call in Telegram.",
        "group_invite_failed": "❌ Could not invite {who}: {reason}",
        "group_dial_failed": "❌ Could not bring `{number}` into the group call: {reason}",
        "group_left_by": "👥 {peer} left the group call",
        "group_ended": "👥 The group call is over",
        "group_none": "No group call is running",
        "status_group": "👥 Voice chat \"{chat}\": {n} line(s)",
        "status_group_conference": "👥 Telegram group call: {n} line(s)",
        "cmd_group": "Telegram group call",
        "filtered": "🔕 The call from {caller} was not put through: {reason}",
        "filtered_forwarded": "↪️ The call from {caller} was forwarded to `{number}`: {reason}",
        "reason_quiet": "quiet hours",
        "reason_off_hours": "outside working hours",
        "reason_blacklist": "the number is blacklisted",
        "reason_whitelist": "the number is not whitelisted",
        "status_filtered": "🕑 Outside working hours right now, PBX calls do not ring",
        "schedule_none": "No schedule is set: calls ring around the clock.\nConfigure it in the `schedule` section.",
        "schedule_header": "**Incoming call schedule**",
        "schedule_work": "🕘 Working hours: `{windows}`",
        "schedule_quiet": "🌙 Quiet hours: `{windows}`",
        "schedule_blacklist": "⛔ Blacklist: `{numbers}`",
        "schedule_whitelist": "✅ Whitelist: `{numbers}`",
        "schedule_action": "Filtered calls get: {action}",
        "schedule_forward": "Filtered calls go to `{number}`",
        "schedule_open": "🟢 Calls ring right now",
        "schedule_closed": "🔕 Calls do not ring right now",
        "cmd_schedule": "incoming call schedule",
        "cmd_rec": "record the call",
        "cmd_conf": "conference",
        "lang_name": "English",
        "lang_current": "Language: **English**. Switch: `/lang ru`",
        "lang_switched": "✅ Interface language: **English**",
        "lang_unknown": "Available languages: `ru`, `en`",
        "cmd_lang": "interface language",
        "cmd_status": "state of the lines and calls",
        "cmd_hangup": "end the current call",
        "cmd_switch": "toggle between calls",
        "cmd_transfer": "transfer the call",
        "cmd_cb": "call back the last caller",
        "cmd_redial": "repeat the last number",
        "cmd_dnd": "do not disturb",
        "cmd_line": "choose a line",
        "cmd_history": "call log",
        "cmd_help": "how to use it",
        "dir_in": "incoming",
        "dir_out": "outgoing",
        "res_answered": "answered",
        "res_missed": "missed",
        "res_busy": "busy",
        "res_failed": "failed",
        "res_declined": "declined",
        "res_transferred": "transferred",
        "res_blocked": "filtered",
        "btn_decline": "⛔ Decline",
        "btn_hangup": "📴 Hang up",
        "btn_switch": "🔁 Switch",
        "btn_accept_waiting": "📞 Accept (hold current)",
        "btn_transfer": "↪️ Connect them",
        "btn_callback": "📲 Call back",
        "btn_redial": "🔁 Redial",
        "btn_dnd_on": "🔕 Do not disturb",
        "btn_dnd_off": "🔔 Accept calls",
        "btn_keypad": "⌨️ Keypad",
        "btn_hide": "▲ Hide",
        "card_in_call": "🟢 In call with {peer}, **{duration}**",
        "card_dialing": "📞 Dialing `{number}`…",
        "card_ringing": "📞 Incoming: {caller}",
        "card_held": "⏸ On hold: {peer}",
        "card_waiting": "📞 Waiting: {caller}",
        "card_conference": "👥 Telegram group call, {n} participant(s)",
        "card_key": "🔐 Key: {emojis}",
        "tg_conference": "👥 The call became a group call. The PBX party stays in it and hears everyone; commands work as before.",
        "tg_conference_failed": "❌ Could not move into the group call, the PBX party was disconnected",
        "taken_by": "📞 {name} took the call from {caller}",
        "callback_failed": "❌ Could not call you in Telegram: {reason}",
        "missed_reason": "Could not reach you: {reason}",
        "tg_privacy": "your privacy settings do not allow calls from the gateway account "
                      "(Settings → Privacy → Calls → Exceptions)",
        "tg_blocked": "you have blocked the gateway account",
        "tg_no_media": "the call did not connect, Telegram call servers are unreachable",
        "tg_flood": "Telegram temporarily limited calls from the gateway account",
        "action_unavailable": "\"no answer\" (480)",
        "action_busy": "\"busy\" (486)",
        "action_reject": "\"declined\" (603)",
        "sip_403": "the PBX refused the call",
        "sip_404": "number not found",
        "sip_408": "no answer",
        "sip_410": "the number is no longer in service",
        "sip_480": "unavailable",
        "sip_484": "incomplete number",
        "sip_486": "busy",
        "sip_487": "call cancelled",
        "sip_488": "no codec in common",
        "sip_503": "line unavailable",
        "sip_600": "busy",
        "sip_603": "declined",
        "sip_604": "the number does not exist",
    },
}


LANGUAGES = tuple(STRINGS)


def set_language(lang: str) -> None:
    """Sets the fallback language for users who have not picked one themselves."""
    global _LANG
    _LANG = lang if lang in STRINGS else "ru"


def default_language() -> str:
    return _LANG


def t(key: str, lang: str | None = None, **kw) -> str:
    strings = STRINGS.get(lang or _LANG) or STRINGS[_LANG]
    text = strings.get(key) or STRINGS["ru"].get(key) or key
    safe = {k: (v.replace("`", "'").replace("**", "") if isinstance(v, str) else v) for k, v in kw.items()}
    try:
        return text.format(**safe)
    except (KeyError, IndexError):
        return text


def fmt_number(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    if len(digits) == 10 and number.startswith("+1"):
        return f"+1 {digits[:3]} {digits[3:6]}-{digits[6:]}"
    return number or "?"


def fmt_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_time(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))
