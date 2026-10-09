# SIPgram

[![ci](https://github.com/vasmarfas/sipgram/actions/workflows/ci.yml/badge.svg)](https://github.com/vasmarfas/sipgram/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue)](LICENSE)

**[Русская версия](README.ru.md)**

A self-hosted SIP to Telegram voice gateway. SIPgram registers on your FreePBX/Asterisk as ordinary extensions and
turns calls into **regular Telegram calls**: peer to peer voice calls, not group chats and not voice messages.
One gateway account serves every user at the same time.

* An extension rings, and the gateway calls its owner in Telegram after sending the caller name and number to the chat.
* Send a number to the chat, and the gateway calls you back and dials it through the PBX.
* Call the gateway account yourself, and it dials the default destination (an IVR, a receptionist) or the last number you sent.
* During a call: DTMF, a second call with the first one on hold (the PBX plays music), switching, transfer,
  automatic reconnect when the Telegram call drops.
* Routing, recording, queues, follow-me and CDR stay on the PBX. To Asterisk this is just a set of SIP phones.
* Call recordings arrive in the chat as a voice message and are never written to disk.
* Opus at 48 kHz on the SIP side, so wideband audio survives all the way from Telegram to the extension.
* Conference through the PBX, HTTP API for CRM systems, admin alerts, several gateway accounts.
* Incoming call screening: work hours, quiet hours, blacklist, whitelist, forwarding of filtered calls back to the PBX.
* Telegram group call: the other party adds people right in the call, or `/group` moves the conversation into a
  group voice chat or a Telegram conference call that needs no group. The PBX party stays in the conversation.
* Optional bot with buttons: DTMF keypad, hold and switch, transfer, call back. A call takes one card message in the
  chat: incoming, in progress, outcome.
* Any Telegram client can answer, Telegram Web included.
* Runs on Linux (Docker or native) and on Windows, with no compilation: `pip install` is enough.

```
 PSTN / SIP trunk ──► FreePBX ──SIP/RTP (Opus, G.711)──► SIPgram ──Telegram P2P (MTProto)──► users' Telegram
                      Asterisk                          Python: own SIP stack + NTgCalls + Telethon
```

## Contents

* [Requirements](#requirements)
* [Quick start with Docker](#quick-start-with-docker)
* [Quick start without Docker](#quick-start-without-docker)
* [Telegram side](#telegram-side)
* [PBX side](#pbx-side)
* [Configuration](#configuration)
* [Using it](#using-it)
* [Several users](#several-users)
* [HTTP API](#http-api)
* [Windows](#windows)
* [Diagnostics](#diagnostics)
* [Tests](#tests)
* [How it is built](#how-it-is-built)
* [Limitations](#limitations)
* [License](#license)

## Requirements

1. **A separate Telegram account for the gateway**, on any number that can receive an SMS. Telegram cannot call
   itself, so the gateway needs an account of its own. One account is enough for everybody: it holds up to
   `telegram.max_calls` conversations in parallel.
2. `api_id` and `api_hash` from <https://my.telegram.org> (API development tools).
3. An extension with a password on the PBX, one per user.
4. The Telegram id of each user (ask `@userinfobot`) or their @username.
5. Optionally a bot token from `@BotFather` for the buttons.
6. Linux with glibc 2.28 or newer (Debian 10+, Ubuntu 20.04+, not Alpine) or Windows 10/11 x64, Python 3.10 to 3.14.
   Or Docker, which covers the Linux case.

## Quick start with Docker

```bash
git clone https://github.com/vasmarfas/sipgram.git && cd sipgram
mkdir -p config sessions
sudo chown -R 1000:1000 config sessions
cp config.example.yaml config/config.yaml
cp .env.example .env
docker compose build
docker compose run --rm sipgram login
docker compose run --rm sipgram whoami
docker compose run --rm sipgram check
docker compose up -d && docker compose logs -f
```

Fill in `api_id`, `api_hash`, `users`, `sip.server` and `sip.accounts` in `config/config.yaml`, and the extension
passwords in `.env`, before the `login` step. `login` asks for the phone number of the gateway account, the code
from the SMS and the 2FA password, and has to be done once. `whoami` shows the session, the resolved users and the
bot; `check` registers every extension once.

The container runs as uid 1000, so the two bind-mounted directories have to belong to it, hence the `chown`.
Without it the first `login` fails with `Permission denied` when it tries to write the session file.

A prebuilt image is published on every push to the default branch, for `linux/amd64` and `linux/arm64`:

```bash
docker pull ghcr.io/vasmarfas/sipgram:latest
```

To use it instead of a local build, replace `build: .` with `image: ghcr.io/vasmarfas/sipgram:latest` in
`docker-compose.yml`.

The container runs with `network_mode: host`, so the SIP ports of the extensions (5070, 5071, and so on) and the
RTP range (`rtp_port_min` to `rtp_port_max`, 40000 to 40200/udp by default) have to be reachable from the PBX.
`docker compose ps` shows the health state, which comes from `sipgram status`.

## Quick start without Docker

```bash
python -m venv venv
. venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
export SIP_PASSWORD_491=...
sipgram login
sipgram whoami
sipgram check
sipgram run
```

On Windows, activate the environment with `venv\Scripts\activate` and set the password with
`$env:SIP_PASSWORD_491 = "..."`; see [Windows](#windows) for the rest of the platform notes.

Use `sipgram -c path/to/config.yaml <command>` when the config is not in the current directory, and
`sipgram -v --sip-trace run` for a verbose log.

| Command | What it does |
|---|---|
| `sipgram run` | Start the gateway. |
| `sipgram login [gateway]` | Interactive Telegram login, once per gateway account. |
| `sipgram whoami` | Show the sessions, resolve the configured users, check the bot. |
| `sipgram check [account]` | Register each extension once and report the result. |
| `sipgram doctor [--offline]` | Check config, directories, ports, DNS, registration, sessions, libopus, schedule. |
| `sipgram status` | Health of a running gateway, read from the state file. Exit code 0 means healthy. |

## Telegram side

* Every user adds the gateway account to their contacts, or allows calls from everyone (Settings, Privacy and
  Security, Calls). Otherwise Telegram rejects the call with `USER_PRIVACY_RESTRICTED`: the PBX gets 480 and the user
  gets a message in the chat saying what to change.
* The gateway adds the users to its own contacts so that it can accept their calls.
* Give the gateway account a recognisable name and avatar, for example "Office". Telegram calls will show up as
  coming from it.
* If `bot_token` is set, each user presses Start in the bot once. After that notifications and buttons come from
  the bot while the calls themselves still come from the gateway account. Without a bot everything works through
  text commands.
* Do not use a freshly registered account for the gateway. Telegram restricts new accounts.

## PBX side

To the PBX the gateway is a set of ordinary SIP phones: every extension in `sip.accounts` registers separately.
The instructions below are for FreePBX 15/16/17 (Asterisk 16 to 22, chan_pjsip). A plain `pjsip.conf` is at the end.

### An extension per user

Applications, Extensions, Add Extension, **Add New SIP [chan_pjsip] Extension**.

| Field | Value |
|---|---|
| User Extension | the number, for example `491` |
| Display Name | the owner's name, shown on the PBX for calls coming from Telegram |
| Secret | a long password, the same one that goes into `password` or into `.env` as `SIP_PASSWORD_491` |

On the **Advanced** tab:

| Field | Value | Why |
|---|---|---|
| Max Contacts | 1 | one registration per extension |
| Codecs (Allowed) | `opus`, `ulaw`, `alaw`, optionally `slin16` | the order sets the priority |
| DTMF Signaling | RFC 4733 | matches the default `dtmf: rfc2833` |
| Direct Media | No | RTP always goes through Asterisk |
| Rewrite Contact | Yes | Asterisk answers to the real address and port of the gateway behind NAT |
| RTP Symmetric | Yes | RTP goes back where it came from |
| Force rport | Yes | |
| Qualify Frequency | 60 | OPTIONS pings, which SIPgram answers |
| Allow Transfer | Yes | needed for `/transfer <number>` (REFER) |
| Call Waiting | Yes | a second call reaches the gateway; with No the PBX answers busy itself |
| Music on Hold Class | any | this is what the other party hears while on hold |

Voicemail, Follow Me and ring time are up to you. They kick in when Telegram does not answer within
`ring_timeout`: the PBX gets 480 and continues down its own chain. Submit, then Apply Config.

In `config.yaml`:

```yaml
sip:
  server: pbx.example.com
  accounts:
    - {username: "491", password: "${SIP_PASSWORD_491}", user: 123456789}
    - {username: "492", password: "${SIP_PASSWORD_492}", user: "@petya"}
```

### Network

* Gateway next to the PBX, same network or same host: nothing else to configure. On a FreePBX host port 5060 is
  taken by Asterisk, so the accounts use their own ports starting at `local_port: 5070`.
* Gateway behind NAT, PBX on the internet: Rewrite Contact and RTP Symmetric on the extension are usually enough.
  Set `sip.public_ip` if you want the address advertised explicitly. Forward `local_port`/udp and the
  `rtp_port_min` to `rtp_port_max` range, or rely on symmetric RTP, since the gateway sends RTP first.
* PBX behind NAT: Settings, Asterisk SIP Settings, NAT Settings, External Address and Local Networks. Without them
  the SDP from the PBX carries a private address and there is no audio.
* FreePBX firewall and fail2ban: put the gateway address into Connectivity, Firewall, Networks as Local or Trusted,
  otherwise it can get banned after a few re-registrations.
* Transport: UDP by default. For TCP or TLS enable the transport in Asterisk SIP Settings and set
  `transport: tcp|tls` with `port: 5061` for TLS. Use `tls_verify: false` for a self-signed certificate.
* Legacy chan_sip listens on a different port (usually 5160 in FreePBX). The extensions in `sip.accounts` must live
  in the driver whose port is in `sip.port`.

### Incoming calls

Point the calls at the user's extension the usual way:

* Connectivity, Inbound Routes, Set Destination, Extensions, 491;
* a Ring Group or Queue built from such extensions: Telegram calls every owner and the first one to answer takes
  the call, the PBX clears the rest;
* Follow Me on a primary extension with 491 added.

While Telegram rings, the PBX has 180 Ringing and plays its own ringback to the caller. A decline in Telegram
becomes 603, busy becomes 486, no answer becomes 480. FreePBX handles all of them normally.

One extension for several people (a family, a duty shift): `users: [id1, id2]` with `ring_all: true` calls
everybody at once and the first to answer takes the call. Without `ring_all` only the first user in the list rings.

### Outgoing calls

The gateway sends an INVITE to `sip:<number>@<domain>` as the user's extension, so Outbound Routes and the
extension permissions (context `from-internal`) apply as usual. The outbound caller ID is whatever the extension has.

The number you send goes through `calls.dial_rules`. By default spaces, brackets and dashes are stripped, a leading
`+` is removed and `8XXXXXXXXXX` becomes `7XXXXXXXXXX`. Adjust them to your Outbound Routes.

The INVITE carries `X-TG-User-Id` and `X-TG-User-Name`, which are handy in the dialplan
(`${PJSIP_HEADER(read,X-TG-User-Id)}`) for CDR or routing.

### Hold, second call, transfer

All of it is plain SIP, and apart from Allow Transfer and Call Waiting nothing extra has to be enabled:

* hold is a re-INVITE with `a=sendonly`, and Asterisk starts the extension's MOH class for the other party;
* a second incoming call rings the same extension, the gateway answers 180 and waits `ring_timeout` seconds for
  the user to decide;
* `/transfer <number>` sends REFER, and Asterisk connects the other party itself and drops the gateway leg;
* `/transfer` with two calls bridges them inside the gateway, so both stay its channels and the CDR continues.

### Gateway mode: calling arbitrary Telegram users from the PBX

Add a shared account:

```yaml
sip:
  accounts:
    - {username: "tg-trunk", password: "${SIP_PASSWORD_TRUNK}", shared: true}
```

Calls arriving on it are routed by the dialled number (the user part of the Request-URI):

| Dialled | Reaches |
|---|---|
| `+79991234567` | the Telegram user with that phone number, imported as a contact; the number must be visible |
| `tg#username` | that @username |
| `123456789` (6 digits or more) | that numeric Telegram id |

The target must be listed in `users`. For the PBX to pass such a number through you need a trunk rather than an
extension: Connectivity, Trunks, Add Trunk, chan_pjsip.

* Trunk Name `sipgram`; pjsip Settings, General: Authentication None, Registration None, SIP Server is the gateway
  address, SIP Server Port is the `local_port` of the shared account, Context `from-internal`.
* Advanced: From User `tg-trunk`, codecs ulaw and alaw.
* An Outbound Route with a dial pattern like `tg#.` or `+X.` pointing at the `sipgram` trunk.

Users without an extension of their own dial out through the shared account. The gateway takes new calls and SIP
MESSAGEs only from the `sip.server` address and the networks in `sip.allow_from`, and drops the rest with a warning
in the log. It still does not authenticate them, so do not expose its SIP ports to the internet without a firewall.

### Plain Asterisk without FreePBX

```ini
; pjsip.conf
[491]
type=endpoint
context=from-internal
disallow=all
allow=opus,alaw,ulaw
auth=491
aors=491
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
allow_transfer=yes
moh_suggest=default
callerid="John" <491>

[491]
type=auth
auth_type=userpass
username=491
password=A_VERY_LONG_PASSWORD

[491]
type=aor
max_contacts=1
qualify_frequency=60
```

```ini
; extensions.conf
[from-internal]
exten => 491,1,Dial(PJSIP/491,40)
 same => n,Hangup()
```

The integration tests run against exactly this configuration, see `tests/integration/asterisk/`.

## Configuration

The annotated example is [config.example.yaml](config.example.yaml). The minimum is:

```yaml
telegram:
  api_id: 12345
  api_hash: "0123456789abcdef0123456789abcdef"
  session: gateway
users:
  - {id: 123456789, name: John}
sip:
  server: pbx.example.com
  accounts:
    - {username: "491", password: "${SIP_PASSWORD_491}", user: 123456789}
```

| Key | Meaning |
|---|---|
| `telegram.max_calls` | how many conversations the gateway account holds at once (10 by default) |
| `telegram.bot_token` | bot token for the buttons; empty means text commands only |
| `telegram.gateways` | additional gateway accounts; `users[].gateway` pins a user to one of them |
| `telegram.language`, `users[].language` | starting interface language (`ru` or `en`); each user switches it with `/lang` and the choice is remembered |
| `users[]` | id, @username or +phone, plus `name` and `can_call: false` for receive-only users |
| `sip.*` | defaults for every account: server, port, transport (`udp`/`tcp`/`tls`), codecs, RTP ports, `public_ip`, `allow_from` (addresses and networks besides `sip.server` that may place calls) |
| `sip.accounts[].user` / `users` | owners of the extension; the first one takes incoming calls, `ring_all: true` rings everybody and the first to answer wins |
| `sip.accounts[].shared` | shared trunk account: the PBX dials `tg#username`, `+7999...` or an id through it, and users without their own extension dial out through it |
| `sip.accounts[].ring_timeout` | how long Telegram rings before the PBX gets 480 and follows its own logic |
| `sip.accounts[].default_destination` | where to connect when the owner calls the gateway without sending a number |
| `calls.outgoing_mode` | `callback` (the gateway calls you first, then dials) or `direct` (dials immediately) |
| `calls.call_waiting` | a second incoming call during a conversation: ring and offer `/switch`; `false` answers busy |
| `calls.reconnect_timeout` | when Telegram drops, hold the other party and call back for this many seconds |
| `calls.dial_rules` | regex rewrites for the numbers you send |
| `calls.ringback` | ringback tone while the PBX dials: `ru`, `us`, `none` |
| `calls.record` | `off`, `ask` (turned on with `/rec`) or `all`; the recording is sent as a voice message and not stored |
| `calls.conference_extension` | ConfBridge room on the PBX, enables `/conf` |
| `calls.group_chat` | Telegram group whose voice chat `/group` uses; empty means a Telegram conference call with no group |
| `schedule.*` | work and quiet hours, blacklist and whitelist, what happens to a filtered call; empty by default, so nothing is filtered |
| `users[].schedule` | the same for one user; missing keys fall back to the global section |
| `notifications.admin` | who gets told about lost registrations, Telegram problems, start and stop |
| `api.*` | HTTP API for CRM systems and scripts, see [HTTP API](#http-api) |

Any value of the form `${VAR}` is taken from the environment, so secrets can stay out of the YAML (`.env` for
Docker). The old v0.1 config format (`lines:`) is still read and migrated on the fly, with a note in the log.

## Using it

Everything is controlled from the chat with the gateway account, or from the bot, which offers the same actions as
buttons.

| Action | How |
|---|---|
| Incoming call from the PBX | A message with the caller name and number, then a Telegram call. No answer within `ring_timeout` gives the PBX 480, so voicemail and follow-me work; declining gives 603. With the bot this is one card: incoming, in progress, and the outcome with a "Call back" button. |
| Call out | Send a number (`+7 999 123-45-67`, `8903...`, `101`, `sip:user@host`). The gateway calls you back and dials. While the PBX is dialling you hear a ringback or the early media of the PBX. With several extensions, `/line` shows and picks the line and `491: 101` dials through a specific one. |
| Call the gateway | Connects you to the `default_destination` of your line, or to the last number you sent within `pending_number_ttl`. Inviting the gateway into a Telegram group call works the same way: it joins and brings the number in. |
| DTMF | During a call send digits, `*` or `#` (`1`, `123#`) or use `/dtmf 123`; the bot has a keypad. You hear the tone yourself and the gateway echoes the digits back as a message. By default the digits go out of band (RFC 4733), so a human on the other end does not hear them; set `dtmf: inband` if they should. Codes like `*1...` are intercepted by the PBX itself, see [Diagnostics](#diagnostics). |
| Feature codes | Outside a call send the code (`*97`, `*43`) and the gateway dials it as an ordinary number, with no in-call interception. |
| Second incoming call | You get a "second incoming call" message: `/switch` accepts it and puts the current one on hold with MOH from the PBX, `/decline` rejects it. `/switch` then toggles between the two, `/hangup` ends the current one and brings back the held one. |
| Second outgoing call | `/call 102` during a conversation holds the current call and dials 102. Then `/switch` or `/transfer`. |
| Transfer | `/transfer` with two calls connects them to each other and drops you (attended). `/transfer 102` transfers the other party to 102 through REFER (blind). |
| Hang up | Hang up in Telegram to end everything, or `/hangup` for the current call only. |
| Connection lost | If the Telegram call drops on its own, the other party goes on hold and the gateway calls you back for up to `reconnect_timeout` seconds. |
| Add people | Tap "Add participant" in the Telegram call. The call becomes a group call, the gateway moves into it together with the PBX party and the card shows the number of participants. When nobody but the gateway is left, the PBX leg is hung up. |
| Encryption key | The call card shows four emoji. If they match the emoji on the Telegram call screen, you and the gateway share the key and nobody sits in the middle. |
| `/cb`, `/redial` | Call the last caller back, repeat the last number dialled. |
| `/dnd` | Do not disturb: incoming PBX calls get busy. |
| Schedule | `/schedule` shows your work and quiet hours, the number lists and whether you are taking calls right now. It is configured in `schedule` and empty by default. |
| `/status`, `/history`, `/help` | State of the lines and calls, the last 10 calls, the command list. |
| Language | `/lang en` or `/lang ru` switches the interface for you only, and the choice survives restarts. |
| Recording | `/rec` toggles recording of the current conversation. When it stops, or the call ends, the recording arrives as a voice message. Nothing is written to disk. With `calls.record: all` every conversation is recorded. |
| Conference | `/conf` moves the active and held calls into the ConfBridge room on the PBX and joins you to it. `/conf 101` also dials 101 into the same room. |
| Group call | `/group` moves the conversation into a Telegram group call: the voice chat of `calls.group_chat`, or a new Telegram conference call when no group is set. The PBX legs stay on the gateway and the Telegram call is replaced by an invitation. `/group @petya` also invites a person, `/group 101` brings in an extension, `/group stop` ends everything and `/hangup` ends only your lines. |

FreePBX feature codes (`##` for transfer, `*2` for parking) work through DTMF as well when they are enabled for the
extension.

## Several users

Give each person their own extension in `sip.accounts` with a `user:`. Conversations of different users run in
parallel through one gateway account, and each user has one conversation plus a held call and a waiting one.
For one extension shared by several people use `users: [...]` with `ring_all: true`: everyone rings, the first to
answer takes the call and the rest see a missed call. To call arbitrary Telegram users from the PBX, use a shared
account, see [Gateway mode](#gateway-mode-calling-arbitrary-telegram-users-from-the-pbx).

## HTTP API

Disabled by default. Turn it on in `config.yaml`:

```yaml
api:
  enabled: true
  bind: 127.0.0.1
  port: 8080
  token: "${SIPGRAM_API_TOKEN}"
  allow: ["10.0.0.0/8"]
  rate_limit: 120
```

With `network_mode: host` in Docker the port is opened on the host itself, so keep `bind: 127.0.0.1` unless you
really want it exposed.

Every request except `/api/health` needs the token, as `Authorization: Bearer <token>` or `X-Api-Token`. It is
compared in constant time. A wrong token gives 401, an address outside `allow` gives 403 and going over
`rate_limit` gives 429. All refusals are logged with the client address.

| Method and path | What it does |
|---|---|
| `GET /api/health` | No token. `{"ok": true, "accounts": {...}}`, or 503 when an account is not registered. Suitable for monitoring. |
| `GET /api/status` | Gateway accounts with their load, SIP accounts with their registration state, all active conversations. |
| `GET /api/users` | Users: id, language, do-not-disturb, their lines and gateway account. |
| `GET /api/history?user=<id>&limit=20` | Call history of one user. |
| `POST /api/calls` | Click to call. Body: `{"user": 123, "number": "+7 999 ...", "account": "491"}`, where `account` is optional. |
| `POST /api/calls/<user>/hangup` | End the user's current conversation. |
| `POST /api/calls/<user>/dtmf` | `{"digits": "123#"}` into the active conversation. |
| `POST /api/calls/<user>/transfer` | `{"number": "101"}`, transfer through REFER. |
| `POST /api/messages` | `{"user": 123, "text": "..."}`, a message to the user from the gateway. |

`user` is the numeric id or the same value as in `users[].id`.

```bash
curl -X POST http://127.0.0.1:8080/api/calls \
  -H "Authorization: Bearer $SIPGRAM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user": 123456789, "number": "+7 999 123-45-67"}'
```

From there it behaves like sending a number to the chat: the gateway calls the user in Telegram and connects them.

Response codes: 200 done, 400 bad body, 401 token, 403 address, 404 no such user, 409 impossible in the current
state (no active call, line busy), 429 rate limit, 500 internal error, which is logged with a traceback.

## Windows

SIPgram runs as an ordinary console application: Python 3.10 to 3.14 x64, `pip install`, no compilers, because the
`ntgcalls` wheel for Windows x64 is on PyPI. Tested on Windows 11 with Python 3.14.

```powershell
winget install Python.Python.3.12
cd C:\sipgram
python -m venv venv
venv\Scripts\activate
pip install -e .
copy config.example.yaml config.yaml
$env:SIP_PASSWORD_491 = "secret"
sipgram login
sipgram check
sipgram run
```

On the first run Windows Firewall asks about `python.exe`. Allow it for private networks. If the PBX is on another
network, open `local_port`/udp and the `rtp_port_min` to `rtp_port_max`/udp range on the host.

To run it as a service, either use Task Scheduler (trigger At startup, action
`C:\sipgram\venv\Scripts\sipgram.exe -c C:\sipgram\config.yaml run`, "Run whether user is logged on or not",
passwords in system-level environment variables) or [NSSM](https://nssm.cc):
`nssm install sipgram C:\sipgram\venv\Scripts\sipgram.exe -c C:\sipgram\config.yaml run`, with a stdout file on
the I/O tab or `logging.file` in the config.

Platform notes:

* `timeBeginPeriod(1)` is called at startup. Without it the 15.6 ms system timer breaks 20 ms RTP packetisation.
* On shutdown the process ends through `TerminateProcess`, because ntgcalls blocks a normal interpreter exit on
  Windows. The Telegram sessions and the SIP registration are closed properly before that: the de-registration is
  sent first.
* Docker Desktop is not recommended for running the gateway on Windows: `network_mode: host` is not available
  there and RTP through its NAT is unreliable. Running natively is simpler.

## Diagnostics

`sipgram doctor` checks everything at once: config, directory permissions, SIP and RTP ports, DNS, registration on
the PBX, Telegram sessions, the bot, libopus and the schedule. Exit code 0 means nothing is broken. Use
`--offline` to skip the network checks, or `docker compose run --rm sipgram doctor` in Docker.

For a detailed log run `sipgram -v --sip-trace <command>`, or set `logging.level: DEBUG` and
`logging.sip_trace: true`. In Docker: `docker compose logs -f`.

**`registration failed: 401 Unauthorized` or `403 Forbidden`.** Wrong `username`, `password` or `auth_username`,
the extension is not chan_pjsip, or the gateway address is banned by the FreePBX firewall (Connectivity, Firewall,
Intrusion Detection).

**`registration failed: 408 no response`.** UDP is not reaching the PBX: wrong `server`, `port` or transport, or a
firewall. Run `sipgram -v --sip-trace check`: a `UDP send/receive error` next to the outgoing REGISTER means the
packet never left the host.

**Incoming calls do not arrive although registration works.** The PBX sends the INVITE to the gateway's Contact.
Check `asterisk -rx "pjsip show contacts"`. A private address there means Rewrite Contact should be on, or
`sip.public_ip` should be set.

**No audio, or audio in one direction only.**

* Nothing from Telegram to the phone: the PBX is not receiving our RTP, so either the SDP from the PBX has a
  private address (NAT Settings) or a firewall is in the way.
* Nothing from the phone to Telegram: our RTP port range is unreachable from the PBX, or `sip.public_ip` is not set
  behind NAT. RTP Symmetric on the extension usually solves it, since the gateway sends RTP first.
* `488 Not Acceptable Here` in the log means there is no common codec: enable opus or ulaw/alaw on the extension.

**The call drops after about 30 seconds.** The ACK is not getting through (`no ACK for final response` in the log),
which is normally a NAT problem with the address in Contact or Via.

**`USER_PRIVACY_RESTRICTED`, 480 for the PBX.** The user has calls restricted, and the gateway tells them so in the
chat. They should add the gateway account to their contacts or to the call exceptions.

**`INVITE from ... ignored` in the log.** The PBX sends calls from an address other than the one `sip.server`
resolves to (several interfaces, a cluster). Add that address or network to `sip.allow_from`.

**`cannot resolve user 123456789`.** The gateway has not seen this user yet. Send it any message from that account,
or list the user as `@username` or `+phone`.

**`FLOOD_WAIT_X` or `PEER_FLOOD`.** Telegram wants you to wait, or considers the activity spam. Use an account with
some history, call contacts only, do not run mass dialling.

**The call is accepted but media goes CONNECTING and then FAILED.** WebRTC/UDP is not getting to the Telegram
relays. Check outgoing UDP from the host and any DPI or blocking of Telegram calls in your network.

**The call connects but there is silence.** The log ends with `bridge stopped: sip->tg N frames, tg->sip M frames`.
`tg->sip 0` means NTgCalls is not producing frames; `sip->tg 0` means there is no RTP from the PBX.

**Delay or crackling.** Raise `calls.jitter_ms` to 60 or 100 on a poor network between the gateway and the PBX, or
lower it to 20 for a local PBX.

**"Access denied" in response to `*1...`.** This is FreePBX, not the gateway: `*1` during a call is reserved for
One Touch Record. Asterisk intercepts those two digits and, if on-demand recording is not allowed for the
extension, plays "Access denied" while the rest of the digits go through. Fix it in Applications, Extensions,
Recording, On Demand Recording, or in Admin, Feature Codes.

**The digits are not heard by anyone but they do arrive.** That is how `dtmf: rfc2833` works, the same as on a real
SIP phone: the digit travels as a separate RTP event, IVRs see it and the person on the other end does not. Set
`dtmf: inband` on the extension if the tone itself has to be audible.

**No buttons, everything comes from the gateway account.** The bot writes only to users who pressed Start in it.
`sipgram whoami` shows its @name.

**Recording does not start, `recording is not available` in the log.** libopus is missing. The Docker image
installs `libopus0`; natively, `apt install libopus0`.

**Calls do not come through although everything is registered.** Check `/schedule` in the chat or `sipgram doctor`:
`schedule.work_hours` or `quiet_hours` are probably in effect. The time is local to the process, so set `TZ` in
`docker-compose.yml`, otherwise the container lives in UTC.

**`GROUPCALL_FORBIDDEN` when starting a voice chat.** The gateway account may not manage voice chats: give it the
"Manage video chats" admin right, or start the voice chat by hand and let the gateway join it.

**A user gets busy (486) although they are free.** Either `telegram.max_calls` is reached (`gateway at capacity` in
the log) or the user is in `/dnd`.

**`WinError 10048` on Windows.** Another instance or a softphone is holding `local_port` or the RTP ports.

## Tests

```bash
pip install -e ".[dev]"
pytest tests --ignore=tests/integration
cd tests/integration
docker compose up --build --abort-on-container-exit --exit-code-from tester
docker compose down
```

125 unit tests, about 15 seconds: the SIP parser, digest against the RFC 2617 vector, SDP offer and answer, G.711
and Opus, RTP, the Ogg container, the config, the call logic on fake legs, the schedule and number lists, the
Telegram call engine with conferences on a fake NTgCalls, localisation, the HTTP API and the state file
housekeeping. Two of them run the real ntgcalls: two instances call each other inside the process over protocols 9
and 13, checking the key exchange, the emoji, audio both ways and the order of frames. They are skipped on Windows,
where ntgcalls keeps the process from exiting.

24 end-to-end tests against `andrius/asterisk` (Asterisk 22.10) with the configs from `tests/integration/asterisk/`:
registration over UDP and TCP, wrong password, outgoing call to `Echo()` with a spectral check of the returned
tone, `Milliwatt()`, 486 Busy, 180 then answer, 183 with early media, CANCEL, BYE from the PBX, DTMF in both
directions, incoming calls through AMI Originate, hold and resume, REFER transfer and two simultaneous dialogs on
one registration. Both suites run in CI on every push.

## How it is built

```
sipgram/
  __main__.py      CLI: run / login / whoami / check / status / doctor
  config.py        YAML into dataclasses, ${ENV}, migration of the v0.1 format
  gateway.py       startup: gateway account, NTgCalls, bot, manager, state file for the healthcheck
  manager.py       CallManager: users, SIP accounts, parallel calls, hold/waiting/transfer, chat commands, call card
  bridge.py        CallBridge (Telegram to SIP: 20 ms ticker, jitter buffer, tones), SipSipBridge, GroupBridge
  messages.py      RU/EN strings, number and duration formatting
  prefs.py         per-user settings that survive restarts
  alerts.py        admin notifications with flap suppression
  api.py           HTTP API on aiohttp
  history.py       per-user call log with rotation
  schedule.py      schedule windows, number patterns, the screening decision
  doctor.py        the doctor command
  sip/             own SIP stack: message parser, digest, SDP, codecs, libopus binding, RTP, RTCP, transports, account
  tg/              Telethon account, NTgCalls engine, group calls and conferences, optional bot, notifications
  audio/           PCM buffer, resampler, Ogg/Opus container, in-memory recorder, tone generator
```

A user has at most one Telegram call (`UserState.tg`). SIP legs attach to it: `active` (bridged to Telegram),
`held` (the PBX plays MOH) and `waiting` (a second incoming call still ringing). Transitions are serialised by
`UserState.lock`. Different users are fully independent: NTgCalls keys calls by peer id and SipAccount keys dialogs
by Call-ID, so parallel conversations need no extra synchronisation.

The SIP stack is a compact subset of RFC 3261 on asyncio, enough for the role of a phone behind a PBX: REGISTER
with digest (MD5 and SHA-256, qop), INVITE/ACK/BYE/CANCEL in both directions, T1/T2 retransmissions for UDP,
replies to OPTIONS/INFO/NOTIFY/UPDATE/MESSAGE, re-INVITE for hold and session timers, REFER with sipfrag NOTIFY,
and UDP, TCP and TLS transports. It needs no compilation and behaves the same on Linux and Windows.

The Telegram side drives NTgCalls directly, with Telethon for MTProto, instead of going through pytgcalls. The
sequence is the same, plus `phone.receivedCall`, precise ring timeouts and explicit `discardCall` reasons, so the
PBX gets meaningful codes (486, 480, 603) and voicemail and follow-me behave correctly.

When the other party adds people, Telegram discards the private call with
`phoneCallDiscardReasonMigrateConferenceCall`. The gateway joins the conference by the slug in that reason, taking
the media of the private call along, and from then on keeps the conference's E2E chain itself: it applies blocks,
answers subchain requests and tells NTgCalls which SSRC belongs to whom. A `messageActionConferenceCall` invitation
from a user is handled as an incoming call.

NTgCalls 3 runs its calls on a thread pool, and two 10 ms frames handed over back to back could overtake each
other: on Windows that happened to 3 to 4% of frames. Frames therefore go through `FrameSender` strictly one at a
time.

Audio needs no resampling in the common case. The external frame rate of NTgCalls is set to the SIP codec rate, and
WebRTC inside NTgCalls resamples to Opus 48 kHz. Telegram to SIP is packetised by a 20 ms ticker with a `jitter_ms`
buffer; SIP to Telegram is handed over in 10 ms frames as the RTP arrives, and the jitter is absorbed by NetEq on
the client.

Call recording is encoded to Opus as it goes (20 kbit/s, about 2.5 KB/s) and kept in an Ogg container in memory, so
a ten minute conversation costs about one and a half megabytes. When the bridge stops, the file is sent to the chat and dropped.

## Limitations

* One conversation per user, plus a held and a waiting call. Parallelism happens between users.
* How many simultaneous calls one Telegram account really sustains is up to Telegram. Keep `max_calls` small on the
  first runs.
* Voice only. SIP codecs: Opus, G.711, L16. TLS signalling is supported, SRTP is not.
* A group voice chat needs the gateway account to be a member with the right to start one; without
  `calls.group_chat`, `/group` starts a Telegram conference call instead. While the conversation is in a group
  call, DTMF cannot be sent from the chat for that leg.
* Telegram conference calls are verified on a fake NTgCalls and against the protocol; there has been no live run
  with several participants yet.
* On Windows the process exits through `TerminateProcess`, a limitation of the ntgcalls library.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) plus [NOTICE.md](NOTICE.md), which adds the required copyright
notice and the restrictions on redistributing forks and on the project name. The source is public but this is not
an OSI-approved license: personal, hobby, educational and other noncommercial use is free, while commercial use
needs a separate agreement. Pull requests are welcome.
