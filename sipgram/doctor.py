"""`sipgram doctor`: one command that checks everything the gateway needs before it will work.

Every check prints one line; the exit code is 1 if any of them failed. Network checks (DNS, SIP
registration, Telegram sessions) are skipped with --offline.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config
from .schedule import time_closed
from .util import detect_local_ip

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[fail]"}


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        self.lines.append((status, name, detail))
        print(f"{MARK[status]} {name:<14} {detail}")

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.lines if s == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for s, _, _ in self.lines if s == WARN)


def _writable(path: Path) -> str:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".sipgram-write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return str(e)
    return ""


def _check_paths(rep: Report, cfg: Config) -> None:
    for name, path in (("state dir", cfg.state_dir), ("sessions dir", cfg.telegram.sessions_dir)):
        error = _writable(path)
        rep.add(FAIL if error else OK, name, f"{path}: {error}" if error else f"{path} is writable")
    for name, path, limit in (("history", cfg.state_dir / "history.json", 1 << 20),
                              ("prefs", cfg.state_dir / "prefs.json", 1 << 18)):
        if not path.exists():
            rep.add(OK, name, "no file yet")
            continue
        size = path.stat().st_size
        try:
            entries = len(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            rep.add(WARN, name, f"{path.name} is unreadable and will be recreated: {e}")
            continue
        rep.add(WARN if size > limit else OK, name, f"{path.name}: {entries} user(s), {size / 1024:.0f} KB"
                + (", will be rotated" if size > limit else ""))


def _check_runtime(rep: Report, cfg: Config) -> None:
    rep.add(OK, "version", f"sipgram {__version__} on python {platform.python_version()} ({platform.system()})")
    try:
        import ntgcalls

        protocol = ntgcalls.NTgCalls.get_protocol()
        version = getattr(ntgcalls, "__version__", "?")
        if not version[:1].isdigit() or int(version.split(".")[0]) < 3:
            rep.add(FAIL, "ntgcalls", f"{version} is too old, sipgram needs 3.x: pip install -U -r requirements.txt")
        else:
            rep.add(OK, "ntgcalls", f"{version}, layers {protocol.min_layer}-{protocol.max_layer}, "
                                    f"libraries {', '.join(protocol.library_versions)}")
    except Exception as e:
        rep.add(FAIL, "ntgcalls", f"not usable: {e}")
    from .sip.opus import available, load_error

    if available():
        rep.add(OK, "libopus", "loaded, opus codec and call recording are available")
    else:
        wanted = any("opus" in a.codecs for a in cfg.accounts)
        rep.add(FAIL if (wanted or cfg.calls.record != "off") else WARN, "libopus",
                f"not loaded ({load_error()}); install libopus0, otherwise no recording and no wideband audio")
    if cfg.api.enabled:
        try:
            import aiohttp

            rep.add(OK, "aiohttp", f"{aiohttp.__version__}, API on {cfg.api.bind}:{cfg.api.port}")
        except ImportError as e:
            rep.add(FAIL, "aiohttp", f"the API is enabled but aiohttp is missing: {e}")


def _check_ports(rep: Report, cfg: Config, running: bool = False) -> None:
    for acc in cfg.accounts:
        ip = acc.local_ip or "0.0.0.0"
        kind = socket.SOCK_DGRAM if acc.transport == "udp" else socket.SOCK_STREAM
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.bind((ip, acc.local_port))
            rep.add(OK, "sip port", f"{acc.name}: {acc.transport.upper()} {ip}:{acc.local_port} is free")
        except OSError as e:
            rep.add(WARN if running else FAIL, "sip port",
                    f"{acc.name}: {ip}:{acc.local_port} is taken ({e})"
                    + (", by the running gateway" if running else ", another instance or a softphone?"))
        finally:
            sock.close()
    acc = cfg.accounts[0]
    ports = (acc.rtp_port_max - acc.rtp_port_min) // 2 + 1
    free, ip = 0, acc.local_ip or "0.0.0.0"
    for port in range(acc.rtp_port_min + acc.rtp_port_min % 2, acc.rtp_port_max + 1, 2):
        s1, s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s1.bind((ip, port))
            s2.bind((ip, port + 1))     # RTCP sits on the odd port
            free += 1
        except OSError:
            pass
        finally:
            s1.close()
            s2.close()
    need = cfg.telegram.max_calls * 2       # a user may hold a second call while talking
    status = FAIL if free == 0 else (WARN if free < need else OK)
    rep.add(status, "rtp ports", f"{free} of {ports} pairs free in {acc.rtp_port_min}-{acc.rtp_port_max}: "
                                 f"one per call leg, up to {need} with max_calls={cfg.telegram.max_calls}")


def _check_schedule(rep: Report, cfg: Config) -> None:
    tz = os.environ.get("TZ") or "/".join(t for t in time.tzname if t)
    configured = [u for u in cfg.users if not u.rules.empty]
    if not configured:
        rep.add(OK, "schedule", f"not configured, calls ring around the clock (local time {datetime.now():%H:%M}, {tz})")
        return
    closed = [u.name or u.id for u in configured if time_closed(u.rules)]
    rep.add(OK, "schedule", f"{len(configured)} user(s) with rules, local time {datetime.now():%H:%M} ({tz})"
            + (f"; not accepting calls now: {', '.join(closed)}" if closed else "; everyone is accepting calls"))


def _check_running(rep: Report, cfg: Config) -> bool:
    path = cfg.state_dir / "state.json"
    if not path.exists():
        rep.add(OK, "running", "no state file: the gateway is not running here")
        return False
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        rep.add(WARN, "running", f"state file unreadable: {e}")
        return False
    age = time.time() - snap.get("ts", 0)
    if age > 90:
        rep.add(OK, "running", f"state file is {age / 60:.0f} min old: the gateway is stopped")
    else:
        bad = [n for n, ok in snap.get("accounts", {}).items() if not ok]
        rep.add(WARN if bad else OK, "running",
                f"a gateway is running here ({snap.get('calls', 0)} SIP legs)"
                + (f", unregistered: {', '.join(bad)}" if bad else "")
                + ", the port and registration checks below apply to it")
    return age <= 90


def _check_dns(rep: Report, cfg: Config) -> None:
    servers = {}
    for acc in cfg.accounts:
        servers.setdefault(acc.server, acc.port)
    for server, port in servers.items():
        try:
            infos = socket.getaddrinfo(server, port, socket.AF_INET, socket.SOCK_DGRAM)
            addrs = sorted({i[4][0] for i in infos})
            rep.add(OK, "dns", f"{server} -> {', '.join(addrs)} (local address {detect_local_ip(server, port)})")
        except OSError as e:
            rep.add(FAIL, "dns", f"{server}: {e}")


async def _check_sip(rep: Report, cfg: Config) -> None:
    from .sip.account import CallError, SipAccount

    for acc in cfg.accounts:
        if acc.transport != "udp":
            try:
                fut = asyncio.open_connection(acc.server, acc.port)
                _, writer = await asyncio.wait_for(fut, 5)
                writer.close()
                rep.add(OK, "sip tcp", f"{acc.name}: {acc.server}:{acc.port} accepts connections")
            except Exception as e:
                rep.add(FAIL, "sip tcp", f"{acc.name}: cannot reach {acc.server}:{acc.port} ({e})")
        local_ip = acc.local_ip or detect_local_ip(acc.server, acc.port)
        probe = SipAccount(_no_register(acc), local_ip)
        try:
            await probe.start()
            expires = await probe._register_once(acc.expires)
            await probe._register_once(0)
            rep.add(OK, "registration", f"{acc.name}: {acc.username}@{acc.domain} OK, expires {expires}s, "
                                        f"codecs {', '.join(acc.codecs)}")
        except CallError as e:
            rep.add(FAIL, "registration", f"{acc.name}: {e}")
        except Exception as e:
            rep.add(FAIL, "registration", f"{acc.name}: {e}")
        finally:
            await probe.transport.stop()


def _no_register(acc):
    import copy

    probe = copy.copy(acc)
    probe.register = False
    probe.keepalive = 0
    probe.local_port = 0
    return probe


async def _check_telegram(rep: Report, cfg: Config) -> None:
    from .tg.account import NotAuthorized, TgAccount

    for gcfg in cfg.telegram.gateways:
        path = cfg.telegram.sessions_dir / f"{gcfg.session}.session"
        if not path.exists():
            rep.add(FAIL, "telegram", f"{gcfg.name}: no session file {path.name}, run: sipgram login {gcfg.name}")
            continue
        account = TgAccount(cfg.telegram, gcfg.session)
        try:
            await account.connect()
            me = account.me
            mine = [u for u in cfg.users if (u.gateway or cfg.telegram.gateways[0].name) == gcfg.name]
            rep.add(OK, "telegram", f"{gcfg.name}: logged in as id={me.id} phone={me.phone}, "
                                    f"{len(mine)} user(s), max_calls={gcfg.max_calls}")
            for u in mine:
                try:
                    await account.resolve_user(u.id)
                except Exception as e:
                    rep.add(FAIL, "telegram user", f"{u.id}: cannot resolve ({e}); write to the gateway account once")
        except NotAuthorized as e:
            rep.add(FAIL, "telegram", f"{gcfg.name}: {e}")
        except Exception as e:
            rep.add(FAIL, "telegram", f"{gcfg.name}: {e}")
        finally:
            await account.disconnect()
    if cfg.telegram.bot_token:
        from .tg.bot import bot_login_check

        try:
            rep.add(OK, "bot", f"@{await bot_login_check(cfg.telegram)} answers, buttons are available")
        except Exception as e:
            rep.add(FAIL, "bot", f"token rejected: {e}")
    if cfg.notifications.enabled and not cfg.notifications.admin:
        rep.add(WARN, "alerts", "notifications are on but notifications.admin is empty")


async def run(cfg: Config, offline: bool = False) -> int:
    rep = Report()
    for note in cfg.notes:
        rep.add(WARN, "config", note)
    rep.add(OK, "config", f"{len(cfg.users)} user(s), {len(cfg.accounts)} SIP account(s), "
                          f"{len(cfg.telegram.gateways)} telegram account(s)")
    _check_runtime(rep, cfg)
    _check_paths(rep, cfg)
    running = _check_running(rep, cfg)
    _check_ports(rep, cfg, running)
    _check_schedule(rep, cfg)
    if offline:
        print("\n(--offline: DNS, SIP registration and Telegram checks skipped)")
    else:
        _check_dns(rep, cfg)
        await _check_sip(rep, cfg)
        await _check_telegram(rep, cfg)
    print(f"\n{len(rep.lines)} checks: {rep.failed} failed, {rep.warned} warnings")
    return 1 if rep.failed else 0
