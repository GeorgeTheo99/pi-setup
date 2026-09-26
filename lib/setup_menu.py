"""Optional keyboard menus. No third-party dependencies or installation side effects."""
import json
import os
import sys
import textwrap


def _wrap(text, width):
    # ASCII escapes preserve exact paths without wide/combining characters or
    # terminal controls corrupting the approval screen. Escape backslashes too.
    return textwrap.wrap(json.dumps(text, ensure_ascii=True)[1:-1], width, drop_whitespace=False)


def require_terminal():
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM", "dumb") == "dumb":
        raise RuntimeError("--guided needs an interactive terminal with TERM set. Use setup --help for flags and --plan to preview.")


def choose(question, choices):
    return _menu(question, choices, multiple=False)


def choose_many(question, choices, *, conflicts=()):
    return _menu(question, choices, multiple=True, conflicts=conflicts)


def confirm(plan):
    return _menu("Apply this plan?", {"no": "Cancel without changes", "yes": "Apply this plan"},
                 multiple=False, details=plan) == "yes"


def _menu(question, choices, *, multiple, conflicts=(), details=None):
    require_terminal()
    # CLI/help/plan paths do not require curses or a terminal database.
    try:
        import curses
    except ImportError:
        raise RuntimeError("Keyboard menus require Python curses. Use setup flags instead; see setup --help.") from None
    try:
        result = curses.wrapper(_screen, question, choices, multiple, conflicts, curses, details)
    except curses.error:
        raise RuntimeError("Could not display keyboard menus. Use setup flags instead; see setup --help.") from None
    labels = [choices[key] for key in result] if multiple else [choices[result]]
    print(question + " " + ("; ".join(labels) or "Keep saved selections"))
    return result


def _screen(screen, question, choices, multiple, conflicts, curses, details=None):
    keys = list(choices)
    if not keys:
        return [] if multiple else None
    selected = set()
    focus = 0
    scroll = 0
    screen.keypad(True)
    screen.clear()  # Repaint after returning from a previous menu/text prompt.
    curses.set_escdelay(100)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    while True:
        height, width = screen.getmaxyx()
        if width < 40:
            raise RuntimeError("Terminal too narrow for --guided (minimum 40 columns). Resize or use setup flags.")
        heading = _wrap(question, width - 2)
        help_lines = _wrap(
            "Up/Down: move | Space: toggle | Enter: continue | Esc: cancel" if multiple else
            "Up/Down: move | Enter: select | Esc: cancel", width - 2)
        if details is not None:
            help_lines += _wrap("PgUp/PgDn: scroll plan | Home/End: first/last page. Text uses JSON escapes.", width - 2)
        rows = []
        for key in keys:
            prefix = ("[x] " if key in selected else "[ ] ") if multiple else ""
            rows.append(_wrap(prefix + choices[key], width - 4))
        available = height - len(heading) - len(help_lines) - sum(map(len, rows)) - 3
        if available < (3 if details is not None else 0):
            raise RuntimeError("Terminal too small for --guided. Resize or use setup flags; see setup --help.")
        detail_lines = [wrapped for line in (details or "").split("\n")
                        for wrapped in (_wrap(line, width - 2) or [""])]
        page_size = available - 1
        max_scroll = max(0, len(detail_lines) - page_size) if details is not None else 0
        scroll = min(scroll, max_scroll)
        screen.erase()
        line = 0
        for text in heading:
            screen.addstr(line, 0, text)
            line += 1
        line += 1
        if details is not None:
            for text in detail_lines[scroll:scroll + page_size]:
                screen.addstr(line, 0, text)
                line += 1
            screen.addstr(line, 0, f"Plan lines {scroll + 1}-{min(scroll + page_size, len(detail_lines))}/{len(detail_lines)}")
            line += 1
        for index, wrapped in enumerate(rows):
            for part, text in enumerate(wrapped):
                marker = "> " if index == focus and part == 0 else "  "
                screen.addstr(line, 0, marker + text,
                              curses.A_REVERSE if index == focus else curses.A_NORMAL)
                line += 1
        for text in help_lines:
            line += 1
            screen.addstr(line, 0, text)
        screen.refresh()
        key = screen.getch()
        if key in (27, 3, 4):  # Escape, Ctrl-C, Ctrl-D
            raise KeyboardInterrupt
        if details is not None and key in (curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            if key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = max_scroll
            else:
                scroll = max(0, min(max_scroll, scroll + (-page_size if key == curses.KEY_PPAGE else page_size)))
        elif key == curses.KEY_UP:
            focus = max(0, focus - 1)
        elif key == curses.KEY_DOWN:
            focus = min(len(keys) - 1, focus + 1)
        elif key in (10, 13, curses.KEY_ENTER):
            return [key for key in keys if key in selected] if multiple else keys[focus]
        elif multiple and key == ord(" "):
            value = keys[focus]
            if value in selected:
                selected.remove(value)
            else:
                # Selecting one topology clears incompatible pending additions.
                for pair in conflicts:
                    if value in pair:
                        selected.difference_update(set(pair) - {value})
                selected.add(value)
