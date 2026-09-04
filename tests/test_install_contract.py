"""Installer contract tests: managed ~/.zshrc block and truthful exit status.

install.sh is exercised with a temporary HOME, an empty manifest (no module
clones), a stub `pi` on PATH, and a stub doctor so no network is used.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SETUP_ROOT = Path(__file__).resolve().parents[1]

RC_BLOCK = (
    "# >>> pi-setup generated launchers >>>\n"
    '[ -f "$HOME/.pi/generated/pi-launchers.zsh" ] && source "$HOME/.pi/generated/pi-launchers.zsh"\n'
    "# <<< pi-setup generated launchers <<<\n"
)


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def setup(tmp_path):
    """A copy of pi-setup with an empty manifest and a controllable doctor."""
    root = tmp_path / "pi-setup"
    (root / "bin").mkdir(parents=True)
    shutil.copy(SETUP_ROOT / "install.sh", root / "install.sh")
    (root / "manifest.yaml").write_text("version: 1\nmodules: {}\n")
    _exe(root / "bin" / "doctor", '#!/bin/sh\nexit "${FAKE_DOCTOR_RC:-0}"\n')
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _exe(fakebin / "pi", "#!/bin/sh\necho 0.0.0\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(fakebin), str(Path(shutil.which("python3")).parent),
                                 str(Path(shutil.which("git")).parent), "/usr/bin", "/bin"]),
        "PI_SETUP_CODE_ROOT": str(tmp_path / "code"),
    }
    return {"root": root, "home": home, "env": env}


def _install(fx, **extra) -> subprocess.CompletedProcess:
    env = {**fx["env"], **{k: str(v) for k, v in extra.items()}}
    return subprocess.run([str(fx["root"] / "install.sh")], env=env, capture_output=True, text=True)


def test_fresh_zshrc_gets_one_managed_block(setup):
    r = _install(setup)
    assert r.returncode == 0, r.stdout + r.stderr
    rc = (setup["home"] / ".zshrc").read_text()
    assert rc == RC_BLOCK


def test_existing_zshrc_preserved_and_backed_up(setup):
    rc = setup["home"] / ".zshrc"
    rc.write_text("export FOO=1\nalias ll='ls -l'")  # no trailing newline on purpose
    r = _install(setup)
    assert r.returncode == 0, r.stderr
    assert rc.read_text() == "export FOO=1\nalias ll='ls -l'\n" + RC_BLOCK
    backups = list(setup["home"].glob(".zshrc.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == "export FOO=1\nalias ll='ls -l'"


def test_repeated_install_does_not_duplicate_block(setup):
    assert _install(setup).returncode == 0
    assert _install(setup).returncode == 0
    rc = (setup["home"] / ".zshrc").read_text()
    assert rc.count("pi-setup generated launchers >>>") == 1
    assert not list(setup["home"].glob(".zshrc.bak-*"))  # nothing pre-existing to back up


def test_unmanaged_existing_source_line_is_left_alone(setup):
    rc = setup["home"] / ".zshrc"
    line = 'source "$HOME/.pi/generated/pi-launchers.zsh"\n'
    rc.write_text(line)
    assert _install(setup).returncode == 0
    assert rc.read_text() == line


def test_opt_out_leaves_zshrc_untouched(setup):
    rc = setup["home"] / ".zshrc"
    rc.write_text("x=1\n")
    assert _install(setup, PI_SETUP_NO_SHELL_RC=1).returncode == 0
    assert rc.read_text() == "x=1\n"
    assert not list(setup["home"].glob(".zshrc.bak-*"))


def test_doctor_failure_fails_install_and_never_prints_done(setup):
    r = _install(setup, FAKE_DOCTOR_RC=1)
    assert r.returncode != 0
    assert "Install complete" not in r.stdout and "Done." not in r.stdout
    assert "install did not complete" in r.stderr


def test_success_message_points_at_pi_list_when_launchers_exist(setup):
    gen = setup["home"] / ".pi" / "generated"
    gen.mkdir(parents=True)
    (gen / "pi-launchers.zsh").write_text("pi-list() { :; }\n")
    r = _install(setup)
    assert r.returncode == 0
    assert "pi-list" in r.stdout and "pi-sonnet" in r.stdout


def test_missing_pi_cli_stops_before_any_work(setup):
    env = dict(setup["env"])
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if "fakebin" not in p)
    r = subprocess.run([str(setup["root"] / "install.sh")], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "Pi CLI is not on PATH" in r.stderr
    assert not (setup["home"] / ".zshrc").exists()


def test_failing_overlay_hook_fails_install(setup, tmp_path):
    overlay = tmp_path / "overlay"
    (overlay / "setup.d").mkdir(parents=True)
    (overlay / "manifest.fragment.yaml").write_text("version: 1\nmodules: {}\n")
    _exe(overlay / "setup.d" / "10-boom.sh", "#!/bin/sh\necho 'INCOMPLETE: boom' >&2\nexit 3\n")
    r = subprocess.run([str(setup["root"] / "install.sh"), "--overlay", str(overlay)],
                       env=setup["env"], capture_output=True, text=True)
    assert r.returncode != 0
    assert "Overlay hook failed" in r.stderr
    assert "Install complete" not in r.stdout
