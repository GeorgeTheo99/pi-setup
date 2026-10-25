"""Guard the reviewed single-runtime snapshot; not a live registry approval check.

Changing this pin requires the cold-cache acquisition and compatibility gates in
`docs/homebrew.md`, not just updating the expected values below.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "@earendil-works/pi-coding-agent"
VERIFIED_PI = "1.3.0"
# Installed from the approved registry with a fresh cache on 2026-10-25:
# macOS arm64, Node 26.12.0, npm 11.20.0; 118 installed, no optional or
# nonoptional omissions or version mismatches.
VERIFIED_LOCK_SHA256 = "a7137c4a62a83324d776e21f4bc4b90e0408e54c2b6adc7d60685ef55f33c210"


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
