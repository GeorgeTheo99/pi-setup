"""Local update receipts and service ownership. Never stores credential values."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess

SETTINGS = (
    "PI_SHARED_AGENT_DIR", "PI_SHARED_OMLX_AGENT_DIR", "PI_SHARED_CATALOG_PI_AGENT_DIR",
    "PI_SHARED_ALIASES", "PI_SHARED_MODELS_OUT", "PI_SHARED_LAUNCHERS_OUT", "PI_SHARED_BIN_DIR",
    "PI_SETUP_ZSHRC", "PI_SETUP_NO_SHELL_RC", "LOCAL_SEARCH_INSTALL_CONFIG",
    "MODEL_GATEWAY_LAUNCHD_LABEL", "MODEL_GATEWAY_PLIST_DIR", "MODEL_GATEWAY_BIN_DIR",
)
GATEWAY_ENV = set("MODEL_GATEWAY_HOST MODEL_GATEWAY_PORT MODEL_GATEWAY_CONFIG MODEL_GATEWAY_MODEL_INFO MODEL_GATEWAY_MODEL_INFO_SOURCE MODEL_GATEWAY_LEDGER_PATH GATEWAY_VISION_FALLBACK GATEWAY_VISION_FALLBACK_LOCAL GATEWAY_VISION_FALLBACK_CLOUD GATEWAY_VISION_FALLBACK_MODE GATEWAY_VISION_FALLBACK_MAX_IMAGES GATEWAY_VISION_OBSERVATION_CACHE_TTL_SECONDS GATEWAY_VISION_EXTRACTION_TOTAL_TIMEOUT_SECONDS MODEL_GATEWAY_LOG_DIR MODEL_GATEWAY_BACKUP_DIR MODEL_GATEWAY_LEGACY_BACKUP_DIRS".split())
SEARCH_ENV = set("MCP_PORT WEBSEARCH_PROVIDER_STACK WEBSEARCH_SEARCH_MODE WEBSEARCH_TOTAL_TIMEOUT WEBSEARCH_BRAVE_TIMEOUT LOCAL_SEARCH_DATA_DIR LOCAL_SEARCH_TELEMETRY_ENABLED DECODO_FALLBACK_ENABLED DECODO_TIMEOUT DECODO_RESPONSE_MAX_BYTES WEBSEARCH_FETCH_OPERATION_TIMEOUT FASTMCP_SHOW_SERVER_BANNER".split())


def remember_settings(env):
    result = {}
    for key in SETTINGS:
        if key not in env:
            continue
        value = env[key]
        if any(c in value for c in "\r\n\0"):
            raise RuntimeError(f"Invalid remembered setting: {key}")
        if key == "MODEL_GATEWAY_LAUNCHD_LABEL" and not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise RuntimeError("Invalid gateway service label")
        if key not in {"MODEL_GATEWAY_LAUNCHD_LABEL", "PI_SETUP_NO_SHELL_RC"}:
            if key == "PI_SHARED_CATALOG_PI_AGENT_DIR" and not value:
                result[key] = value
                continue
            path = Path(value).expanduser()
            if not value or not path.is_absolute():
                raise RuntimeError(f"Use an absolute path for remembered setting {key}")
            value = str(path)
        result[key] = value
    return result


def omlx_url():
    path = Path.home() / ".omlx/settings.json"
    document = {}
    if path.exists() or path.is_symlink():
        target = path.resolve(strict=True)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 1048576:
            raise RuntimeError("Unsafe oMLX settings file")
        document = json.loads(target.read_text())
    server = document.get("server", {}) if isinstance(document, dict) else None
    if not isinstance(server, dict):
        raise RuntimeError("Invalid oMLX server settings")
    port = server.get("port", 8000)
    host = server.get("host", "127.0.0.1")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RuntimeError("Invalid oMLX port")
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        host = "127.0.0.1"
    elif host in ("::1", "::"):
        host = "[::1]"
    else:
        raise RuntimeError("Custom oMLX binding needs manual verification; no remote discovery attempted")
    return f"http://{host}:{port}/v1"


def private_plist(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"Unsafe installed service metadata: {path}")
    if info.st_size > 65536:
        raise RuntimeError("Installed service metadata exceeds size limit")
    data = plistlib.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise RuntimeError("Invalid installed service metadata")
    return data


def identity(data):
    values = {k: data.get(k) for k in ("Label", "WorkingDirectory", "ProgramArguments")}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def service_snapshot(modules, settings, omlx=None):
    home = Path.home()
    agents = home / "Library/LaunchAgents"
    gateway_dir = Path(settings.get("MODEL_GATEWAY_PLIST_DIR", str(agents))).expanduser()
    label = settings.get("MODEL_GATEWAY_LAUNCHD_LABEL", "com.local.model-gateway")
    if not label or any(c in label for c in "/\\\r\n"):
        raise RuntimeError("Invalid gateway service label")
    paths = {"model-gateway": gateway_dir / (label + ".plist"),
             "browser-worker": agents / "com.local.mcp-browser-worker.plist",
             "local_web_search": agents / "com.local.mcp-websearch.plist"}
    selected = list(modules)
    if omlx == "install":
        paths["omlx"] = agents / "homebrew.mxcl.omlx.plist"
        selected.append("omlx")
    result = {}
    for name in selected:
        path = paths.get(name)
        if path and (path.exists() or path.is_symlink()):
            # Homebrew service plists may be owner-only writable but world readable.
            data = read_service(path, name)
            result[name] = {"path": str(path), "identity": identity(data)}
    return result


def read_service(path, name):
    if name != "omlx":
        return private_plist(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 65536:
        raise RuntimeError("Unsafe oMLX service metadata")
    data = plistlib.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise RuntimeError("Invalid oMLX service metadata")
    return data


def service_environment(data):
    """Recover current component settings, not stale copies or secrets in receipts."""
    result = {}
    names = [m for m in data["modules"] if m != "pi-shared"]
    if data.get("omlx") == "install":
        names.append("omlx")
    for name in names:
        saved = data.get("services", {}).get(name)
        if not isinstance(saved, dict) or not isinstance(saved.get("path"), str):
            raise RuntimeError(f"No saved ownership for {name}; rerun setup to establish it")
        current = read_service(Path(saved["path"]), name)
        if identity(current) != saved.get("identity"):
            raise RuntimeError(f"{name} service ownership changed; refusing automatic adoption")
        env = current.get("EnvironmentVariables", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise RuntimeError(f"Invalid {name} service environment")
        if name in {"model-gateway", "local_web_search"}:
            allowed = GATEWAY_ENV if name == "model-gateway" else SEARCH_ENV
            if set(env) - allowed:
                raise RuntimeError(f"{name} has unsupported custom environment entries; refusing to discard them")
            result.update(env)
            log = current.get("StandardOutPath")
            if not isinstance(log, str) or not Path(log).is_absolute() or current.get("StandardErrorPath") != log:
                raise RuntimeError(f"Invalid {name} log path")
            if name == "model-gateway":
                result["MODEL_GATEWAY_LOG_FILE"] = log
            else:
                result["LOCAL_SEARCH_LOG_DIR"] = str(Path(log).parent)
        # Browser preserves its complete installed environment itself. oMLX
        # remains managed through Homebrew; neither environment is copied here.
    return result


def omlx_version():
    # The active opt link, not every retained keg reported by brew list.
    prefix = subprocess.check_output(["brew", "--prefix", "jundot/omlx/omlx"], text=True, timeout=15).strip()
    return Path(prefix).resolve(strict=True).name


def runtime_versions():
    return {name: subprocess.check_output([name, "--version"], text=True, timeout=10).strip()
            for name in ("node", "python3", "uv")}


def revisions(root, modules):
    return {m: subprocess.check_output(["git", "-C", str(root / m), "rev-parse", "HEAD"], text=True).strip()
            for m in modules if (root / m / ".git").exists()}
