"""Configuration loading: YAML file with ${ENV} substitution -> dataclasses.

Layout (v0.2): one gateway Telegram account serves every user; each SIP account (extension)
belongs to one or more users, or is shared (routed by the dialed number).
The v0.1 ``lines`` layout is migrated automatically when all lines use the same session.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .schedule import Schedule, ScheduleError

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    pass


def _subst(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _subst(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v) for v in value]
    return value


@dataclass
class GatewayConfig:
    """One Telegram account that places and receives the calls."""
    name: str
    session: str
    max_calls: int = 10

    def __post_init__(self) -> None:
        self.name = str(self.name)
        self.session = str(self.session)
        if self.max_calls < 1:
            raise ConfigError(f"gateway {self.name}: max_calls must be >= 1")


@dataclass
class TelegramAppConfig:
    api_id: int
    api_hash: str
    session: str = "gateway"
    sessions_dir: Path = Path("sessions")
    bot_token: str = ""
    bot_session: str = "bot"
    max_calls: int = 10
    language: str = "ru"
    gateways: list[GatewayConfig] = field(default_factory=list)
    device_model: str = "SIPgram"
    app_version: str = __version__

    def __post_init__(self) -> None:
        self.language = (self.language or "ru").lower()
        if self.language not in ("ru", "en"):
            raise ConfigError("telegram.language must be ru or en")
        if self.max_calls < 1:
            raise ConfigError("telegram.max_calls must be >= 1")
        extra = []
        for i, g in enumerate(self.gateways):
            if isinstance(g, GatewayConfig):
                extra.append(g)
                continue
            if not isinstance(g, dict):
                raise ConfigError(f"telegram.gateways[{i}]: expected a mapping")
            d = dict(g)
            d.setdefault("name", d.get("session", f"gw{i + 2}"))
            d.setdefault("session", d["name"])
            extra.append(_build(GatewayConfig, d, f"telegram.gateways[{i}]"))
        primary = GatewayConfig(name="main", session=self.session, max_calls=self.max_calls)
        self.gateways = [primary] + extra
        names = [g.name for g in self.gateways]
        if len(set(names)) != len(names):
            raise ConfigError("telegram gateway names must be unique")
        sessions = [g.session for g in self.gateways]
        if len(set(sessions)) != len(sessions):
            raise ConfigError("each telegram gateway needs its own session file")

    def gateway(self, name: str) -> GatewayConfig | None:
        for g in self.gateways:
            if g.name == name:
                return g
        return None


@dataclass
class ScheduleConfig:
    """Screening of incoming PBX calls. Empty by default: every call rings as before."""
    work_hours: Any = field(default_factory=list)   # only inside these windows the call rings
    quiet_hours: Any = field(default_factory=list)  # inside these windows it does not
    blacklist: list[str] = field(default_factory=list)
    whitelist: list[str] = field(default_factory=list)   # non-empty: everyone else is filtered
    action: str = "unavailable"     # unavailable (480) | busy (486) | reject (603)
    forward: str = ""               # redirect the filtered call to this number instead (302)
    notify: bool = True             # tell the user in the chat that a call was filtered

    ACTIONS = {"unavailable": 480, "busy": 486, "reject": 603}

    def __post_init__(self) -> None:
        self.work = Schedule.parse(self.work_hours, "schedule.work_hours")
        self.quiet = Schedule.parse(self.quiet_hours, "schedule.quiet_hours")
        self.blacklist = [str(x) for x in self.blacklist or []]
        self.whitelist = [str(x) for x in self.whitelist or []]
        self.action = str(self.action or "unavailable").lower()
        if self.action not in self.ACTIONS:
            raise ConfigError(f"schedule.action must be one of {', '.join(self.ACTIONS)}")
        self.forward = str(self.forward or "")

    @property
    def empty(self) -> bool:
        return self.work.empty and self.quiet.empty and not self.blacklist and not self.whitelist

    @property
    def code(self) -> int:
        return self.ACTIONS[self.action]


@dataclass
class UserConfig:
    id: str
    name: str = ""
    can_call: bool = True
    language: str = ""          # starting language; the user can change it with /lang
    gateway: str = ""           # which gateway account calls this user (default: the first one)
    schedule: dict | None = None    # overrides the global schedule section for this user
    rules: ScheduleConfig = field(init=False, default_factory=ScheduleConfig)

    def __post_init__(self) -> None:
        self.language = (self.language or "").lower()
        if self.language and self.language not in ("ru", "en"):
            raise ConfigError(f"user {self.id}: language must be ru or en")
        if self.schedule is not None and not isinstance(self.schedule, dict):
            raise ConfigError(f"user {self.id}: schedule must be a mapping")


@dataclass
class SipConfig:
    server: str
    username: str
    password: str
    port: int = 5060
    transport: str = "udp"
    auth_username: str = ""
    domain: str = ""
    display_name: str = ""
    register: bool = True
    expires: int = 300
    local_ip: str = ""
    local_port: int = 0
    public_ip: str = ""
    rtp_port_min: int = 40000
    rtp_port_max: int = 40200
    codecs: list[str] = field(default_factory=lambda: ["pcma", "pcmu"])
    dtmf: str = "rfc2833"
    keepalive: int = 25
    tls_verify: bool = True
    user_agent: str = f"SIPgram/{__version__}"
    allow_from: list[str] = field(default_factory=list)   # besides sip.server, who may start calls and send messages

    def __post_init__(self) -> None:
        if not self.server:
            raise ConfigError("sip.server is required")
        self.username = str(self.username)
        self.password = str(self.password)
        self.transport = self.transport.lower()
        if self.transport not in ("udp", "tcp", "tls"):
            raise ConfigError(f"sip.transport must be udp, tcp or tls, got {self.transport}")
        if not self.domain:
            self.domain = self.server
        if not self.auth_username:
            self.auth_username = self.username
        if self.dtmf not in ("rfc2833", "inband", "info", "none"):
            raise ConfigError("sip.dtmf must be rfc2833, inband, info or none")
        if self.rtp_port_min > self.rtp_port_max:
            raise ConfigError("sip.rtp_port_min must be <= sip.rtp_port_max")
        self.allow_from = [str(a) for a in self.allow_from or []]
        for item in self.allow_from:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as e:
                raise ConfigError(f"sip.allow_from: {item} is not an IP address or network") from e
        from .sip.codecs import codec_list

        try:
            codec_list(self.codecs)
        except ValueError as e:
            raise ConfigError(str(e)) from e


@dataclass
class AccountConfig(SipConfig):
    name: str = ""
    users: list[str] = field(default_factory=list)
    shared: bool = False
    ring_all: bool = False
    ring_timeout: int = 40
    default_destination: str = ""
    route_by_dialed: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.name:
            self.name = self.username
        self.users = [str(u) for u in self.users]
        self.default_destination = str(self.default_destination or "")
        if not self.users and not self.shared:
            raise ConfigError(f"sip account {self.name}: list its users or mark it shared: true")


@dataclass
class CallsConfig:
    outgoing_mode: str = "callback"
    outgoing_ring_timeout: int = 60
    pending_number_ttl: int = 120
    dial_rules: list[list[str]] = field(default_factory=lambda: [["[^0-9+*#]", ""], ["^\\+", ""], ["^8(\\d{10})$", "7\\1"]])
    call_waiting: bool = True
    reconnect_timeout: int = 60
    notify_incoming: bool = True
    jitter_ms: int = 40
    tg_sample_rate: int = 0
    ringback: str = "ru"
    history_size: int = 50
    record: str = "off"                  # off | ask | all; recordings are sent to the chat, never stored
    record_max_minutes: int = 60
    conference_extension: str = ""       # ConfBridge room on the PBX, enables /conf
    group_chat: str = ""                 # Telegram group whose voice chat /group uses
    group_title: str = "SIPgram"         # title of the voice chat when the gateway starts one

    def __post_init__(self) -> None:
        if self.outgoing_mode not in ("callback", "direct"):
            raise ConfigError("calls.outgoing_mode must be callback or direct")
        if isinstance(self.record, bool):        # YAML turns a bare off/on into a boolean
            self.record = "all" if self.record else "off"
        self.record = str(self.record).lower()
        if self.record not in ("off", "ask", "all"):
            raise ConfigError("calls.record must be off, ask or all")
        self.conference_extension = str(self.conference_extension or "")
        self.group_chat = str(self.group_chat or "")


@dataclass
class NotificationsConfig:
    """Where the gateway reports its own problems."""
    admin: str = ""              # Telegram id / @username that receives the alerts
    registration: bool = True    # SIP account lost or regained its registration
    telegram: bool = True        # Telegram session problems, flood waits, failed reconnects
    startup: bool = True         # gateway started and stopped
    calls: bool = False          # one line per finished call
    down_after: int = 60         # how long a problem must last before it is reported, seconds

    def __post_init__(self) -> None:
        self.admin = str(self.admin or "")

    @property
    def enabled(self) -> bool:
        return bool(self.admin)


@dataclass
class ApiConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8080
    token: str = ""
    allow: list[str] = field(default_factory=list)   # optional IP/CIDR allowlist
    rate_limit: int = 120                            # requests per minute per client

    def __post_init__(self) -> None:
        self.token = str(self.token or "")
        if self.enabled and len(self.token) < 16:
            raise ConfigError("api.token must be at least 16 characters when the API is enabled")


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""
    sip_trace: bool = False


@dataclass
class Config:
    telegram: TelegramAppConfig
    users: list[UserConfig]
    accounts: list[AccountConfig]
    calls: CallsConfig = field(default_factory=CallsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    state_dir: Path = Path("sessions")
    notes: list[str] = field(default_factory=list)

    def user(self, spec: str) -> UserConfig | None:
        for u in self.users:
            if u.id == str(spec):
                return u
        return None


def _build(cls, data: dict, path: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"{path}: {e}") from e


def _user_entry(u: Any, path: str) -> UserConfig:
    if isinstance(u, (int, str)):
        return UserConfig(id=str(u))
    if isinstance(u, dict):
        d = dict(u)
        d["id"] = str(d.get("id", ""))
        if not d["id"]:
            raise ConfigError(f"{path}: user id is required")
        return _build(UserConfig, d, path)
    raise ConfigError(f"{path}: expected a user id or mapping")


def _migrate_lines(raw: dict) -> dict:
    """v0.1 layout (lines[] each with its own session) -> v0.2 layout."""
    lines = raw.get("lines") or []
    if not lines:
        raise ConfigError("no lines configured")
    sessions = []
    for ln in lines:
        tg = ln.get("telegram") if isinstance(ln.get("telegram"), dict) else {}
        sessions.append(str(tg.get("session") or ln.get("session") or ln.get("name") or "line1"))
    if len(set(sessions)) > 1:
        raise ConfigError("v0.1 config with several gateway sessions cannot be migrated automatically: "
                          "one process now serves one gateway account; split it into separate configs "
                          "or move every extension under sip.accounts of one session")
    out: dict[str, Any] = {k: v for k, v in raw.items() if k not in ("lines", "sip_defaults")}
    telegram = dict(raw.get("telegram") or {})
    telegram["session"] = sessions[0]
    out["telegram"] = telegram
    users: list[Any] = list(raw.get("users") or [])
    seen = {str(u["id"] if isinstance(u, dict) else u) for u in users}
    sip = dict(raw.get("sip_defaults") or {})
    accounts: list[dict] = []
    calls: dict[str, Any] = {}
    for i, ln in enumerate(lines):
        acc = dict(ln.get("sip") or {})
        acc["name"] = str(ln.get("name") or f"line{i + 1}")
        acc_users: list[str] = []
        for u in ln.get("users") or []:
            uid = str(u["id"] if isinstance(u, dict) else u)
            if uid not in seen:
                seen.add(uid)
                users.append(u if isinstance(u, dict) else uid)
            acc_users.append(uid)
        inc = ln.get("incoming") or {}
        ring_to = str(inc.get("ring_to") or "")
        if ring_to and ring_to in acc_users:
            acc_users.remove(ring_to)
            acc_users.insert(0, ring_to)
        acc["users"] = acc_users
        if "ring_timeout" in inc:
            acc["ring_timeout"] = inc["ring_timeout"]
        if "route_by_dialed" in inc:
            acc["route_by_dialed"] = inc["route_by_dialed"]
        if "notify_text" in inc:
            calls["notify_incoming"] = inc["notify_text"]
        outg = ln.get("outgoing") or {}
        if outg.get("default_destination"):
            acc["default_destination"] = outg["default_destination"]
        if i == 0:
            for src, dst in (("mode", "outgoing_mode"), ("dial_rules", "dial_rules"),
                             ("pending_number_ttl", "pending_number_ttl"), ("ring_timeout", "outgoing_ring_timeout")):
                if src in outg:
                    calls[dst] = outg[src]
            for k in ("jitter_ms", "tg_sample_rate", "ringback"):
                if k in (ln.get("audio") or {}):
                    calls[k] = ln["audio"][k]
        accounts.append(acc)
    sip["accounts"] = accounts
    out["sip"] = sip
    out["users"] = users
    if calls:
        out["calls"] = {**(raw.get("calls") or {}), **calls}
    return out


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw = _subst(raw)
    notes: list[str] = []
    if "lines" in raw:
        raw = _migrate_lines(raw)
        notes.append("config uses the v0.1 'lines' layout; it was migrated in memory, see config.example.yaml for the current format")
    if "telegram" not in raw:
        raise ConfigError("missing 'telegram' section")
    tg_raw = dict(raw["telegram"])
    if "sessions_dir" in tg_raw:
        tg_raw["sessions_dir"] = Path(tg_raw["sessions_dir"])
    telegram = _build(TelegramAppConfig, tg_raw, "telegram")
    if not telegram.sessions_dir.is_absolute():
        telegram.sessions_dir = (path.parent / telegram.sessions_dir).resolve()

    users = [_user_entry(u, f"users[{i}]") for i, u in enumerate(raw.get("users") or [])]
    known = {u.id for u in users}

    sip_raw = dict(raw.get("sip") or {})
    accounts_raw = sip_raw.pop("accounts", None) or []
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("sip.accounts must list at least one SIP account")
    base_port = int(sip_raw.get("local_port") or 5070)
    accounts: list[AccountConfig] = []
    for i, acc in enumerate(accounts_raw):
        p = f"sip.accounts[{i}]"
        if not isinstance(acc, dict):
            raise ConfigError(f"{p}: expected a mapping")
        merged = {**sip_raw, **acc}
        if "user" in merged:
            single = merged.pop("user")
            merged.setdefault("users", [])
            merged["users"] = [str(single)] + [str(u) for u in merged["users"]]
        if not acc.get("local_port"):
            merged["local_port"] = base_port + i
        merged["username"] = str(merged.get("username", ""))
        if not merged["username"]:
            raise ConfigError(f"{p}: username is required")
        merged["password"] = str(merged.get("password", ""))
        account = _build(AccountConfig, merged, p)
        for uid in account.users:
            if uid not in known:
                known.add(uid)
                users.append(UserConfig(id=uid))
        accounts.append(account)
    names = [a.name for a in accounts]
    if len(set(names)) != len(names):
        raise ConfigError("sip account names must be unique (set name: on duplicates)")
    ports = [(a.local_ip, a.local_port) for a in accounts]
    if len(set(ports)) != len(ports):
        raise ConfigError("each sip account needs its own local_port")
    if not users:
        raise ConfigError("no users configured")
    for u in users:
        if u.gateway and telegram.gateway(u.gateway) is None:
            raise ConfigError(f"user {u.id}: no telegram gateway named {u.gateway}")
    calls = _build(CallsConfig, dict(raw.get("calls") or {}), "calls")
    schedule_raw = dict(raw.get("schedule") or {})
    try:
        schedule = _build(ScheduleConfig, dict(schedule_raw), "schedule")
        for u in users:
            if not u.schedule:
                u.rules = schedule
                continue
            u.rules = _build(ScheduleConfig, {**schedule_raw, **u.schedule}, f"user {u.id}: schedule")
    except ScheduleError as e:
        raise ConfigError(str(e)) from e
    notifications = _build(NotificationsConfig, dict(raw.get("notifications") or {}), "notifications")
    api = _build(ApiConfig, dict(raw.get("api") or {}), "api")
    logging_cfg = _build(LoggingConfig, dict(raw.get("logging") or {}), "logging")
    state_dir = Path(raw.get("state_dir") or telegram.sessions_dir)
    if not state_dir.is_absolute():
        state_dir = (path.parent / state_dir).resolve()
    return Config(telegram=telegram, users=users, accounts=accounts, calls=calls, schedule=schedule,
                  notifications=notifications, api=api, logging=logging_cfg, state_dir=state_dir, notes=notes)
