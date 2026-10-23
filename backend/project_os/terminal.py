"""Visible agent terminals: one tmux session per team run, shown in a desktop terminal.

tmux keeps the agents alive independently of any window: closing the
terminal does not stop them, and the session can be reattached from any
shell with `tmux attach -t <session>`. It is also how messages reach a
running agent: they are typed into its pane as if the user wrote them.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path

TMUX_TIMEOUT_S = 10


class TerminalError(RuntimeError):
    pass


def _tmux(*args: str) -> str:
    try:
        proc = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise TerminalError("tmux is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise TerminalError(f"tmux {args[0]} timed out") from exc
    if proc.returncode != 0:
        raise TerminalError(f"tmux {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def open_session(name: str, panes: list[tuple[str, Path, Path]]) -> list[str]:
    """Start `name` with one pane per (title, cwd, script), side by side.

    Returns the tmux pane ids, in the order given.
    """
    ids = []
    for i, (title, cwd, script) in enumerate(panes):
        if i == 0:
            pane = _tmux("new-session", "-d", "-s", name, "-x", "240", "-y", "60", "-c", str(cwd),
                         "-P", "-F", "#{pane_id}", str(script))
        else:
            pane = _tmux("split-window", "-h", "-t", name, "-c", str(cwd), "-P", "-F", "#{pane_id}", str(script))
        _tmux("select-pane", "-t", pane, "-T", title)
        ids.append(pane)
    for option, value in (
        ("pane-border-status", "top"),
        ("pane-border-format", " #{pane_title} "),
        # Keep a pane readable after its agent exits, instead of closing it.
        ("remain-on-exit", "on"),
        ("mouse", "on"),
    ):
        _tmux("set-option", "-t", name, option, value)
    _tmux("select-layout", "-t", name, "even-horizontal")
    _tmux("select-pane", "-t", ids[0])
    return ids


def session_exists(name: str) -> bool:
    try:
        _tmux("has-session", "-t", name)
    except TerminalError:
        return False
    return True


def kill_session(name: str) -> None:
    if session_exists(name):
        _tmux("kill-session", "-t", name)


def attach_command(name: str) -> str:
    return f"tmux attach -t {shlex.quote(name)}"


def open_window(name: str, terminal: str | None = None) -> bool:
    """Show the session in a new desktop terminal window. False when there is none to use."""
    if terminal:
        prefix = shlex.split(terminal)
    elif shutil.which("x-terminal-emulator"):
        prefix = ["x-terminal-emulator", "-e"]
    else:
        return False
    subprocess.Popen(
        [*prefix, "tmux", "attach", "-t", name],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True


def capture(pane: str, lines: int = 12) -> list[str]:
    """The last non-empty lines visible in a pane."""
    screen = _tmux("capture-pane", "-p", "-t", pane).splitlines()
    return [line for line in screen if line.strip()][-lines:]


def press_enter(pane: str) -> None:
    _tmux("send-keys", "-t", pane, "Enter")


def send(pane: str, text: str) -> None:
    """Type `text` into a pane as one line and submit it."""
    line = " ".join(text.split())
    _tmux("send-keys", "-t", pane, "-l", line)
    # Sent separately, and a moment later, so a TUI that detects pastes still
    # sees the Enter as a keypress rather than as a newline in the pasted text.
    time.sleep(0.3)
    _tmux("send-keys", "-t", pane, "Enter")
