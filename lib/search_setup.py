"""Offline search onboarding. collect never writes; apply runs only after approval.

Selections are JSON-safe references, never credentials. Pending secrets use only
``brave_key`` (local provider) and ``search_key`` (existing broker bearer token).
The caller must not persist, log, or pass that second dictionary to subprocesses.
"""
from __future__ import annotations

import getpass
import ipaddress
import json
import os
from pathlib import Path
import stat
import tempfile
import urllib.parse
import uuid
import warnings


BRAVE_SIGNUP = "https://api-dashboard.search.brave.com/"


def _path(value):
    if (not isinstance(value, str) or not value or not Path(value).is_absolute()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or ".." in Path(value).parts):
        raise RuntimeError("Search paths must be absolute paths without traversal or control characters")
    return Path(value)


def _safe_path(path):
    """Do not resolve symlinks: reject them, including dangling ancestors."""
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Search paths must not contain symlinks")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Search path ancestor is not a directory")
        # Root-owned sticky temp ancestors are safe; user-writable peer
        # directories without the sticky bit are not.
        sticky_root = (stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                       and info.st_mode & stat.S_ISVTX)
        if (info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not sticky_root)):
            raise RuntimeError("Search paths must not be writable by other users")


def _read_file(path, *, private=True):
    _safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_size > 1048576
                    or (private and stat.S_IMODE(info.st_mode) != 0o600)):
                raise RuntimeError("Search key/config file must be owner-owned, regular, single-link and private (0600)")
            raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise RuntimeError("Search key/config file is too large")
            return raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise RuntimeError("Search key/config file is missing, inaccessible, or invalid") from None


def _secret(value):
    if not isinstance(value, str):
        raise RuntimeError("Search credential must be a nonempty single-line token")
    value = value.rstrip("\r\n")
    if not value or len(value) > 16383 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise RuntimeError("Search credential must be a nonempty single-line token")
    return value


def _hidden(prompt):
    # getpass otherwise falls back to echoed stdin when no controlling TTY exists.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise RuntimeError("Hidden input is unavailable; use an owner-only credential file") from None
    if value.strip().lower() == "cancel":
        raise KeyboardInterrupt
    return _secret(value)


def _input(prompt):
    value = input(prompt).strip()
    if value.lower() == "cancel":
        raise KeyboardInterrupt
    return value


def _choose(choose, question, choices):
    answer = choose(question, choices)
    if answer == "cancel":
        raise KeyboardInterrupt
    if answer not in choices:
        raise RuntimeError("Invalid search setup choice")
    return answer


def _url(value, authenticated=False):
    if (not isinstance(value, str) or not value
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in "?#\\")):
        raise RuntimeError("Invalid search URL; use HTTP(S) without credentials, query, fragment or control characters")
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and "%" not in parsed.netloc
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
        # Reject escaped control characters as well as literal ones.
        decoded = urllib.parse.unquote(parsed.path)
        valid = valid and not any(ord(c) < 32 or ord(c) == 127 for c in decoded)
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("Invalid search URL; use an HTTP(S) MCP endpoint without credentials")
    if authenticated and parsed.scheme == "http":
        loopback = parsed.hostname.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise RuntimeError("Authenticated search requires HTTPS or a loopback HTTP endpoint")
    return value


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise RuntimeError("Search port must be an integer from 1 to 65535")
    return value


def validate_saved(selection):
    """Validate exact secret-free receipt shape without opening files or networking."""
    if not isinstance(selection, dict):
        raise RuntimeError("Invalid saved search selection")
    kind = selection.get("kind")
    if kind == "skip":
        valid = set(selection) == {"kind", "url"} and selection["url"] is None
    elif kind == "local":
        valid = set(selection) == {"kind", "url", "data_dir", "port"}
        if valid:
            _path(selection["data_dir"])
            port = _port(selection["port"])
            valid = selection["url"] == f"http://127.0.0.1:{port}/mcp"
    elif kind == "existing":
        valid = set(selection) in ({"kind", "url"}, {"kind", "url", "key_file"})
        if valid:
            _url(selection["url"], "key_file" in selection)
            if "key_file" in selection:
                _path(selection["key_file"])
    else:
        valid = False
    if not valid:
        raise RuntimeError("Invalid saved search selection")


def _persisted_settings(path):
    if not path.exists() and not path.is_symlink():
        return {}
    # Match the broker's literal KEY=value format and first-match semantics.
    # These are not shell assignments: spaces, #, quotes and backslashes are data.
    values = {}
    for line in _read_file(path).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"MCP_PORT", "LOCAL_SEARCH_DATA_DIR"}:
            values.setdefault(key, value)
    return values


def collect(args, previous, env, code_root, interactive, choose):
    """Return (selection, pending_secrets); only mutate local settings in env.

    --plan never prompts or reads credentials/install config. The callback takes
    (question, {choice: label}) and returns a choice. Cancellation/EOF propagates.
    """
    plan = bool(getattr(args, "plan", False))
    interactive = interactive and not plan
    previous = previous or {}
    saved = previous.get("search")
    if saved is not None:
        validate_saved(saved)
    explicit = getattr(args, "search", None)
    legacy = bool(getattr(args, "with_search", False))
    if explicit not in {None, "local", "existing", "skip"}:
        raise RuntimeError("Invalid --search choice")
    if legacy and explicit not in {None, "local"}:
        raise RuntimeError("--with-search conflicts with --search existing/skip")
    kind = explicit or ("local" if legacy else None)
    kept = False
    if kind is None and interactive:
        choices = {"local": "Install local Brave search", "existing": "Connect an existing MCP endpoint",
                   "skip": "No new search provisioning (existing tools/config stay enabled)"}
        if saved:
            choices = {"keep": "Keep saved search selection", **choices}
        kind = _choose(choose, "Web search and page retrieval:", choices)
        if kind == "keep":
            kind = saved["kind"]
            kept = True
    if kind is None:
        kind = saved["kind"] if saved else (
            "local" if "local_web_search" in previous.get("modules", ()) else "skip")
    url_arg = getattr(args, "search_url", None)
    key_arg = getattr(args, "search_key_file", None)
    brave_arg = getattr(args, "brave_key_file", None)
    port_arg = getattr(args, "search_port", None)
    if (url_arg is not None or key_arg is not None) and kind != "existing":
        raise RuntimeError("--search-url and --search-key-file require --search existing")
    if (brave_arg is not None or port_arg is not None) and kind != "local":
        raise RuntimeError("--brave-key-file and --search-port require --search local")
    if kind == "skip":
        return {"kind": "skip", "url": None}, {}
    secrets = {}
    if kind == "existing":
        prior = saved if saved and saved["kind"] == "existing" else {}
        url = url_arg if url_arg is not None else prior.get("url")
        if interactive and url_arg is None and not kept:
            prompt = f"MCP HTTP(S) endpoint URL [{url}] (Enter to keep, or cancel): " if url else "MCP HTTP(S) endpoint URL (or cancel): "
            url = _input(prompt) or url
        if not url:
            raise RuntimeError("--search existing requires --search-url (or a saved endpoint).")
        url = _url(url)
        print("Existing MCP must implement compatible web_search and web_fetch tools; a generic MCP server is not sufficient.")
        key_file = key_arg
        saved_key = prior.get("key_file") if url == prior.get("url") else None
        if key_file is None and (kept or not interactive):
            key_file = saved_key
        if key_file is None and interactive and not kept:
            choices = {"none": "No bearer authentication", "file": "Use an existing private bearer key file",
                       "hidden": "Enter a bearer token using hidden input"}
            if saved_key:
                choices = {"keep": "Keep the saved bearer key file", **choices}
            method = _choose(choose, "Existing MCP authentication:", choices)
            if method == "keep":
                key_file = saved_key
            elif method == "file":
                key_file = _input("Absolute private bearer key file (or cancel): ")
            elif method == "hidden":
                _url(url, True)  # Reject unsafe transport before requesting a token.
                target = Path.home() / ".pi/research/search_key"
                if target.exists() or target.is_symlink():
                    # Reconfiguration retains the old credential. Reserve no file
                    # before approval; apply still uses exclusive creation.
                    target = target.with_name("search_key-" + uuid.uuid4().hex)
                key_file = str(target)
                secrets["search_key"] = _hidden("MCP bearer token (hidden, or cancel): ")
        selection = {"kind": kind, "url": url}
        if key_file is not None:
            path = _path(key_file)
            _url(url, True)
            if not plan and "search_key" not in secrets:
                _secret(_read_file(path))
            selection["key_file"] = str(path)
    else:
        prior = saved if saved and saved["kind"] == "local" else {}
        repo_data = Path(code_root) / "local_web_search/data"
        # The broker persists install settings in its repository data directory,
        # even when runtime data lives elsewhere (verified scripts/local-search).
        config = _path(env.get("LOCAL_SEARCH_INSTALL_CONFIG") or str(repo_data / "install.env"))
        persisted = {} if plan else _persisted_settings(config)
        data_dir = env.get("LOCAL_SEARCH_DATA_DIR") or prior.get("data_dir") or persisted.get("LOCAL_SEARCH_DATA_DIR")
        if not data_dir:
            data_dir = str(repo_data if (repo_data / "brave_key").exists() else
                           Path.home() / ".local/share/pi-shared/search")
        data = _path(data_dir)
        port = port_arg if port_arg is not None else env.get("MCP_PORT", prior.get("port", persisted.get("MCP_PORT")))
        if port is None:
            port = 8889
        if isinstance(port, str) and port.isascii() and port.isdigit():
            port = int(port)
        port = _port(port)
        selection = {"kind": kind, "url": f"http://127.0.0.1:{port}/mcp",
                     "data_dir": str(data), "port": port}
        target = data / "brave_key"
        if brave_arg is not None:
            source = _path(brave_arg)
            if not plan:
                secrets["brave_key"] = _secret(_read_file(source))
        elif not plan:
            _safe_path(target)
            exists = target.exists()
            if interactive and not kept:
                print(f"Local search requires a Brave Search API key. Sign up: {BRAVE_SIGNUP}")
                choices = {"file": "Import a private Brave key file", "hidden": "Enter a Brave key using hidden input"}
                if exists:
                    choices = {"reuse": "Reuse the existing local Brave key", **choices}
                method = _choose(choose, "Brave credential:", choices)
                if method == "file":
                    secrets["brave_key"] = _secret(_read_file(_path(_input("Absolute private Brave key file (or cancel): "))))
                elif method == "hidden":
                    secrets["brave_key"] = _hidden("Brave API key (hidden, or cancel): ")
                else:
                    _secret(_read_file(target))
            elif exists:
                _secret(_read_file(target))
            elif not (legacy or (explicit is None and not saved and "local_web_search" in previous.get("modules", ()))):
                raise RuntimeError(f"Local search needs a private Brave key before installation; use --brave-key-file. Sign up: {BRAVE_SIGNUP}")
        env.update(LOCAL_SEARCH_DATA_DIR=str(data), MCP_PORT=str(port))
    validate_saved(selection)
    return selection, secrets


def _private_directory(path):
    _safe_path(path)
    if not path.exists():
        if not path.parent.exists():
            _private_directory(path.parent)
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("Search credential/config parent must be an owner-owned directory")
    # Only approved apply/configure calls reach here. Tighten an existing owned
    # directory rather than breaking installations created with a normal umask.
    path.chmod(0o700)


def _key_preflight(path, value):
    _safe_path(path)
    if path.exists():
        existing = _secret(_read_file(path))
        if value is not None and existing != value:
            raise RuntimeError("Search key already exists with a different value; refusing to overwrite it")
    elif value is None:
        raise RuntimeError("Search bearer key file is missing")


def _provision(path, value):
    _private_directory(path.parent)
    _key_preflight(path, value)
    if path.exists():
        return
    # Exclusive creation never truncates an existing credential, even on a race.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value + "\n")


def apply(selection, secrets, env):
    """Apply an approved selection. No network/service operations; return None.

    Missing local keys without pending secrets are left to the component installer
    (legacy --with-search compatibility); collect enforces the new CLI policy.
    """
    validate_saved(selection)
    kind = selection["kind"]
    allowed = {"brave_key"} if kind == "local" else {"search_key"} if kind == "existing" else set()
    if not isinstance(secrets, dict) or set(secrets) - allowed:
        raise RuntimeError("Invalid pending search credentials")
    if kind == "skip":
        return
    pending = {name: _secret(value) for name, value in secrets.items()}
    if "search_key" in pending and "key_file" not in selection:
        raise RuntimeError("Pending bearer credential needs a key file")
    # Validate existing config before provisioning any key.
    config_path, config = _configuration(selection)
    key = None
    value = None
    if kind == "local":
        data = _path(selection["data_dir"])
        _safe_path(data)
        key = data / "brave_key"
        _safe_path(key)
        value = pending.get("brave_key")
        if value is not None or key.exists():
            _key_preflight(key, value)
    elif "key_file" in selection:
        key = _path(selection["key_file"])
        value = pending.get("search_key")
        _key_preflight(key, value)
    _private_directory(config_path.parent)
    if kind == "local":
        _private_directory(data)
        env.update(LOCAL_SEARCH_DATA_DIR=str(data), MCP_PORT=str(selection["port"]))
    if value is not None:
        _provision(key, value)
    _write_config(config_path, config)


def _configuration(selection):
    config_path = Path.home() / ".pi/research/config.json"
    _safe_path(config_path)
    config = {}
    if config_path.exists():
        try:
            config = json.loads(_read_file(config_path, private=False))
        except ValueError:
            raise RuntimeError("Invalid research configuration JSON; refusing to replace it") from None
        if not isinstance(config, dict):
            raise RuntimeError("Research configuration must be a JSON object")
    config.pop("mcpUrl", None)
    config["websearchMcpUrl"] = selection["url"]
    for name in ("websearchMcpKeyFile", "websearchMcpKeyUrl"):
        config.pop(name, None)
    if selection["kind"] == "existing" and "key_file" in selection:
        config.update(websearchMcpKeyFile=selection["key_file"], websearchMcpKeyUrl=selection["url"])
    return config_path, config


def _write_config(config_path, config):
    _private_directory(config_path.parent)
    # Atomic replacement avoids partial JSON, with a private temporary file.
    fd, temp = tempfile.mkstemp(prefix=".config-", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(config, stream, indent=2)
            stream.write("\n")
        _safe_path(config_path)
        os.replace(temp, config_path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def configure(selection):
    """Reassert approved client config after component install/update.

    Does not provision credentials, change local service settings, or contact the
    endpoint. Skip is a complete no-op. Existing bearer references are rechecked.
    """
    validate_saved(selection)
    if selection["kind"] == "skip":
        return
    if selection["kind"] == "existing" and "key_file" in selection:
        _key_preflight(_path(selection["key_file"]), None)
    _write_config(*_configuration(selection))


def describe(selection):
    """Print only validated, credential-free selection metadata."""
    validate_saved(selection)
    if selection["kind"] == "skip":
        print("Search: no new provisioning; existing search configuration/tools are preserved.")
    else:
        print(f"Search: {selection['kind']} — {selection['url']}")
        if selection["kind"] == "local":
            print(f"Search data: {selection['data_dir']} (port {selection['port']})")
        elif "key_file" in selection:
            print(f"Search bearer key file: {selection['key_file']} (scoped to this exact endpoint)")
