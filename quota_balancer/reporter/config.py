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

import json
import os
import re
import sys
import tomllib
from urllib.parse import urlsplit

from . import decisions, mapper
from .events import _NAME as _AGENT_NAME
from ..validate import ALIAS_RE

CONFIG_VERSION = 1
MAX_FILE_BYTES = 65536
_INT64 = 2 ** 63 - 1
_LOOPBACK = ("127.0.0.1", "::1", "localhost")

TOP_REQUIRED = ("version", "base_url", "identity", "machine", "journal_root",
                "state_path", "policy_ids")
TOP_OPTIONAL = ("agents", "poll_seconds", "silent_after_seconds")
AGENT_REQUIRED = ("seat", "model", "task_class", "effort", "expected_calls",
                  "max_calls", "output_cap", "per_call_output_cap", "ttl_ms")
AGENT_OPTIONAL = ("account", "team")
_CEILINGS = ("expected_calls", "max_calls", "output_cap",
             "per_call_output_cap", "ttl_ms")

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
                 "poll_seconds", "silent_after_seconds", "agents")

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
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        _invalid("agents: must be a table of agent tables")
    config.agents = {}
    for name, table in agents.items():
        if not isinstance(name, str) or not _AGENT_NAME.fullmatch(name):
            _invalid("agents: an agent name is not valid")
        config.agents[name] = _agent_table(name, table)
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
    return parse_config(text)


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
        if not all(isinstance(v, str) for row in seats + models
                   for v in row) \
                or not all(isinstance(t, str) for t in task_classes):
            raise TypeError
    except (KeyError, TypeError):
        raise ConfigError(CONFIG_UNRESOLVED,
                          "the policy-ids snapshot has an unexpected shape"
                          ) from None
    return {"seats": seats, "models": models, "task_classes": task_classes}


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
