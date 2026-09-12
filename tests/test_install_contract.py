"""Installer contract tests: managed ~/.zshrc block and truthful exit status.

install.sh is exercised with a temporary HOME, local Git repositories, a stub
`pi` on PATH, and a stub doctor. Git transport is file-only; no network is used.
"""

from __future__ import annotations

import json
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
    """A copy of pi-setup with a real local module and a controllable doctor."""
    root = tmp_path / "pi-setup"
    (root / "bin").mkdir(parents=True)
    shutil.copy(SETUP_ROOT / "install.sh", root / "install.sh")
    _exe(root / "bin" / "doctor", '#!/bin/sh\nprintf "%s\\n" "$*" > "$HOME/doctor-args"\nexit "${FAKE_DOCTOR_RC:-0}"\n')
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _exe(fakebin / "pi", "#!/bin/sh\necho 0.0.0\n")
    for name in ("bash", "git", "python3", "dirname", "basename", "mkdir", "grep",
                 "date", "cp", "chmod", "tail", "od", "tr"):
        (fakebin / name).symlink_to(shutil.which(name))
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": str(fakebin),
        "PI_SETUP_CODE_ROOT": str(tmp_path / "code"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull, "GIT_ALLOW_PROTOCOL": "file",
        "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Installer Test",
        "GIT_AUTHOR_EMAIL": "installer@example.test", "GIT_COMMITTER_NAME": "Installer Test",
        "GIT_COMMITTER_EMAIL": "installer@example.test",
    }
    fx = {"root": root, "home": home, "env": env}
    origin = tmp_path / "shared-origin"
    origin.mkdir()
    _exe(origin / "install.sh", "#!/bin/sh\nexit 0\n")
    (origin / "bin").mkdir()
    _exe(origin / "bin/pi-launchers-refresh", '#!/bin/sh\necho refreshed > "$HOME/refreshed"\n')
    _git(fx, origin, "init", "-q", "-b", "main")
    _git(fx, origin, "add", ".")
    _git(fx, origin, "commit", "-qm", "fixture")
    (root / "manifest.yaml").write_text(
        f"modules:\n  pi-shared:\n    repo: {origin}\n    ref: main\n    tier: required\n")
    return fx


def _git(fx, repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], env=fx["env"], text=True).strip()


def _install(fx, *args, **extra) -> subprocess.CompletedProcess:
    env = {**fx["env"], **{k: str(v) for k, v in extra.items()}}
    return subprocess.run([str(fx["root"] / "install.sh"), *map(str, args)], env=env, capture_output=True, text=True, timeout=30)


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
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


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
    assert "pi-list" in r.stdout and "pi-sonnet" not in r.stdout
    assert "after a gateway alias catalog is configured" in r.stdout


def test_missing_pi_cli_stops_before_any_work(setup):
    env = dict(setup["env"])
    (Path(env["PATH"]) / "pi").unlink()
    r = subprocess.run([str(setup["root"] / "install.sh")], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "Pi CLI is not on PATH" in r.stderr
    assert not (setup["home"] / ".zshrc").exists()


def test_failing_overlay_hook_fails_install(setup, tmp_path):
    overlay = tmp_path / "overlay"
    (overlay / "setup.d").mkdir(parents=True)
    _exe(overlay / "setup.d" / "10-boom.sh", "#!/bin/sh\necho 'INCOMPLETE: boom' >&2\nexit 3\n")
    r = subprocess.run([str(setup["root"] / "install.sh"), "--overlay", str(overlay)],
                       env=setup["env"], capture_output=True, text=True)
    assert r.returncode != 0
    assert "Overlay hook failed" in r.stderr
    assert "Install complete" not in r.stdout


def _browser_module(fx):
    worker = Path(fx["env"]["PI_SETUP_CODE_ROOT"]) / "browser-worker"
    (worker / "scripts").mkdir(parents=True)
    _exe(worker / "install.sh", '''#!/bin/sh
[ "${BROWSER_INSTALL_FAIL:-0}" != 1 ] || exit 7
mkdir -p "$HOME/srv/browser-worker/shared/tokens"
printf 'SYNTHETIC_TOKEN' > "$HOME/srv/browser-worker/shared/tokens/pi-production"
chmod 600 "$HOME/srv/browser-worker/shared/tokens/pi-production"
echo installed > "$HOME/browser-installed"
''')
    _exe(worker / "scripts/browser-worker", '''#!/bin/sh
case "$1" in
 verify) exit 0 ;;
 env) printf 'PORT=8890\\nDATA_DIR=%s/srv/browser-worker/shared\\n' "$HOME" ;;
 *) exit 9 ;;
esac
''')
    (fx["root"] / "lib").mkdir()
    shutil.copy(SETUP_ROOT / "lib/configure_browser_worker.py", fx["root"] / "lib/configure_browser_worker.py")
    _git(fx, worker, "init", "-q", "-b", "main")
    _git(fx, worker, "add", ".")
    _git(fx, worker, "commit", "-qm", "worker fixture")
    origin = fx["root"].parent / "worker-origin.git"
    _git(fx, worker, "clone", "-q", "--bare", str(worker), str(origin))
    _git(fx, worker, "remote", "add", "origin", str(origin))
    _git(fx, worker, "fetch", "-q", "origin")
    manifest = fx["root"] / "manifest.yaml"
    manifest.write_text(manifest.read_text() +
                        f"  browser-worker:\n    repo: {origin}\n    ref: main\n    tier: recommended\n")
    return worker


@pytest.mark.parametrize("flags", [[], ["--minimal", "--with-browser-worker"]])
def test_browser_selection_happens_after_overlay_and_preserves_databricks_web(setup, tmp_path, flags):
    _browser_module(setup)
    overlay = tmp_path / "overlay"
    (overlay / "setup.d").mkdir(parents=True)
    _exe(overlay / "setup.d/60-web.sh", '''#!/bin/sh
mkdir -p "$HOME/.pi/research"
printf '{"websearchMcpUrl":"http://127.0.0.1:8891/mcp","browserWorkerEnabled":false}' > "$HOME/.pi/research/config.json"
''')
    result = _install(setup, *flags, "--overlay", overlay)
    assert result.returncode == 0, result.stdout + result.stderr
    config = json.loads((setup["home"] / ".pi/research/config.json").read_text())
    assert config["browserWorkerEnabled"] is True
    assert config["browserWorkerMcpUrl"] == "http://127.0.0.1:8890/mcp"
    assert config["websearchMcpUrl"] == "http://127.0.0.1:8891/mcp"
    assert "mcpUrl" not in config


def test_minimal_does_not_install_or_select_browser_worker(setup):
    _browser_module(setup)
    result = _install(setup, "--minimal")
    assert result.returncode == 0, result.stderr
    assert not (setup["home"] / "browser-installed").exists()
    assert not (setup["home"] / ".pi/research/config.json").exists()


def test_worker_install_failure_never_selects_client_or_reports_success(setup):
    _browser_module(setup)
    result = _install(setup, BROWSER_INSTALL_FAIL="1")
    assert result.returncode != 0
    assert "browser-worker installer failed" in result.stderr
    assert "Install complete" not in result.stdout
    assert not (setup["home"] / ".pi/research/config.json").exists()


def test_existing_checkout_requires_explicit_update_to_reconcile_exact_pin(setup):
    worker = _browser_module(setup)
    def git(*args):
        return _git(setup, worker, *args)
    old = git("rev-parse", "HEAD")
    (worker / "version").write_text("new")
    git("add", ".")
    git("commit", "-qm", "new")
    pin = git("rev-parse", "HEAD")
    git("checkout", "-q", "--detach", old)
    manifest = setup["root"] / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace(
        f"repo: {git('remote', 'get-url', 'origin')}\n    ref: main",
        f"repo: {git('remote', 'get-url', 'origin')}\n    ref: {pin}"))
    result = _install(setup)
    assert result.returncode != 0
    assert "does not match" in result.stderr and "--update" in result.stderr
    assert git("rev-parse", "HEAD") == old
    assert not (setup["home"] / "browser-installed").exists()
    assert not (setup["home"] / ".pi/research/config.json").exists()
    result = _install(setup, "--update")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git("rev-parse", "HEAD") == pin
    assert git("branch", "--show-current") == ""
    # A matching exact pin also works on a normal no-fetch rerun.
    assert _install(setup).returncode == 0


def test_browser_doctor_selection_tracks_only_successfully_installed_modules(setup):
    _browser_module(setup)
    assert _install(setup, "--minimal").returncode == 0
    assert (setup["home"] / "doctor-args").read_text().strip() == "--module pi-shared"
    assert _install(setup, "--minimal", "--with-browser-worker").returncode == 0
    assert (setup["home"] / "doctor-args").read_text().strip() == "--module pi-shared --module browser-worker"


def test_browser_checkout_local_work_is_protected_before_client_configuration(setup):
    worker = _browser_module(setup)
    private = worker / "untracked-local-work"
    private.write_text("preserve me")
    result = _install(setup, "--update")
    assert result.returncode != 0 and "local changes" in result.stderr
    assert private.read_text() == "preserve me"
    assert not (setup["home"] / "browser-installed").exists()
    assert not (setup["home"] / ".pi/research/config.json").exists()


def test_unchanged_service_is_verified_without_reinstall_or_restart(setup):
    worker = _browser_module(setup)
    assert _install(setup).returncode == 0
    (setup["home"] / "browser-installed").unlink()
    revision = _git(setup, worker, "rev-parse", "HEAD")
    result = _install(setup, "--update-changed", "--only", "pi-shared,browser-worker",
                      PI_SETUP_INSTALLED_REVISIONS=json.dumps({"browser-worker": revision}))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not restarting" in result.stdout
    assert not (setup["home"] / "browser-installed").exists()
    assert (setup["home"] / "refreshed").exists()
    assert "--module browser-worker" in (setup["home"] / "doctor-args").read_text()


def test_exclusive_updates_do_not_select_recommended_modules(setup):
    _browser_module(setup)
    result = _install(setup, "--update-changed", "--only", "pi-shared")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (setup["home"] / "browser-installed").exists()
    assert (setup["home"] / "doctor-args").read_text().strip() == "--module pi-shared"


def test_failed_apply_is_retried_even_when_checkout_already_advanced(setup):
    worker = _browser_module(setup)
    old = _git(setup, worker, "rev-parse", "HEAD")
    (worker / "new-code").write_text("changed")
    _git(setup, worker, "add", ".")
    _git(setup, worker, "commit", "-qm", "next")
    _git(setup, worker, "push", "-q", "origin", "main")
    flags = ["--update-changed", "--only", "pi-shared,browser-worker"]
    receipt = json.dumps({"browser-worker": old})
    assert _install(setup, *flags, PI_SETUP_INSTALLED_REVISIONS=receipt, BROWSER_INSTALL_FAIL="1").returncode != 0
    result = _install(setup, *flags, PI_SETUP_INSTALLED_REVISIONS=receipt)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (setup["home"] / "browser-installed").exists()


def test_bad_exclusive_selection_fails_before_module_installation(setup):
    result = _install(setup, "--update-changed", "--only", "pi-shared,typo")
    assert result.returncode != 0
    assert not (setup["home"] / "doctor-args").exists()


def test_shell_override_and_commented_source_do_not_execute_rc(setup):
    rc = setup["home"] / "custom rc.zsh"
    original = '# source "$HOME/.pi/generated/pi-launchers.zsh"\nexit 91\n'
    rc.write_text(original)
    result = _install(setup, PI_SETUP_ZSHRC=rc)
    assert result.returncode == 0, result.stdout + result.stderr
    assert rc.read_text() == original + RC_BLOCK
    assert not (setup["home"] / ".zshrc").exists()


@pytest.mark.parametrize("text", ["modules: {}\n", "modules:\n", "not a manifest\n"])
def test_empty_or_malformed_manifest_never_reports_installation_success(setup, text):
    (setup["root"] / "manifest.yaml").write_text(text)
    result = _install(setup)
    assert result.returncode != 0 and "Invalid module manifest" in result.stderr
    assert not (setup["home"] / "doctor-args").exists()
    assert not (setup["home"] / ".zshrc").exists()
