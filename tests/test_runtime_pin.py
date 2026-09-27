"""Guard the reviewed single-runtime snapshot; not a live registry approval check.

Changing this pin requires the cold-cache acquisition and compatibility gates in
`docs/homebrew.md`, not just updating the expected values below.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "@earendil-works/pi-coding-agent"
VERIFIED_PI = "0.87.1"
# Reacquired from approved public npm with fresh cache on 2026-09-27:
# macOS arm64, Node 26.10.0, npm 11.19.1; 144 installed, no nonoptional omissions or version mismatches.
VERIFIED_LOCK_SHA256 = "58713fc3b479b762ebb6503fd2bbb2476571be3e7b47c7359297d0f09d486735"


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
