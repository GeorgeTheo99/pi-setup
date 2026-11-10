"""pi-jobs onboarding: run a local pi-jobs server, or point this machine's Pi tools at one.

collect stays offline with --plan; apply runs only after approval. Selections are
credential-free. The only pending secret is ``token`` (a remote server's API token),
written solely to the pi-jobs client config; the caller must not persist, log, or pass
it to subprocesses. A local server's client token is minted by the pi-jobs installer.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import search_setup
import update_support

LOCAL_URL = "http://127.0.0.1:8790"
API_VERSION = 1
LABEL = "com.local.pi-jobs"
_TOKEN = re.compile(r"pjt_[A-Za-z0-9_-]{20,200}")
_PROBE_TIMEOUT = 10
_PROBE_DEADLINE = 15
_MAX_RESPONSE = 65536


def plist_path():
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def check_service_environment(service):
    """Refuse settings install.sh would silently drop, and a port other than the selected URL's."""
    env = service.get("EnvironmentVariables", {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise RuntimeError("Invalid pi-jobs service environment")
    extra = set(env) - update_support.PI_JOBS_ENV
    if extra:
        raise RuntimeError("pi-jobs has custom LaunchAgent settings (" + ", ".join(sorted(extra))
                           + ") that its installer would drop; move them into install.sh options first")
    if env.get("PI_JOBS_PORT", "8790") != "8790":
        raise RuntimeError("pi-jobs setup manages the server on port 8790 only")
    return env


def unowned_server(previous):
    """The working directory of a pi-jobs LaunchAgent this setup does not own, or None. Read-only."""
    if (previous or {}).get("services", {}).get("pi-jobs"):
        return None
    path = plist_path()
    if not path.exists() and not path.is_symlink():
        return None
    try:
        # The same owner-only checks that service ownership applies after approval.
        service = update_support.private_plist(path)
    except (RuntimeError, OSError, ValueError):
        raise RuntimeError(f"Unreadable or unsafe pi-jobs LaunchAgent {path} (needs owner-only 0600); "
                           "reconcile it before setup") from None
    if not isinstance(service, dict) or service.get("Label") != LABEL:
        raise RuntimeError(f"Unexpected pi-jobs LaunchAgent {path}; reconcile it before setup")
    check_service_environment(service)
    return str(service.get("WorkingDirectory", "unknown directory"))


def client_path():
    """Read by pi-shared's jobs_* tools; written by pi-jobs connect-local or remote setup."""
    return Path.home() / ".config/pi-jobs/client.json"


def _private_link(host):
    """Loopback and Tailscale hosts, where plain HTTP is acceptable (as in the pi-shared tools)."""
    host = host.lower()
    if host == "localhost" or host.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
            or address in ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _url(value):
    """A server base URL such as https://host.example.ts.net/pi-jobs, without a trailing slash."""
    if (not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in "?#\\")):
        raise RuntimeError("Invalid pi-jobs URL; use HTTP(S) without credentials, query or fragment")
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None
                 and parsed.password is None and "%" not in parsed.netloc
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("Invalid pi-jobs URL; use an HTTP(S) server URL without credentials")
    if parsed.scheme == "http" and not _private_link(parsed.hostname):
        raise RuntimeError("pi-jobs over plain HTTP is allowed only for loopback and tailnet hosts; use HTTPS")
    return value.rstrip("/")


def _token(value):
    value = search_setup._secret(value)
    if not _TOKEN.fullmatch(value):
        raise RuntimeError("That is not a pi-jobs API token (pjt_…); create one in pi-jobs Settings → API tokens")
    return value


def _read_token_file(value):
    try:
        return _token(search_setup._read_file(search_setup._path(value)))
    except RuntimeError as exc:
        if "pi-jobs" in str(exc):
            raise
        raise RuntimeError(f"pi-jobs token file {value} must be an absolute path to an owner-owned, regular, "
                           "single-link 0600 file holding one token") from None


def _is_loopback(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_client():
    """The saved client config as {url, token}, or None when absent."""
    path = client_path()
    search_setup._safe_path(path)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        data = json.loads(search_setup._read_file(path))
    except (RuntimeError, ValueError):
        raise RuntimeError(f"{path} must be valid owner-only (0600) JSON; fix or remove it and rerun setup") from None
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not isinstance(data.get("token"), str):
        raise RuntimeError(f"{path} needs a url and a token; fix or remove it and rerun setup")
    return data


def validate_saved(selection):
    """Validate the exact secret-free receipt shape without opening files or networking."""
    if not isinstance(selection, dict):
        raise RuntimeError("Invalid saved pi-jobs selection")
    kind = selection.get("kind")
    if kind == "skip":
        valid = selection == {"kind": "skip"}
    elif kind == "local":
        valid = selection == {"kind": "local", "url": LOCAL_URL}
    elif kind == "remote":
        valid = set(selection) == {"kind", "url"} and _url(selection["url"]) == selection["url"]
    else:
        valid = False
    if not valid:
        raise RuntimeError("Invalid saved pi-jobs selection")


def collect(args, previous, interactive, choose):
    """Return (selection, pending_secrets). --plan never prompts or reads files.

    Precedence: --pi-jobs, then the saved selection, then the guided menu, then skip.
    The callback takes (question, {choice: label}) and returns a choice.
    """
    plan = bool(getattr(args, "plan", False))
    interactive = interactive and not plan
    previous = previous or {}
    saved = previous.get("pi_jobs")
    if saved is not None:
        validate_saved(saved)
    explicit = getattr(args, "pi_jobs", None)
    token_file = getattr(args, "pi_jobs_token_file", None)
    url = None
    if explicit in {"local", "skip"}:
        kind = explicit
    elif explicit is not None:
        kind, url = "remote", _url(explicit)
    elif interactive:
        choices = {"local": f"Run a pi-jobs server on this Mac ({LOCAL_URL})",
                   "remote": "Connect this machine's Pi tools to an existing pi-jobs server",
                   "skip": "Not now (existing pi-jobs configuration is left as-is)"}
        if saved and saved["kind"] != "skip":
            choices = {"keep": "Keep the saved pi-jobs selection", **choices}
        kind = search_setup._choose(choose, "Scheduled agent jobs (pi-jobs):", choices)
        if kind == "keep":
            return saved, {}
    else:
        kind = saved["kind"] if saved else "skip"
        url = saved.get("url") if saved and kind == "remote" else None
    if token_file is not None and kind != "remote":
        raise RuntimeError("--pi-jobs-token-file requires --pi-jobs <server URL>")
    if kind == "skip":
        return {"kind": "skip"}, {}
    if kind == "local":
        return {"kind": "local", "url": LOCAL_URL}, {}
    if url is None:
        if not interactive:
            raise RuntimeError("Connecting to a pi-jobs server needs its URL: --pi-jobs https://host/pi-jobs")
        url = _url(search_setup._input("pi-jobs server URL, e.g. https://host.example.ts.net/pi-jobs (or cancel): "))
    selection = {"kind": "remote", "url": url}
    validate_saved(selection)
    if plan:
        return selection, {}
    if token_file is not None:
        return selection, {"token": _read_token_file(token_file)}
    try:
        current = _read_client()
    except RuntimeError:
        current = None  # unreadable: a new token replaces it after approval
    if current and current["url"].rstrip("/") == url and _TOKEN.fullmatch(current["token"]):
        if interactive:
            print(f"Keeping the saved pi-jobs token for {url}.")
        return selection, {}
    if not interactive:
        raise RuntimeError("Connecting to a pi-jobs server needs an API token: --pi-jobs-token-file <private 0600 file>")
    print("Create an API token in that server's pi-jobs Settings → API tokens.")
    method = search_setup._choose(choose, "pi-jobs API token:", {
        "hidden": "Paste the token using hidden input",
        "file": "Import a private token file"})
    if method == "file":
        token = _read_token_file(search_setup._input("Absolute private token file (or cancel): "))
    else:
        token = _token(search_setup._hidden("pi-jobs API token (hidden, or cancel): "))
    return selection, {"token": token}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("The pi-jobs server redirected; use its final URL (redirects are disabled)")


def _get(url, token=None):
    """One GET with an overall deadline covering connect, TLS, headers and body."""
    result = {}

    def work():
        try:
            result["value"] = _fetch(url, token)
        except BaseException as exc:  # handed to the caller below
            result["error"] = exc

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(_PROBE_DEADLINE)
    if worker.is_alive():
        raise RuntimeError("The pi-jobs server answered too slowly; check the URL and tailnet")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _fetch(url, token):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    # Never route the token through an environment or system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=_PROBE_TIMEOUT) as response:
            body = b""
            while len(body) <= _MAX_RESPONSE:
                chunk = response.read1(4096)
                if not chunk:
                    break
                body += chunk
            if len(body) > _MAX_RESPONSE:
                raise RuntimeError("The pi-jobs server's answer is too large")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise RuntimeError("The pi-jobs server rejected the token; create a new one in its Settings → API tokens") from None
        if exc.code == 403:
            raise RuntimeError("The pi-jobs server refused this host name; add it to the server's "
                               "PI_JOBS_ALLOWED_HOSTS (rerun its install.sh)") from None
        raise RuntimeError(f"The pi-jobs server answered HTTP {exc.code}") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        raise RuntimeError("The pi-jobs server is unreachable or did not answer with JSON; check the URL and tailnet") from None


def _probe(url, token):
    health = _get(f"{url}/api/health")
    if not isinstance(health, dict) or health.get("apiVersion") != API_VERSION:
        raise RuntimeError(f"The pi-jobs server does not speak API version {API_VERSION}; update pi-jobs or pi-shared")
    _get(f"{url}/api/session", token)


def _backup(path):
    """Keep a client config privately under a unique name; the original stays in place."""
    for attempt in range(100):
        backup = path.with_name(f"client.json.{time.strftime('%Y%m%d-%H%M%S')}-{attempt}.bak")
        try:
            os.link(path, backup)
        except FileExistsError:
            continue
        os.chmod(backup, 0o600)
        print(f"  Kept the previous pi-jobs client config as {backup}; revoke its token on that server "
              "(Settings → API tokens) when no longer needed.")
        return backup
    raise RuntimeError("Could not back up the existing pi-jobs client config")


def _write_client(url, token):
    path = client_path()
    search_setup._private_directory(path.parent)
    search_setup._safe_path(path)
    fd, temp = tempfile.mkstemp(prefix=".client-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"url": url, "token": token}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            _backup(path)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def restore(backup):
    """Undo apply's set-aside after a failed installation, unless something new took its place."""
    path = client_path()
    if backup and not path.exists() and not path.is_symlink():
        os.link(backup, path)


def apply(selection, secrets):
    """Before module installation. Remote: check the server and token, then save the client config.
    Local: set aside a client config that points elsewhere, so the installer connects this server;
    returns that backup, which the caller restores if installation fails."""
    validate_saved(selection)
    if not isinstance(secrets, dict) or set(secrets) - ({"token"} if selection["kind"] == "remote" else set()):
        raise RuntimeError("Invalid pending pi-jobs credentials")
    path = client_path()
    try:
        current = _read_client()
    except RuntimeError:
        if not (selection["kind"] == "remote" and "token" in secrets):
            raise
        current = None  # replaced below; an unreadable file is kept as a backup
    if selection["kind"] == "local":
        if current and not _is_loopback(current["url"]):
            backup = _backup(path)
            os.unlink(path)
            return backup
        return None
    if selection["kind"] != "remote":
        return None
    saved = current if current and current["url"].rstrip("/") == selection["url"] else None
    if "token" in secrets:
        token = _token(secrets["token"])
    elif saved:
        token = _token(saved["token"])
    else:
        raise RuntimeError("No saved pi-jobs token for this server; rerun setup with --pi-jobs-token-file")
    _probe(selection["url"], token)
    if not saved or saved["token"] != token or saved["url"] != selection["url"]:
        _write_client(selection["url"], token)
    return None


def configure(selection):
    """After install or update: confirm the client config still matches; never writes or prompts."""
    validate_saved(selection)
    if selection["kind"] == "skip":
        return
    try:
        current = _read_client()
    except RuntimeError as exc:
        print(f"  ! pi-jobs: {exc}")
        return
    if not current:
        print(f"  ! pi-jobs: {client_path()} is missing; rerun pi-shared setup to reconnect Pi's jobs_* tools.")
    elif current["url"].rstrip("/") != selection["url"] and not (
            selection["kind"] == "local" and _is_loopback(current["url"])):
        print(f"  ! pi-jobs: Pi's jobs_* tools use {current['url']}, not the selected {selection['url']}.")


def describe(selection):
    """Print only validated, credential-free selection metadata."""
    validate_saved(selection)
    kind = selection["kind"]
    if kind == "skip":
        print("  pi-jobs: not selected; existing pi-jobs configuration is left as-is.")
    elif kind == "local":
        print(f"  pi-jobs: local server (LaunchAgent on {LOCAL_URL}); Pi's jobs_* tools use it with a token "
              "minted on this Mac. Sign in to its UI with: pi-jobs token create <name>")
    else:
        print(f"  pi-jobs: connect Pi's jobs_* tools to {selection['url']} (checks /api/health and the token "
              f"after approval; saves {client_path()}, owner-only; no local server)")
