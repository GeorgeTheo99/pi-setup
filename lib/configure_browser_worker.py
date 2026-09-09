#!/usr/bin/env python3
"""Select a successfully installed local browser-worker without changing research routing.

Only browserWorker* fields are written. Tokens remain in their owner-only service
files; this configuration contains a path, never the token. Custom conflicting
client paths/endpoints are not silently overwritten.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile


def read_config(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("research config is a symlink; update its target explicitly")
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("research config must be a JSON object")
    return data


def command(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if result.returncode:
        # The operator reports its own bounded diagnostic. No token is an arg.
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"browser-worker {argv[-1]} exited {result.returncode}: {detail}")
    return result.stdout


def configure(worker: Path, path: Path) -> None:
    read_config(path)  # Fail before service checks if configuration is malformed.
    control = worker / "scripts/browser-worker"
    if not control.is_file():
        raise ValueError(f"browser-worker is not installed at {worker}")
    command([str(control), "verify"])
    fields = {}
    for line in command([str(control), "env"]).splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    port = fields.get("PORT", "")
    if not port.isascii() or not port.isdecimal() or not 1024 <= int(port) <= 65535:
        raise ValueError("browser-worker returned an invalid PORT")
    data = Path(fields.get("DATA_DIR", ""))
    if not data.is_absolute():
        raise ValueError("browser-worker DATA_DIR must be absolute")
    token = Path(fields.get("PRODUCTION_TOKEN_FILE", str(data / "tokens/pi-production")))
    if not token.is_absolute() or data.resolve() not in token.resolve().parents:
        raise ValueError("production token path must be inside the worker's private data directory")
    info = token.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("pi-production token must be a private regular file owned by this user")
    wanted = {
        "browserWorkerEnabled": True,
        "browserWorkerMcpUrl": f"http://127.0.0.1:{int(port)}/mcp",
        "browserWorkerTokenFile": str(token),
    }
    config = read_config(path)  # Preserve edits made while the service was checked.
    if "browserWorkerEnabled" in config and not isinstance(config["browserWorkerEnabled"], bool):
        raise ValueError("browserWorkerEnabled must be boolean")
    for name in ("browserWorkerMcpUrl", "browserWorkerTokenFile"):
        if name in config and config[name] != wanted[name]:
            raise ValueError(f"existing {name} selects a different worker; configuration preserved. "
                             "Review that setting explicitly before selecting this installation")
    changed = any(config.get(key) != value for key, value in wanted.items())
    if changed:
        config.update(wanted)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        fd, temporary = tempfile.mkstemp(prefix=".browser-worker-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(config, out, indent=2)
                out.write("\n")
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f"Browser-worker selected at {wanted['browserWorkerMcpUrl']}; production token path wired, research endpoints unchanged")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path.home() / ".pi/research/config.json")
    args = parser.parse_args()
    try:
        configure(args.worker_root.expanduser().resolve(), args.config.expanduser())
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        print(f"Repair: {shlex.quote(str(args.worker_root / 'install.sh'))}; then rerun pi-setup/install.sh", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
