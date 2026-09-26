"""Help is a read-only entry point; its setup recipes must remain usable."""
import os
import shlex
import subprocess
import sys

import pytest

from test_cli import ROOT


# Audit the CLI child itself: an unchanged HOME alone would miss credential reads
# or network calls. These fresh-home/help cases must not open any user files.
READ_ONLY_LAUNCHER = """
import os, runpy, sys
home = os.path.abspath(os.environ["HOME"]) + os.sep

def guard(event, args):
    if event.startswith("socket.") or event in (
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"
    ):
        raise AssertionError("Help/plan attempted network or command execution")
    if event == "open" and isinstance(args[0], (str, bytes)):
        if os.path.abspath(os.fsdecode(args[0])).startswith(home):
            raise AssertionError("Help/plan attempted to open a user file")

sys.addaudithook(guard)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def run_cli(home, *args):
    return subprocess.run(
        [sys.executable, "-B", "-c", READ_ONLY_LAUNCHER,
         str(ROOT / "bin/pi-shared"), *args],
        env={"HOME": str(home), "PATH": os.environ["PATH"],
             "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": "80"},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("command", [[], ["setup"], ["update"], ["status"], ["uninstall"]])
def test_short_and_long_help_match_and_do_not_write(tmp_path, command):
    short = run_cli(tmp_path, *command, "-h")
    long = run_cli(tmp_path, *command, "--help")
    assert short.returncode == long.returncode == 0
    assert short.stdout == long.stdout
    assert short.stderr == long.stderr == ""
    assert all(len(line) <= 80 for line in short.stdout.splitlines())
    assert list(tmp_path.iterdir()) == []


def test_help_does_not_require_valid_saved_setup(tmp_path):
    receipt = tmp_path / ".config/pi-shared/setup.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("invalid-receipt-fixture")
    before = sorted(tmp_path.rglob("*"))
    for args in (("-h",), ("setup", "-h")):
        result = run_cli(tmp_path, *args)
        assert result.returncode == 0, result.stderr
        assert "invalid-receipt-fixture" not in result.stdout + result.stderr
    assert sorted(tmp_path.rglob("*")) == before
    assert receipt.read_text() == "invalid-receipt-fixture"


def test_top_level_help_exposes_capabilities_and_next_steps(tmp_path):
    result = run_cli(tmp_path, "-h")
    assert result.returncode == 0
    for text in (
        "Native providers", "Remote gateway", "Local gateway", "Local models (oMLX)",
        "Browser automation", "Web search", "Omnigent checks", "Recovery guidance",
        "pi-shared setup --plan", "pi-shared setup --guided", "pi-shared setup -h",
        "pi-shared status", "pi-shared update --plan", "pi-shared uninstall --plan",
        "Help never runs setup", "Provider login", "pi models",
    ):
        assert text in result.stdout


def test_setup_help_groups_all_options_and_explains_safety(tmp_path):
    result = run_cli(tmp_path, "setup", "-h")
    assert result.returncode == 0
    # Check the option definitions, not the usage line or example mentions.
    groups = {
        "Preview and approval": ["--guided", "--plan", "--yes", "--update"],
        "Model access": ["--with", "--mode", "--local"],
        "Remote gateway": ["--gateway-url", "--gateway-key-file", "--allow-private-http"],
        "Local models (oMLX)": ["--omlx", "--omlx-url"],
        "Browser automation": ["--without-browser", "--with-browser"],
        "Web search": ["--search", "--with-search", "--search-url", "--search-key-file",
                       "--brave-key-file", "--search-port"],
        "Optional checks and guidance": ["--with-omnigent", "--without-omnigent", "--recovery"],
    }
    for title, flags in groups.items():
        section = result.stdout.split(f"\n{title}:\n", 1)[1].split("\n\n", 1)[0]
        actual = [line.strip().split()[0] for line in section.splitlines()
                  if line.startswith("  --")]
        assert actual == flags
    for text in (
        "--plan never reads keys", "existing private 0600 files, never literal secrets",
        "key sent without TLS", "--search skip does not stop existing search services",
        "Setup never downloads LLM weights or tests inference", "not installers",
        "Replace --plan with --yes after review", "not a migration or removal command",
        "Search: bearer authentication requires HTTPS or loopback HTTP", "Remote gateway:",
    ):
        assert text in result.stdout


@pytest.mark.parametrize("example", range(12))
def test_every_setup_recipe_plans_from_an_empty_home(tmp_path, example):
    help_result = run_cli(tmp_path, "setup", "-h")
    assert help_result.returncode == 0
    examples = help_result.stdout.split("Examples (", 1)[1].split("Apply and verify:", 1)[0]
    # Follow shell line continuations and expand only the documented HOME variable.
    recipes = [line.strip() for line in examples.replace("\\\n", "").splitlines()
               if line.strip().startswith("pi-shared setup ")]
    assert len(recipes) == 12
    argv = shlex.split(recipes[example].replace("$HOME", str(tmp_path)))
    assert argv[0] == "pi-shared" and "--plan" in argv
    result = run_cli(tmp_path, *argv[1:])
    assert result.returncode == 0, result.stderr
    assert "Apply this plan?" not in result.stdout
    assert list(tmp_path.iterdir()) == []
