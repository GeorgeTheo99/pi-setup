"""Search onboarding. collect stays offline; apply runs only after approval.

Selections are JSON-safe references, never credentials. Pending secrets use only
``brave_key`` and the optional ``decodo_key`` (local provider) and ``search_key``
(existing broker bearer token).
The caller must not persist, log, or pass that second dictionary to subprocesses.
"""
from __future__ import annotations

import getpass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings


BRAVE_SIGNUP = "https://api-dashboard.search.brave.com/"
DECODO_NOTE = ("Optional: a Decodo Web Scraping API token lets local search recover pages that block "
               "direct fetches. Use only the value after 'Basic' in the Playground Authorization header. "
               "Without it, recovery falls back to Jina Reader.")
_PROBE_TIMEOUT = 10
_PROBE_FAILURE = "Search tools/list could not connect or read a response; check endpoint, TLS and reachability"


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
    decodo_arg = getattr(args, "decodo_key_file", None)
    port_arg = getattr(args, "search_port", None)
    if (url_arg is not None or key_arg is not None) and kind != "existing":
        raise RuntimeError("--search-url and --search-key-file require --search existing")
    if (brave_arg is not None or decodo_arg is not None or port_arg is not None) and kind != "local":
        raise RuntimeError("--brave-key-file, --decodo-key-file and --search-port require --search local")
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
        # Decodo is optional: only an explicit flag or a fresh interactive answer
        # provisions it, and an existing token is always kept unchanged.
        decodo = data / "decodo_key"
        if decodo_arg is not None:
            source = _path(decodo_arg)
            if not plan:
                secrets["decodo_key"] = _secret(_read_file(source))
        elif interactive and not kept:
            _safe_path(decodo)
            if decodo.exists():
                _secret(_read_file(decodo))
                print("Decodo fetch fallback: keeping the existing private token.")
            else:
                print(DECODO_NOTE)
                method = _choose(choose, "Decodo fetch fallback (optional):", {
                    "skip": "Skip Decodo (recommended if you have no token)",
                    "file": "Import a private Decodo token file",
                    "hidden": "Enter a Decodo token using hidden input"})
                if method == "file":
                    secrets["decodo_key"] = _secret(_read_file(_path(_input("Absolute private Decodo token file (or cancel): "))))
                elif method == "hidden":
                    secrets["decodo_key"] = _hidden("Decodo token (hidden, or cancel): ")
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Search endpoint redirected tools/list; use the final MCP URL (redirects are disabled)")


def _validate_tools(tools):
    """Check advertised names/basic argument shapes, not arbitrary JSON Schema semantics."""
    contracts = {"web_search": {"query": "string", "num_results": "integer"},
                 "web_fetch": {"url": "string", "max_chars": "integer"}}
    for name, arguments in contracts.items():
        matches = [tool for tool in tools if tool.get("name") == name]
        if not matches:
            raise RuntimeError(f"Search endpoint is missing required tool {name}; a generic MCP server is not sufficient")
        if len(matches) != 1:
            raise RuntimeError(f"Search inventory has duplicate {name} definitions")
        schema = matches[0].get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise RuntimeError(f"Search tool {name} must advertise an object inputSchema")
        properties = schema.get("properties")
        required = schema.get("required", [])
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)):
            raise RuntimeError(f"Search tool {name} has an invalid argument schema")
        if set(required) - arguments.keys():
            raise RuntimeError(f"Search tool {name} requires arguments the Pi client does not send")
        for scope in [schema, *(properties.get(key) for key in arguments)]:
            if isinstance(scope, dict) and set(scope) & {"$ref", "allOf", "anyOf", "oneOf", "not", "if"}:
                raise RuntimeError(f"Cannot verify composed/referenced inputSchema for {name}; advertise explicit argument types")
        for key, expected in arguments.items():
            prop = properties.get(key)
            types = prop.get("type") if isinstance(prop, dict) else None
            types = [types] if isinstance(types, str) else types
            allowed = {expected, "number"} if expected == "integer" else {expected}
            if (not isinstance(types, list) or not all(isinstance(item, str) for item in types)
                    or not allowed.intersection(types)):
                raise RuntimeError(f"Search tool {name} must accept {key} as {expected}")


def _probe_existing(url, token=None):
    """Bound the entire inventory, including DNS, headers, body and pagination.

    A socket timeout only bounds inactivity, and Python signal handlers cannot
    reliably interrupt libc DNS resolution. Isolate the synchronous probe in a
    process instead: subprocess.run kills and reaps it before timeout returns.
    Credentials travel only over stdin, never argv, environment or disk. This
    works on macOS/Linux without forking a potentially multithreaded interpreter.
    """
    _url(url, token is not None)
    if token is not None:
        token = _secret(token)
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe-existing"],
            input=json.dumps([url, token]), text=True, capture_output=True,
            timeout=_PROBE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Search tools/list exceeded the overall inventory deadline") from None
    except OSError:
        raise RuntimeError(_PROBE_FAILURE) from None
    if result.returncode != 0:
        # Only our worker's controlled diagnostic is safe to display. Never
        # surface stderr (tracebacks) or subprocess exceptions containing input.
        if result.returncode == 1:
            try:
                message = json.loads(result.stdout)
            except (ValueError, TypeError):
                message = None
            if isinstance(message, str):
                raise RuntimeError(message) from None
        raise RuntimeError(_PROBE_FAILURE)
    print("Search inventory checked: web_search/web_fetch names and basic argument types; "
          "tools/call compatibility and provider readiness NOT tested (no tool calls).")


def _read_inventory(url, token=None):
    """Read tools/list only: no initialize, session, SSE or real tool calls.

    Inventory success cannot prove tools/call transport or provider readiness.
    Never include response bodies/headers or underlying errors in diagnostics.
    """
    _url(url, token is not None)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + _secret(token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    tools, cursors = [], set()
    params = {}
    for page in range(10):
        request_id = f"pi-search-setup-{page}"
        payload = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": params}
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with opener.open(request, timeout=_PROBE_TIMEOUT) as response:
                if response.headers.get_content_type() == "text/event-stream":
                    raise RuntimeError("Search endpoint returned SSE; Pi requires direct HTTP JSON, not an SSE stream")
                if response.headers.get("Mcp-Session-Id") is not None:
                    raise RuntimeError("Search endpoint issued an MCP session; use a session-free HTTP JSON endpoint")
                if response.headers.get_content_type() != "application/json":
                    raise RuntimeError("Search endpoint must return application/json for direct tools/list requests")
                raw = response.read(1048577)
            if len(raw) > 1048576:
                raise RuntimeError("Search tools/list response exceeded the 1 MiB size limit")
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise RuntimeError("Search tools/list authentication failed; check --search-key-file and endpoint access") from None
            raise RuntimeError(f"Search tools/list returned HTTP {status}; use direct HTTP JSON without redirects, initialize, session or SSE negotiation") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise RuntimeError(_PROBE_FAILURE) from None
        except (ValueError, RecursionError):
            raise RuntimeError("Search tools/list returned invalid JSON; Pi requires direct HTTP JSON, not SSE") from None
        if (not isinstance(data, dict) or data.get("jsonrpc") != "2.0"
                or data.get("id") != request_id):
            raise RuntimeError("Search tools/list returned an invalid JSON-RPC response")
        if "error" in data:
            raise RuntimeError("Search tools/list failed; the endpoint must support inventory without initialize/session negotiation")
        result = data.get("result")
        if (not isinstance(result, dict) or not isinstance(result.get("tools"), list)
                or any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                       for tool in result["tools"])):
            raise RuntimeError("Search tools/list returned an invalid tool inventory")
        tools.extend(result["tools"])
        cursor = result.get("nextCursor")
        if cursor is None:
            _validate_tools(tools)
            return
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise RuntimeError("Search tools/list returned an invalid or repeated pagination cursor")
        cursors.add(cursor)
        params = {"cursor": cursor}
    raise RuntimeError("Search tools/list exceeded the 10-page inventory limit")


def apply(selection, secrets, env):
    """Apply an approved selection; probe existing inventory before any search writes.

    Missing local keys without pending secrets are left to the component installer
    (legacy --with-search compatibility); collect enforces the new CLI policy.
    """
    validate_saved(selection)
    kind = selection["kind"]
    allowed = {"brave_key", "decodo_key"} if kind == "local" else {"search_key"} if kind == "existing" else set()
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
        decodo = data / "decodo_key"
        decodo_value = pending.get("decodo_key")
        if decodo_value is not None:
            _safe_path(decodo)
            _key_preflight(decodo, decodo_value)
    elif "key_file" in selection:
        key = _path(selection["key_file"])
        value = pending.get("search_key")
        _key_preflight(key, value)
    if kind == "existing":
        token = value
        if token is None and key is not None:
            token = _secret(_read_file(key))
        _probe_existing(selection["url"], token)
    _private_directory(config_path.parent)
    if kind == "local":
        _private_directory(data)
        env.update(LOCAL_SEARCH_DATA_DIR=str(data), MCP_PORT=str(selection["port"]))
    if value is not None:
        _provision(key, value)
    if kind == "local" and decodo_value is not None:
        _provision(decodo, decodo_value)
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
        else:
            if "key_file" in selection:
                print(f"Search bearer key file: {selection['key_file']} (scoped to this exact endpoint)")
            print("Search setup approval permits POST tools/list inventory checks before saving routing; "
                  "no tools/call, initialize, session or SSE negotiation. Plans and updates stay offline.")


if __name__ == "__main__":
    if sys.argv[1:] != ["--probe-existing"]:
        sys.exit(2)
    try:
        _read_inventory(*json.load(sys.stdin))
    except RuntimeError as exc:
        print(json.dumps(str(exc)))
        sys.exit(1)
    except Exception:
        # No endpoint, credential, response or underlying exception diagnostics.
        print(json.dumps(_PROBE_FAILURE))
        sys.exit(1)
