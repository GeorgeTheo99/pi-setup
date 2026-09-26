"""Direct gateway references: local validation only; shared pi-gateway owns discovery."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import stat
import urllib.parse


def normalize_url(url):
    # Validate before displaying or persisting a URL. Never echo rejected input:
    # it may contain a credential, query parameter, or terminal escape sequence.
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise RuntimeError("Invalid gateway URL; use an HTTPS base URL without credentials")
    try:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.removesuffix("/")
        segments = path.split("/")[1:] if path else []
        valid = (parsed.scheme in {"https", "http"} and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not any(c in url for c in "?#\\%")
                 # A repeated API suffix would normalize again on saved reruns.
                 and not path.endswith("/v1/v1")
                 and all(re.fullmatch(r"[A-Za-z0-9._~-]+", segment)
                         and segment not in {".", ".."} for segment in segments)
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("Invalid gateway URL; use a base URL with a safe optional path prefix (/v1 optional), without credentials, query or fragment")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path.removesuffix("/v1"), "", ""))


def connection(url, key_file, allow_private_http=False):
    normalized = normalize_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    if not isinstance(allow_private_http, bool):
        raise RuntimeError("Invalid gateway HTTP opt-in")
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            raise RuntimeError("Private HTTP requires a numeric private/Tailscale IP; use HTTPS for hostnames") from None
        networks = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                    "100.64.0.0/10", "::1/128", "fc00::/7")
        if not allow_private_http or not any(address in ipaddress.ip_network(net) for net in networks):
            raise RuntimeError("HTTP requires --allow-private-http and a private/Tailscale IP; prefer HTTPS")
    if (not isinstance(key_file, str) or not key_file or not Path(key_file).is_absolute()
            or any(ord(c) < 32 or ord(c) == 127 for c in key_file)):
        raise RuntimeError("--gateway-key-file must be an absolute file path, not a credential value")
    return {"url": normalized,
            "key_file": key_file, "allow_private_http": allow_private_http}


def validate_saved(data):
    if (not isinstance(data, dict) or set(data) != {"url", "key_file", "allow_private_http"}
            or connection(data["url"], data["key_file"], data["allow_private_http"]) != data):
        raise RuntimeError("Invalid saved external gateway connection")


def check_key_file(data):
    # Do not read the credential; the shared helper revalidates when opening it.
    path = Path(data["key_file"])
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError("Gateway key file path must not contain symlinks")
    try:
        info = path.lstat()
    except OSError:
        raise RuntimeError("Gateway key file is missing or inaccessible; provision it separately") from None
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise RuntimeError("Gateway key file must be a regular, non-symlink file owned by you with mode 0600 and one link")


def command(data, action):
    gateway = data["external_gateway"]
    return [Path(data["code_root"]) / "pi-shared/bin/pi-gateway", action,
            "--url", gateway["url"], "--key-file", gateway["key_file"],
            "--cli-out", data["cli_file"],
            *(["--allow-private-http"] if gateway["allow_private_http"] else [])]
