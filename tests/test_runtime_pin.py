"""Guard the reviewed single-runtime snapshot; not a live registry approval check.

Changing this pin requires the cold-cache acquisition and compatibility gates in
`docs/homebrew.md`, not just updating the expected values below.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "@earendil-works/pi-coding-agent"
VERIFIED_PI = "0.85.1"
# Released in setup 0.1.9; reacquired with all 165 versions matching on 2026-09-24.
VERIFIED_LOCK_SHA256 = "e939db3e7125d2e80cec61c4a1fed8fb0733a91d3f2ca59ca9e9f79fc0a97e3e"


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
