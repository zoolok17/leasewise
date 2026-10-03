"""The reporter's configuration file (DESIGN-7A.md 4.1; card 7a-3).

A TOML file with one table per agent. It holds a secret (the identity value
the ingress expects), so it must be readable by its owner only, and it is
never committed or put in a bus root. Every error here is a fixed sentence:
never the file's content, the URL or the identity.

Keys are compound, as `policy-ids --json` returns them: a seat is
(machine alias, seat alias), a model is (account alias, model alias). They are
resolved to ids at start-up from a saved snapshot of that command's output.
A bare alias that matches more than one row is refused.
"""

import ipaddress
import json
import os
import re
import sys
import tomllib
from urllib.parse import urlsplit

from . import cmdline, decisions, mapper
from .events import _NAME as _AGENT_NAME
from ..validate import ALIAS_RE

CONFIG_VERSION = 1
MAX_FILE_BYTES = 65536
_INT64 = 2 ** 63 - 1
_LOOPBACK = ("127.0.0.1", "::1", "localhost")

TOP_REQUIRED = ("version", "base_url", "identity", "machine", "journal_root",
                "state_path", "policy_ids")
TOP_OPTIONAL = ("agents", "poll_seconds", "silent_after_seconds", "limit_events", "gateway_receipts",
                "admission")
AGENT_REQUIRED = ("seat", "model", "task_class", "effort", "expected_calls",
                  "max_calls", "output_cap", "per_call_output_cap", "ttl_ms")
AGENT_OPTIONAL = ("account", "team")
_CEILINGS = ("expected_calls", "max_calls", "output_cap",
             "per_call_output_cap", "ttl_ms")

# Card 7c-2: the account map. Each source says: the seats of this provider, under this OS user, on
# the agenttalk folder `bus_root`, use this Leasewise account. v1 reads Claude only.
LIMIT_PROVIDERS = ("claude",)
MAX_LIMIT_SOURCES = 8
_LIMIT_SOURCE_KEYS = ("bus_root", "os_user", "provider", "account")

# Card L6a-1: where a gateway's receipts are read from. Gateway receipts are read from a fixture file in this version.
# Card L6a-1b (step 1): the settings of a command source are checked here, but a command is still refused at the end.
_RECEIPT_KEYS = ("gateway", "admission_store")
_RECEIPT_OPTIONAL = ("fixture", "command", "timeout_seconds", "env", "max_bytes", "page_limit")
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000
MAX_COMMAND_ARGS = 16
MAX_RECEIPT_ENV = 8
DEFAULT_COMMAND_TIMEOUT = 20
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_DRIVE_ROOTED = re.compile(r"[A-Za-z]:\\")           # X:\ ... : fully qualified, not C:foo and not \foo

# A command source (a program the reporter runs) is not part of this version (card L6a-1b, until step 3).
MSG_NO_COMMAND_SOURCE = "Command receipt sources are not available in this version; use a fixture file."
MSG_COMMAND_NOT_WINDOWS = "Command receipt sources are only available on Windows in this version; use a fixture file."

CONFIG_INVALID = "CONFIG_INVALID"
CONFIG_PERMISSIONS = "CONFIG_PERMISSIONS"
CONFIG_UNRESOLVED = "CONFIG_UNRESOLVED"


class ConfigError(Exception):
    """A configuration problem. `message` is a fixed sentence (key names from
    this module's own closed lists and agent aliases are the only variable
    parts); it never carries a value from the file."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Config:
    """The checked file. Its repr hides everything: the identity is a secret
    and the URL names the deployment."""

    __slots__ = ("scheme", "host", "port", "identity", "machine",
                 "journal_root", "state_path", "policy_ids_path",
                 "poll_seconds", "silent_after_seconds", "agents",
                 "limit_sources", "gateway_receipts", "admission")

    def __repr__(self):
        return "Config(<hidden>)"

    __str__ = __repr__


def _invalid(message):
    raise ConfigError(CONFIG_INVALID, message)


def _names(keys):
    return ", ".join(sorted(keys))


def _closed_keys(table, required, optional, where):
    missing = [k for k in required if k not in table]
    if missing:
        _invalid("%s: a required key is missing: %s" % (where, _names(missing)))
    if any(k not in required and k not in optional for k in table):
        _invalid("%s: the file has a key that is not allowed" % where)


def _text(value, where, limit=256):
    if not isinstance(value, str) or not value or len(value) > limit:
        _invalid("%s: a value must be a short, non-empty text" % where)
    return value


def _alias(value, where):
    """A Leasewise alias (machine, seat, model, account, team)."""
    if not isinstance(value, str) or ALIAS_RE.fullmatch(value) is None:
        _invalid("%s: a value is not a valid alias" % where)
    return value


def _positive(value, where):
    if type(value) is not int or not 0 < value <= _INT64:
        _invalid("%s: a value must be a positive whole number" % where)
    return value


def _path(value, where):
    _text(value, where, 1024)
    if not os.path.isabs(value) or "\x00" in value:
        _invalid("%s: a path must be absolute" % where)
    return value


class LimitSource:
    """One line of the account map. `folder` is the RESOLVED, normalised bus folder."""

    __slots__ = ("bus_root", "os_user", "provider", "account", "folder")

    def __repr__(self):
        return "LimitSource(<hidden>)"

    __str__ = __repr__


def folder_key(path):
    """The folder a path really names: resolved (dots, links, trailing separators) and normalised
    (case and slashes on Windows). Two spellings of one folder give one key."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        _invalid("a folder path cannot be resolved")


def _inside(child, parent):
    """True if the resolved path `child` is the resolved folder `parent` or lies under it."""
    base = parent.rstrip(os.sep) + os.sep
    return child == base.rstrip(os.sep) or child.startswith(base)


def _limit_sources(table, state_path):
    where = "limit_events"
    if not isinstance(table, dict):
        _invalid("%s: must be a table" % where)
    _closed_keys(table, ("sources",), (), where)
    rows = table["sources"]
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_LIMIT_SOURCES:
        _invalid("%s: sources must be a list of 1 to %d tables" % (where, MAX_LIMIT_SOURCES))
    state_key = folder_key(state_path)
    found, seen, accounts = [], set(), {}
    for row in rows:
        if not isinstance(row, dict):
            _invalid("%s: a source must be a table" % where)
        _closed_keys(row, _LIMIT_SOURCE_KEYS, (), where + " source")
        source = LimitSource()
        source.bus_root = _path(row["bus_root"], where + " source")
        source.os_user = _alias(row["os_user"], where + " source")
        provider = row["provider"]
        if not isinstance(provider, str) or provider not in LIMIT_PROVIDERS:
            _invalid("%s source: this provider is not supported" % where)
        source.provider = provider
        source.account = _alias(row["account"], where + " source")
        source.folder = folder_key(source.bus_root)
        if (source.folder, source.provider) in seen:
            _invalid("%s: two sources name the same folder and provider" % where)
        seen.add((source.folder, source.provider))
        if accounts.setdefault((source.os_user, source.provider), source.account) != source.account:
            _invalid("%s: one OS user and provider must name one account" % where)
        if _inside(state_key, source.folder):
            _invalid("%s: the state file may not be inside a bus folder" % where)
        found.append(source)
    return tuple(found)


class GatewayReceipts:
    """The receipt source of card L6a-1. `gateway_id` is None until `resolve_gateway_receipts`
    fills it from the policy-ids snapshot. The repr hides everything (a path can name a deployment)."""

    __slots__ = ("gateway", "gateway_id", "admission_store", "fixture", "max_bytes", "page_limit")

    def __repr__(self):
        return "GatewayReceipts(<hidden>)"

    __str__ = __repr__


def _windows():
    return sys.platform == "win32"


def _has_control_character(text):
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def _receipt_command(value, where):
    """The `command` setting: an argument list for a program run without a shell. Every refusal is one fixed sentence
    that never holds text from the file. Nothing is started here; the reporter appends its own options last."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_COMMAND_ARGS \
            or not all(isinstance(item, str) and 0 < len(item) <= 1024 for item in value):
        _invalid("%s: the command must be a list of 1 to %d texts" % (where, MAX_COMMAND_ARGS))
    program = value[0].replace("/", "\\")         # Windows reads both slashes the same way: check what it will see
    if program.startswith("\\\\"):
        # \\server, //server, \\?\ and \\.\ (a local \\?\C:\ too): a network or device path, never a local file
        _invalid("%s: the command may not start from a network or device path" % where)
    if _DRIVE_ROOTED.match(program) is None:
        _invalid("%s: the command must start with an absolute path" % where)
    if not program.lower().endswith(".exe"):
        # Windows runs a .bat or .cmd file through cmd.exe, which splits arguments by other rules than our quoting.
        _invalid("%s: the command must start with an .exe file" % where)
    if any('"' in item or _has_control_character(item) for item in value):
        _invalid("%s: the command has a character it may not have" % where)
    if any(cmdline.names_reserved_option(item) for item in value[1:]):
        _invalid("%s: the command may not name --after, --limit or --json in any spelling; the reporter adds them"
                 % where)
    return tuple(value)


def _receipt_timeout(value, where):
    if type(value) is not int or not 1 <= value <= 60:
        _invalid("%s: timeout_seconds must be a whole number from 1 to 60" % where)
    return value


def _receipt_env(value, where):
    if not isinstance(value, dict) or len(value) > MAX_RECEIPT_ENV \
            or not all(isinstance(item, str) and 0 < len(item) <= 1024 for item in value.values()):
        _invalid("%s: env must be a table of at most %d texts" % (where, MAX_RECEIPT_ENV))
    for name, text in value.items():
        if _ENV_NAME.fullmatch(name) is None:
            _invalid("%s: an env name is not valid" % where)
        if _has_control_character(text):
            _invalid("%s: an env value has a character it may not have" % where)
    return tuple(sorted(value.items()))


def _gateway_receipts(table):
    where = "gateway_receipts"
    if not isinstance(table, dict):
        _invalid("%s: must be a table" % where)
    _closed_keys(table, _RECEIPT_KEYS, _RECEIPT_OPTIONAL, where)
    if ("fixture" in table) == ("command" in table):
        _invalid("%s: name exactly one of fixture or command" % where)
    if "fixture" in table and ("timeout_seconds" in table or "env" in table):
        _invalid("%s: timeout_seconds and env belong to a command source" % where)
    if "command" in table:
        if not _windows():
            _invalid(MSG_COMMAND_NOT_WINDOWS)
        _receipt_command(table["command"], where)
        _receipt_timeout(table.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT), where)
        _receipt_env(table.get("env", {}), where)
        _invalid(MSG_NO_COMMAND_SOURCE)         # every setting is fine; the runner comes with step 3
    row = GatewayReceipts()
    row.gateway = _alias(table["gateway"], where)
    row.gateway_id = None
    row.admission_store = _path(table["admission_store"], where)
    row.fixture = _path(table["fixture"], where)
    row.max_bytes = table.get("max_bytes", 1024 * 1024)
    if type(row.max_bytes) is not int or not 1024 <= row.max_bytes <= 4 * 1024 * 1024:
        _invalid("%s: max_bytes must be a whole number from 1024 to 4194304" % where)
    row.page_limit = table.get("page_limit", DEFAULT_PAGE_LIMIT)
    if type(row.page_limit) is not int or not 1 <= row.page_limit <= MAX_PAGE_LIMIT:
        _invalid("%s: page_limit must be a whole number from 1 to %d" % (where, MAX_PAGE_LIMIT))
    return row


# Card L6a-2: the turn admission program (`reporter admit`). The store path, the gateway the leases are for, the agents
# whose turns the gateway admits, and the operator's token bounds (an unproven bound is never invented).
_ADMISSION_REQUIRED = ("store", "gateway", "gateway_agents")
_ADMISSION_BOUNDS = ("fresh_overhead_bound_tokens", "resume_history_bound_tokens", "growth_bound_tokens",
                     "context_ceiling_tokens")
MAX_GATEWAY_AGENTS = 64
_ADMISSION_BOUND_MAX = 10 ** 12


class Admission:
    """The `admission` block. `gateway_id` is None until `resolve_admission` fills it from the snapshot. A bound
    that was not written is None: the request that needs it is `held` as `context_unprovable`."""

    __slots__ = ("store", "gateway", "gateway_id", "gateway_agents") + _ADMISSION_BOUNDS

    def __repr__(self):
        return "Admission(<hidden>)"

    __str__ = __repr__


def _admission(table):
    where = "admission"
    if not isinstance(table, dict):
        _invalid("%s: must be a table" % where)
    _closed_keys(table, _ADMISSION_REQUIRED, _ADMISSION_BOUNDS, where)
    row = Admission()
    row.store = _path(table["store"], where)
    row.gateway = _alias(table["gateway"], where)
    row.gateway_id = None
    names = table["gateway_agents"]
    if not isinstance(names, list) or not 0 < len(names) <= MAX_GATEWAY_AGENTS:
        _invalid("%s: gateway_agents must be a list of 1 to %d agent names" % (where, MAX_GATEWAY_AGENTS))
    for name in names:
        if not isinstance(name, str) or not _AGENT_NAME.fullmatch(name):
            _invalid("%s: an agent name in gateway_agents is not valid" % where)
    if len(set(names)) != len(names):
        _invalid("%s: gateway_agents names an agent twice" % where)
    row.gateway_agents = tuple(sorted(names))
    for key in _ADMISSION_BOUNDS:
        value = table.get(key)
        if key in table and (type(value) is not int or not 0 < value <= _ADMISSION_BOUND_MAX):
            _invalid("%s: a bound must be a positive whole number" % where)
        setattr(row, key, value)
    return row


def _parse_url(value):
    """-> (scheme, host, port). Plain http is for loopback only: the identity
    travels in a header, so anything else must be https."""
    text = _text(value, "base_url", 512)
    try:
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port
    except ValueError:
        _invalid("base_url: the address is not valid")
    if parts.scheme not in ("http", "https") or not host or parts.username \
            or parts.password or parts.query or parts.fragment \
            or parts.path not in ("", "/"):
        _invalid("base_url: use scheme://host[:port] with nothing after it")
    if parts.scheme == "http" and host not in _LOOPBACK:
        _invalid("base_url: plain http is allowed for loopback only")
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


def _admission_host(host):
    """With `[admission]` the address of Leasewise must be an IP address or `localhost`, so that a run of `admit` never
    waits on a name lookup that its deadline cannot stop."""
    if host == "localhost":
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        _invalid("base_url: with [admission], the host must be an IP address or localhost")


def _identity(value):
    text = _text(value, "identity", 512)
    if any(ord(c) < 33 or ord(c) > 126 for c in text):
        _invalid("identity: the value has characters a header cannot carry")
    lowered = text.lower()
    if any(lowered in word.lower() for word in decisions.CLOSED_CODES):
        _invalid("identity: the value must not be, or be part of, one of the "
                 "public error words; use a long generated value")
    return text


def _agent_table(name, table):
    where = "agent %s" % name
    if not isinstance(table, dict):
        _invalid("%s: must be a table" % where)
    _closed_keys(table, AGENT_REQUIRED, AGENT_OPTIONAL, where)
    row = {"seat": _alias(table["seat"], where),
           "model": _alias(table["model"], where),
           "task_class": _text(table["task_class"], where, 128),
           "effort": _text(table["effort"], where, 128)}
    for key in AGENT_OPTIONAL:
        row[key] = _alias(table[key], where) if key in table else None
    for key in _CEILINGS:
        row[key] = _positive(table[key], where)
    return row


def parse_config(text):
    """TOML text -> Config (no file or permission checks)."""
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        _invalid("the configuration file is not valid TOML")
    _closed_keys(data, TOP_REQUIRED, TOP_OPTIONAL, "configuration")
    if data["version"] != CONFIG_VERSION or type(data["version"]) is not int:
        _invalid("configuration: this version is not supported")
    config = Config()
    config.scheme, config.host, config.port = _parse_url(data["base_url"])
    config.identity = _identity(data["identity"])
    config.machine = _alias(data["machine"], "machine")
    config.journal_root = _path(data["journal_root"], "journal_root")
    config.state_path = _path(data["state_path"], "state_path")
    config.policy_ids_path = _path(data["policy_ids"], "policy_ids")
    config.poll_seconds = _positive(data.get("poll_seconds", 5), "poll_seconds")
    config.silent_after_seconds = _positive(
        data.get("silent_after_seconds", 86400), "silent_after_seconds")
    config.limit_sources = ()
    if "limit_events" in data:
        config.limit_sources = _limit_sources(data["limit_events"], config.state_path)
    config.gateway_receipts = None
    if "gateway_receipts" in data:
        config.gateway_receipts = _gateway_receipts(data["gateway_receipts"])
    config.admission = None
    if "admission" in data:
        config.admission = _admission(data["admission"])
        _admission_host(config.host)
        receipts = config.gateway_receipts
        if receipts is not None and os.path.normcase(os.path.abspath(receipts.admission_store))                 != os.path.normcase(os.path.abspath(config.admission.store)):
            _invalid("admission: the store must be the same file as gateway_receipts names")
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        _invalid("agents: must be a table of agent tables")
    config.agents = {}
    for name, table in agents.items():
        if not isinstance(name, str) or not _AGENT_NAME.fullmatch(name):
            _invalid("agents: an agent name is not valid")
        config.agents[name] = _agent_table(name, table)
    if config.admission is not None and any(name not in config.agents for name in config.admission.gateway_agents):
        _invalid("admission: every agent in gateway_agents needs its own agent table")
    return config


def load_config(path, *, permissions=None):
    """Read, check and parse the file. `permissions` is the file-permission
    check (default: `check_permissions`); only tests pass another one."""
    check = check_permissions if permissions is None else permissions
    try:
        size = os.stat(path).st_size
    except OSError:
        _invalid("the configuration file cannot be read")
    if size > MAX_FILE_BYTES:
        _invalid("the configuration file is too large")
    check(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        _invalid("the configuration file cannot be read")
    config = parse_config(text)
    if config.limit_sources:
        here = folder_key(path)
        if any(_inside(here, source.folder) for source in config.limit_sources):
            # The file holds a secret and never belongs in a bus folder.
            _invalid("limit_events: the configuration file may not be inside a bus folder")
    return config


# --- file permissions -------------------------------------------------------

# Windows: who may hold ANY access entry on the file, as security identifiers (never
# as display names, which are localised). The current user's own SID is added.
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"
_ALLOWED_SIDS = frozenset({SID_SYSTEM, SID_ADMINISTRATORS})
_SID_TEXT = re.compile(r"S-1-\d+(?:-\d+){1,15}")


def _refuse_permissions(why):
    raise ConfigError(CONFIG_PERMISSIONS, "the configuration file is too open: "
                      + why)


def check_permissions(path):
    """Refuse a configuration file that other users could read.

    POSIX: no group or other permission bits at all.
    Windows (mode bits mean nothing there): the access list is read as security
    identifiers. Only the current user, SYSTEM (S-1-5-18) and BUILTIN\\Administrators
    (S-1-5-32-544) may hold an access entry, and no entry may be inherited from the
    folder (the file has its own list). Any other identifier, an entry that cannot be
    classified, or a list that cannot be read is a refusal, never a pass."""
    if sys.platform == "win32":
        return _check_windows(path, _read_windows_acl)
    return _check_posix(path)


def _check_posix(path):
    try:
        mode = os.stat(path).st_mode
    except OSError:
        _refuse_permissions("it cannot be inspected")
    if mode & 0o077:
        _refuse_permissions("group or other users can access it")


def _check_windows(path, read_acl):
    """`read_acl(path)` -> (entries, current_user_sid) or None, where each entry is
    (sid_text, kind, inherited) and kind is "allow", "deny" or "other"."""
    try:
        found = read_acl(path)
    except Exception:  # noqa: BLE001 - never a crash, never a pass
        found = None
    if not found:
        _refuse_permissions("its access list cannot be read")
    try:
        entries, current = found
        entries = list(entries)
    except Exception:  # noqa: BLE001 - a reading of the wrong shape is a refusal
        _refuse_permissions("its access list cannot be read")
    if not isinstance(current, str) or _SID_TEXT.fullmatch(current) is None \
            or not entries:
        _refuse_permissions("its access list cannot be read")
    allowed = _ALLOWED_SIDS | {current}
    for entry in entries:
        try:
            sid, kind, inherited = entry
        except Exception:  # noqa: BLE001
            _refuse_permissions("an access entry cannot be classified")
        if kind == "deny":
            continue                    # a refusal grants nobody anything
        if kind != "allow" or not isinstance(sid, str) \
                or _SID_TEXT.fullmatch(sid) is None:
            _refuse_permissions("an access entry cannot be classified")
        if inherited:
            _refuse_permissions("it inherits access from its folder")
        if sid not in allowed:
            _refuse_permissions("someone other than you, SYSTEM and "
                                "Administrators can access it")


def _read_windows_acl(path):
    """The file's access list as security identifiers, through the Windows security
    API (no tool output, no names). None when anything cannot be read."""
    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    adv.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(ctypes.c_void_p)]
    adv.GetAce.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wintypes.BOOL
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    adv.GetTokenInformation.restype = wintypes.BOOL

    def sid_text(sid_pointer):
        text = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def current_user():
        token = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008,
                                    ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD()
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not adv.GetTokenInformation(token, 1, buffer, size,
                                           ctypes.byref(size)):
                return None
            # TOKEN_USER starts with the pointer to the user's SID.
            return sid_text(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            k32.CloseHandle(token)

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT = 1, DACL_SECURITY_INFORMATION = 4
    if adv.GetNamedSecurityInfoW(path, 1, 4, None, None, ctypes.byref(dacl),
                                 None, ctypes.byref(descriptor)) != 0:
        return None
    try:
        if not dacl.value:
            return None                 # no list at all would mean "everyone"
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        entries = []
        for index in range(count):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                return None
            ace_type = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            kind = {0: "allow", 1: "deny"}.get(ace_type, "other")
            sid = sid_text(ctypes.c_void_p(ace.value + 8)) \
                if kind != "other" else None
            if kind != "other" and sid is None:
                return None
            entries.append((sid, kind, bool(flags & 0x10)))
        return entries, current_user()
    finally:
        k32.LocalFree(descriptor)


# --- resolution against a policy-ids snapshot ---------------------------------

def load_snapshot(path):
    """The saved `policy-ids --json` output, shape-checked."""
    try:
        if os.stat(path).st_size > 4 * 1024 * 1024:
            raise ValueError
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        raise ConfigError(CONFIG_UNRESOLVED,
                          "the policy-ids snapshot cannot be read") from None
    try:
        seats = [(r["machine"], r["team"], r["seat"], r["id"])
                 for r in data["seats"]]
        models = [(r["account"], r["model"], r["id"]) for r in data["models"]]
        task_classes = list(data["task_classes"])
        # Card 7c-2: the account list is OPTIONAL (an older snapshot has none).
        accounts = [(r["alias"], r["id"]) for r in data.get("accounts", [])
                    if set(r) == {"alias", "id"}]
        if len(accounts) != len(data.get("accounts", [])):
            raise TypeError
        # Card L6a-1: so is the gateway list (alias and id, nothing else).
        gateways = [(r["alias"], r["id"]) for r in data.get("gateways", [])
                    if set(r) == {"alias", "id"}]
        if len(gateways) != len(data.get("gateways", [])):
            raise TypeError
        if not all(isinstance(v, str) for row in seats + models + accounts + gateways
                   for v in row) \
                or not all(isinstance(t, str) for t in task_classes):
            raise TypeError
    except (KeyError, TypeError, AttributeError):
        raise ConfigError(CONFIG_UNRESOLVED,
                          "the policy-ids snapshot has an unexpected shape"
                          ) from None
    return {"seats": seats, "models": models, "task_classes": task_classes,
            "accounts": accounts, "gateways": gateways}


class ResolvedLimitSource:
    """A source whose account alias is resolved to the account's local id."""

    __slots__ = ("bus_root", "folder", "os_user", "provider", "account", "account_id")

    def __repr__(self):
        return "ResolvedLimitSource(<hidden>)"

    __str__ = __repr__


_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def resolve_limit_sources(config, snapshot):
    """-> a tuple of ResolvedLimitSource. Every source's account alias must be in the snapshot,
    exactly once, with a local id: otherwise the reporter refuses to start. Never the first of
    several, never a guess. The messages name no value."""
    resolved = []
    for source in config.limit_sources:
        ids = [account_id for alias, account_id in snapshot["accounts"] if alias == source.account]
        if not ids:
            raise ConfigError(CONFIG_UNRESOLVED, "limit_events: an account is not in the policy-ids snapshot")
        if len(ids) > 1:
            raise ConfigError(CONFIG_UNRESOLVED, "limit_events: an account alias is ambiguous in the "
                              "policy-ids snapshot")
        if _UUID_TEXT.fullmatch(ids[0]) is None:
            raise ConfigError(CONFIG_UNRESOLVED, "limit_events: an account id in the policy-ids snapshot "
                              "is not valid")
        row = ResolvedLimitSource()
        row.bus_root, row.folder = source.bus_root, source.folder
        row.os_user, row.provider = source.os_user, source.provider
        row.account, row.account_id = source.account, ids[0]
        resolved.append(row)
    return tuple(resolved)


def resolve_gateway_receipts(config, snapshot):
    """-> the GatewayReceipts with its gateway's local id filled in, or None without the block. The
    gateway alias must be in the snapshot exactly once and its id must be a UUID: otherwise the
    reporter refuses to start. Never the first of several, never a guess. The messages name no value."""
    source = config.gateway_receipts
    if source is None:
        return None
    ids = [gateway_id for alias, gateway_id in snapshot["gateways"] if alias == source.gateway]
    if not ids:
        raise ConfigError(CONFIG_UNRESOLVED, "gateway_receipts: the gateway is not in the policy-ids snapshot")
    if len(ids) > 1:
        raise ConfigError(CONFIG_UNRESOLVED, "gateway_receipts: the gateway alias is ambiguous in the "
                          "policy-ids snapshot")
    if _UUID_TEXT.fullmatch(ids[0]) is None:
        raise ConfigError(CONFIG_UNRESOLVED, "gateway_receipts: the gateway id in the policy-ids snapshot "
                          "is not valid")
    resolved = GatewayReceipts()
    for name in GatewayReceipts.__slots__:
        setattr(resolved, name, getattr(source, name))
    resolved.gateway_id = ids[0]
    return resolved


def resolve_admission(config, snapshot):
    """-> the Admission with its gateway's local id filled in, or None without the block. The alias must be in the
    snapshot exactly once with a UUID id; otherwise `reporter admit` refuses to start. The messages name no value."""
    source = config.admission
    if source is None:
        return None
    ids = [gateway_id for alias, gateway_id in snapshot["gateways"] if alias == source.gateway]
    if not ids:
        raise ConfigError(CONFIG_UNRESOLVED, "admission: the gateway is not in the policy-ids snapshot")
    if len(ids) > 1:
        raise ConfigError(CONFIG_UNRESOLVED, "admission: the gateway alias is ambiguous in the policy-ids snapshot")
    if _UUID_TEXT.fullmatch(ids[0]) is None:
        raise ConfigError(CONFIG_UNRESOLVED, "admission: the gateway id in the policy-ids snapshot is not valid")
    resolved = Admission()
    for name in Admission.__slots__:
        setattr(resolved, name, getattr(source, name))
    resolved.gateway_id = ids[0]
    return resolved


def _unresolved(agent, what):
    raise ConfigError(CONFIG_UNRESOLVED, "agent %s: %s" % (agent, what))


def resolve_agents(config, snapshot):
    """-> {agent: mapper.AgentConfig}. Every agent table must resolve to
    exactly one seat and one model of this machine's snapshot."""
    resolved = {}
    for agent in sorted(config.agents):
        row = config.agents[agent]
        seats = [s for s in snapshot["seats"]
                 if s[0] == config.machine and s[2] == row["seat"]
                 and (row["team"] is None or s[1] == row["team"])]
        if not seats:
            _unresolved(agent, "the seat is not in the snapshot for this "
                        "machine")
        if len(seats) > 1:
            _unresolved(agent, "the seat alias is ambiguous: add the team "
                        "key, or fix the snapshot")
        models = [m for m in snapshot["models"] if m[1] == row["model"]
                  and (row["account"] is None or m[0] == row["account"])]
        if not models:
            _unresolved(agent, "the model is not in the snapshot")
        if len(models) > 1:
            _unresolved(agent, "the model alias is ambiguous: add the "
                        "account key")
        if row["task_class"] not in snapshot["task_classes"]:
            _unresolved(agent, "the task class is not in the snapshot")
        resolved[agent] = mapper.AgentConfig(
            agent, seats[0][3], models[0][2], row["task_class"],
            row["effort"], row["expected_calls"], row["max_calls"],
            row["output_cap"], row["per_call_output_cap"], row["ttl_ms"])
    return resolved
