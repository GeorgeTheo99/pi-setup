"""Console conventions shared by setup, update, status and doctor.

pi-shared commands are compact by default: one line per stage, with captured
details replayed only on failure or with --verbose. Standalone helpers keep
full detail. Limitations are collected into one "Not verified" footer instead
of being repeated inline; their wording is preserved, never dropped.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile

VERBOSE_ENV = "PI_SETUP_VERBOSE"
NOTES_ENV = "PI_SETUP_NOTES_FILE"
# kind: (symbol, ASCII fallback, SGR color)
_SYMBOLS = {"step": ("▶", ">", "1"), "ok": ("✓", "+", "32"), "warn": ("!", "!", "33"), "fail": ("✗", "x", "31")}


def verbose():
    """Full detail unless an orchestrating pi-shared command selected compact output."""
    return os.environ.get(VERBOSE_ENV) != "0"


def _unicode(stream):
    return (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "") == "utf8"


def arrow():
    return "→" if _unicode(sys.stdout) else "->"


def line(kind, message, indent=""):
    """Print one status line; color only on a terminal (NO_COLOR and TERM=dumb opt out)."""
    symbol, fallback, color = _SYMBOLS[kind]
    symbol = symbol if _unicode(sys.stdout) else fallback
    isatty = getattr(sys.stdout, "isatty", None)
    if isatty and isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM", "dumb") != "dumb":
        symbol = f"\033[{color}m{symbol}\033[0m"
    print(f"{indent}{symbol} {message}", flush=True)


def footer(notes):
    unique = list(dict.fromkeys(" ".join(note.split()) for note in notes if note.strip()))
    if unique:
        print("\nNot verified:")
        for note in unique:
            print(f"  - {note}")


def record(notes):
    """Hand limitations to the orchestrator's footer, or print them when standalone."""
    path = os.environ.get(NOTES_ENV)
    if not path:
        footer(notes)
        return
    with open(path, "a", encoding="utf-8") as stream:
        stream.writelines(" ".join(note.split()) + "\n" for note in notes)


def table(title, rows):
    """Aligned plain-text rows; the last column is left unpadded."""
    if not rows:
        return
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    print(f"\n{title}")
    for row in rows:
        print("  " + "  ".join([cell.ljust(width) for cell, width in zip(row, widths)] + [row[-1]]).rstrip())


class Session:
    """One compact/verbose command whose children append notes to a private file."""

    def __init__(self, verbose_output):
        self.verbose = bool(verbose_output)
        self.notes = []
        self.path = None

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(prefix="pi-setup-notes-")
        os.close(fd)
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(FileNotFoundError):  # e.g. a TMPDIR cleaner
            os.unlink(self.path)

    @property
    def env(self):
        return {VERBOSE_ENV: "1" if self.verbose else "0", NOTES_ENV: self.path}

    def note(self, message):
        self.notes.append(message)

    def footer(self):
        with open(self.path, encoding="utf-8") as stream:
            footer([*stream.read().splitlines(), *self.notes])
