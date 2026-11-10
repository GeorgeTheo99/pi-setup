"""Guard the reviewed single-runtime snapshot; not a live registry approval check.

Changing this pin requires the cold-cache acquisition and compatibility gates in
`docs/homebrew.md`, not just updating the expected values below.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "@earendil-works/pi-coding-agent"
VERIFIED_PI = "1.5.0"
# Installed from registry.npmjs.org with a fresh cache on 2026-11-09: macOS arm64,
# Node 26.12.1, npm 11.20.0; 123 installed, no optional or nonoptional omissions
# or version mismatches. Locked with --before=2026-11-02T12:00:00Z, so every
# package is at least seven days old.
VERIFIED_LOCK_SHA256 = "6c449f21d20d33c5756989b0fb229f48e9f541f5a06acb4c239afa604b60156c"


def test_runtime_manifest_pins_one_verified_version():
    manifest = json.loads((ROOT / "runtime/package.json").read_text())
    assert manifest["private"] is True
    assert manifest["dependencies"] == {PACKAGE: VERIFIED_PI}


def test_runtime_lock_matches_verified_dependency_snapshot():
    content = (ROOT / "runtime/package-lock.json").read_bytes()
    lock = json.loads(content)
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == {PACKAGE: VERIFIED_PI}
    assert lock["packages"][f"node_modules/{PACKAGE}"]["version"] == VERIFIED_PI
    assert hashlib.sha256(content).hexdigest() == VERIFIED_LOCK_SHA256
