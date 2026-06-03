"""Tiny CLI spinner for long-running operations (BigQuery, Anthropic API).

Usage:
    with spinner("running peak SQL"):
        df, stats = bq.execute(sql)

Threads a single rotating-glyph line on stdout. On context exit the line is
cleared so subsequent prints land on a fresh line. No-op if stdout isn't a TTY
(so log piping stays clean).
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_INTERVAL_S = 0.08

# ANSI color codes for spinner. Pick a color per phase so the user can tell
# at a glance which long-running thing is happening (peak / agent / judge).
COLORS = {
    "default": "\033[36m",  # cyan
    "peak":    "\033[36m",  # cyan
    "agent":   "\033[34m",  # blue
    "judge":   "\033[35m",  # magenta — distinct from BQ phases
}
_RESET = "\033[0m"


@contextmanager
def spinner(text: str = "", *, color: str = "default") -> Iterator[None]:
    if not sys.stdout.isatty():
        yield
        return

    stop = threading.Event()
    col = COLORS.get(color, COLORS["default"])

    def run() -> None:
        i = 0
        while not stop.is_set():
            frame = _FRAMES[i % len(_FRAMES)]
            # Truncate to terminal width so the line never wraps — wrapped
            # spinner lines accumulate stale frames instead of redrawing.
            cols = shutil.get_terminal_size((80, 20)).columns
            max_text = max(10, cols - 3)  # frame + space + safety margin
            display = text if len(text) <= max_text else text[: max_text - 1] + "…"
            sys.stdout.write(f"\r\033[2K{col}{frame}{_RESET} {display}")
            sys.stdout.flush()
            time.sleep(_INTERVAL_S)
            i += 1

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
