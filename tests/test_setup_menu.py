"""Keyboard state-machine and real PTY regression tests; no real setup applies."""
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_cli import cli, isolated, options


class Curses:
    KEY_UP, KEY_DOWN, KEY_ENTER, KEY_RESIZE = 259, 258, 343, 410
    KEY_PPAGE, KEY_NPAGE, KEY_HOME, KEY_END = 339, 338, 262, 360
    A_REVERSE, A_NORMAL = 1, 0
    error = RuntimeError

    @staticmethod
    def set_escdelay(value):
        pass

    @staticmethod
    def curs_set(value):
        pass


class Screen:
    def __init__(self, keys, size=(40, 120)):
        self.keys = iter(keys)
        self.size = size
        self.frames = []

    def getmaxyx(self):
        return self.size

    def keypad(self, value):
        assert value

    def clear(self):
        pass

    def erase(self):
        self.frames.append([])

    def addstr(self, row, column, text, *args):
        assert row < self.size[0] and column + len(text) < self.size[1]
        self.frames[-1].append(text)

    def refresh(self):
        pass

    def getch(self):
        return next(self.keys)


def test_arrow_selection_ignores_typed_option_names():
    screen = Screen([ord("b"), Curses.KEY_DOWN, 10])
    assert cli.setup_menu._screen(screen, "Select", {"a": "One", "b": "Two"}, False, (), Curses) == "b"


def test_checklist_toggles_and_finishes_once():
    screen = Screen([32, 32, Curses.KEY_DOWN, 32, 10])
    assert cli.setup_menu._screen(screen, "Select", {"a": "One", "b": "Two"}, True, (), Curses) == ["b"]


def test_conflicting_additions_are_cleared():
    screen = Screen([32, Curses.KEY_DOWN, 32, Curses.KEY_DOWN, 32, 10])
    choices = {"model-gateway": "Local", "omlx": "oMLX", "existing-gateway": "Remote"}
    conflicts = (("existing-gateway", "model-gateway"), ("existing-gateway", "omlx"))
    assert cli.setup_menu._screen(screen, "Select", choices, True, conflicts, Curses) == ["existing-gateway"]


@pytest.mark.parametrize("key", [27, 3, 4])
def test_keyboard_cancel(key):
    with pytest.raises(KeyboardInterrupt):
        cli.setup_menu._screen(Screen([key]), "Select", {"a": "One"}, False, (), Curses)


@pytest.mark.parametrize("size", [(5, 80), (40, 30)])
def test_small_terminal_fails_with_cli_alternative(size):
    with pytest.raises(RuntimeError, match="setup flags"):
        cli.setup_menu._screen(Screen([], size), "Select", {"a": "One"}, False, (), Curses)


def test_default_cli_prompts_only_for_approval(isolated, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    monkeypatch.setattr(cli, "choose", lambda *args: pytest.fail("CLI must not open menus"))
    assert cli.setup(options(mode=None, yes=False, recovery=None, without_browser=None, with_omnigent=None)) == 0
    assert prompts == ["Apply this plan? [y/N] "]
    assert not isolated[1] and not list(isolated[0].iterdir())


def test_cli_rerun_never_repeats_component_questions(isolated, monkeypatch):
    cli.setup(options())
    before = cli.state_path().read_bytes()
    isolated[1].clear()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    monkeypatch.setattr(cli.setup_menu, "choose_many", lambda *a, **k: pytest.fail("no checklist"))
    monkeypatch.setattr(cli, "choose", lambda *a: pytest.fail("no menu"))
    assert cli.setup(options(mode=None, yes=False, without_browser=None, with_omnigent=None, recovery=None)) == 0
    assert cli.state_path().read_bytes() == before and not isolated[1]


@pytest.mark.parametrize("extra,error", [
    ({"mode": "existing-gateway"}, "--gateway-url and --gateway-key-file"),
    ({"mode": "local", "omlx": "existing"}, "--omlx-url"),
    ({"search": "existing"}, "--search-url"),
    ({"search": "local"}, "--brave-key-file"),
])
def test_missing_cli_inputs_fail_without_prompts(isolated, monkeypatch, extra, error):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must fail, not prompt"))
    with pytest.raises(RuntimeError, match=error):
        cli.setup(options(yes=False, **extra))
    assert not isolated[1] and not list(isolated[0].iterdir())


def test_guided_requires_terminal_before_any_collection(isolated):
    with pytest.raises(RuntimeError, match="interactive terminal"):
        cli.setup(options(guided=True, yes=False))
    assert not isolated[1] and not list(isolated[0].iterdir())


def test_guided_yes_is_rejected(isolated):
    with pytest.raises(RuntimeError, match="cannot be combined"):
        cli.setup(options(guided=True))
    assert not isolated[1] and not list(isolated[0].iterdir())


def test_guided_plan_is_read_only_without_terminal(isolated, monkeypatch):
    monkeypatch.setattr(cli.setup_menu, "require_terminal", lambda: pytest.fail("plan has no terminal requirement"))
    assert cli.setup(options(guided=True, yes=False, plan=True)) == 0
    assert not isolated[1] and not list(isolated[0].iterdir())


@pytest.mark.parametrize("saved,expected", [
    ({"mode": "direct"}, {"model-gateway", "existing-gateway", "omlx"}),
    ({"mode": "existing-gateway"}, {"direct"}),
    ({"mode": "local", "modules": ["pi-shared", "model-gateway"]}, {"direct"}),
])
def test_saved_checklist_excludes_existing_and_incompatible_access(isolated, monkeypatch, saved, expected):
    seen = []
    def choose_many(question, choices, **kwargs):
        seen.append(set(choices))
        return []
    monkeypatch.setattr(cli.setup_menu, "choose_many", choose_many)
    cli.collect_components(options(mode=None), {"modules": ["pi-shared"], **saved}, True)
    assert seen == [expected]


def test_unicode_and_control_text_wraps_as_lossless_ascii():
    import json
    value = "/tmp/" + "界" * 60 + "/key\\u754c\x1b"
    rows = cli.setup_menu._wrap(value, 78)
    assert all(row.isascii() and len(row) <= 78 for row in rows)
    assert json.loads('"' + "".join(rows) + '"') == value


def test_wrap_preserves_spaces_at_line_boundaries():
    import json
    value = "/tmp/" + "x" * 72 + " tail"
    rows = cli.setup_menu._wrap(value, 78)
    assert json.loads('"' + "".join(rows) + '"') == value


def test_plan_escapes_unicode_separators_instead_of_consuming_them():
    screen = Screen([10], (24, 80))
    cli.setup_menu._screen(screen, "Apply?", {"no": "Cancel", "yes": "Apply"},
                           False, (), Curses, "/tmp/name\u2028other\rfile")
    assert "/tmp/name\\u2028other\\rfile" in screen.frames[0]


def test_confirmation_plan_scroll_keeps_cancel_selected():
    screen = Screen([Curses.KEY_END, Curses.KEY_HOME, Curses.KEY_NPAGE, Curses.KEY_PPAGE, 10], (24, 80))
    details = "\n".join(f"Detail {i}" for i in range(50))
    assert cli.setup_menu._screen(screen, "Apply?", {"no": "Cancel", "yes": "Apply"},
                                  False, (), Curses, details) == "no"
    assert "Detail 0" in screen.frames[0]
    assert "Detail 49" in screen.frames[1]
    assert "Detail 0" in screen.frames[2]


def test_guided_approval_includes_full_plan_and_warnings(isolated, monkeypatch):
    from test_questionnaire import answers
    answers(monkeypatch, ["none"])
    monkeypatch.setenv("SEARCH_MCP_URL", "https://override.example/mcp")
    plans = []
    monkeypatch.setattr(cli.setup_menu, "confirm", lambda plan: plans.append(plan) or False)
    assert cli.setup(options(guided=True, yes=False, search="existing", search_url="https://search.example/mcp",
                             search_key_file=None)) == 0
    assert len(plans) == 1
    assert "https://search.example/mcp" in plans[0] and "SEARCH_MCP_URL" in plans[0]
    assert "override.example" not in plans[0]
    assert not isolated[1] and not list(isolated[0].iterdir())


def run_pty(tmp_path, arguments, actions, *, home_name="home"):
    """Exercise real argparse/curses; replace only platform prerequisites and apply."""
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    home = tmp_path / home_name
    home.mkdir()
    script = """
import fcntl, os, sys, termios
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
original = termios.tcgetattr(0)
sys.path.insert(0, sys.argv.pop(1))
from test_cli import cli
cli.platform.system = lambda: 'Darwin'
cli.shutil.which = lambda name: '/fake/' + name
def forbidden(*args, **kwargs):
    raise AssertionError('PTY tests must never apply setup')
cli.apply_setup = forbidden
result = cli.main()
restored = termios.tcgetattr(0)
# macOS sets PENDIN after cooked-mode restoration; this is kernel input
# bookkeeping, not a change to the user's echo/canonical/signal modes.
for settings in (original, restored):
    settings[3] &= ~getattr(termios, 'PENDIN', 0)
assert restored == original, ('terminal settings not restored', original, restored)
sys.exit(result)
"""
    process = subprocess.Popen([sys.executable, "-B", "-c", script, str(Path(__file__).parent),
                                "setup", *arguments], stdin=slave, stdout=slave, stderr=slave,
                               env={"HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm-256color",
                                    "PYTHONDONTWRITEBYTECODE": "1"})
    output = b""
    try:
        for expected, key in actions:
            pending = b""
            deadline = time.monotonic() + 10
            while expected.encode() not in pending:
                assert time.monotonic() < deadline, (expected, output.decode(errors="replace"))
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    data = os.read(master, 65536)
                    pending += data
                    output += data
                elif process.poll() is not None:
                    pytest.fail(f"Exited before {expected}: {output!r}")
            os.write(master, key)
        deadline = time.monotonic() + 10
        # Drain while waiting: PTY buffers can fill during curses teardown.
        while process.poll() is None:
            assert time.monotonic() < deadline, output.decode(errors="replace")
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        code = process.wait()
        while select.select([master], [], [], 0)[0]:
            data = os.read(master, 65536)
            if not data:  # macOS reports PTY EOF as readable.
                break
            output += data
        assert list(home.iterdir()) == []
        return code, output.decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        os.close(master)
        os.close(slave)


def test_real_guided_flow_and_decline_restore_terminal(tmp_path):
    down = b"\x1bOB"  # xterm application-mode Down (keypad enabled by curses)
    code, output = run_pty(tmp_path, ["--guided"], [
        ("Model connection", down * 5 + b"\r"),
        ("Public browser automation", down + b"\r"),
        ("Optional Omnigent", b"\r"),
        ("Optional independent recovery", b"\r"),
        ("Web search and page retrieval", down * 2 + b"\r"),
        ("Cancel without changes", b"\x1b[6~\x1bOH\r"),
    ])
    assert code == 0 and "Cancelled; no installation changes made." in output, output
    assert "Choice (or cancel)" not in output


@pytest.mark.parametrize("key", [b"\x1b", b"\x03", b"\x04"])
def test_real_guided_cancel_restores_terminal(tmp_path, key):
    code, output = run_pty(tmp_path, ["--guided"], [("Model connection", key)])
    assert code == 130 and "Cancelled" in output, output


def test_real_unicode_plan_uses_escaped_cells_without_overwrite(tmp_path):
    code, output = run_pty(tmp_path, ["--guided", "--mode", "later", "--without-browser",
                                   "--without-omnigent", "--recovery", "skip", "--search", "skip"],
                           [("Cancel without changes", b"\r")], home_name="界" * 60)
    assert code == 0, output
    # Inspect the actual curses output, not the plain plan printed before it.
    screen = output.split("\x1b[?1049h", 1)[1].split("\x1b[?1049l", 1)[0]
    assert "界" not in screen and "\\u754c" in screen
    assert "Text uses JSON escapes" in screen


def test_real_cli_default_only_requests_confirmation(tmp_path):
    code, output = run_pty(tmp_path, [], [("Apply this plan? [y/N]", b"n\r")])
    assert code == 0 and "Model connection:" not in output
    assert "Modules: pi-shared, browser-worker" in output
