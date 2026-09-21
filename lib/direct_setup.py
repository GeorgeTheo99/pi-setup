"""Fail closed when an older shared installer ignores the direct-only policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys


def verify(config_path, shared_dir):
    """Check policy metadata offline; the shared doctor validates full routing."""
    try:
        path = Path(config_path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 1048576):
            raise ValueError("unsafe launcher config")
        config = json.loads(path.read_text())
        generation = config.get("generation") if isinstance(config, dict) else None
        args = generation.get("args") if isinstance(generation, dict) else None
        # No catalog, model output or gateway profile options may be retained.
        prefix = ["--direct-only", "--shared-dir", str(Path(shared_dir).resolve())]
        if (not isinstance(args, list) or len(args) != 4 or args[:3] != prefix
                or args[3] not in ("--direct-launchers", "--no-direct-launchers")):
            raise ValueError("missing or inconsistent --direct-only generation metadata")
    except (OSError, ValueError) as exc:
        raise RuntimeError("Direct-only launcher verification failed; a compatible shared installer must record "
                           "--direct-only. Setup/update is incomplete; review the shared version and launcher config.") from exc


if __name__ == "__main__":
    try:
        verify(*sys.argv[1:])
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
