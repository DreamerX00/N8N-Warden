"""Terminal output and operator prompts.

The only module that talks to stdin/stdout. Keeping it isolated means the
operation layer can be exercised head-lessly by the self-test without any
prompt stubbing.
"""

from __future__ import annotations

import itertools
import os
import shutil
import sys
import threading
import time

from .errors import Fatal

# Animation and colour are terminal affordances. Piped output, --json, CI logs
# and `less` all get plain text, so nothing here can corrupt a machine reader.
_TTY = sys.stdout.isatty()
_COLOR = _TTY and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
_ANIMATE = _COLOR and not os.environ.get("N8NADM_NO_ANIMATION")

# Mutable process state, held in a dict rather than a bare module global so
# that `from .console import assume_yes` can never capture a stale copy.
_state = {"assume_yes": False}


def set_assume_yes(value: bool) -> None:
    _state["assume_yes"] = bool(value)


def assume_yes() -> bool:
    return _state["assume_yes"]


# --- colour --------------------------------------------------------------

def set_color(enabled: bool) -> None:
    """Force colour/animation off (--no-color)."""
    global _COLOR, _ANIMATE
    _COLOR = enabled and _TTY
    _ANIMATE = _COLOR and not os.environ.get("N8NADM_NO_ANIMATION")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s):   return _c("1", s)
def dim(s):    return _c("2", s)
def red(s):    return _c("31", s)
def green(s):  return _c("32", s)
def yellow(s): return _c("33", s)
def blue(s):   return _c("36", s)
def cyan(s):   return _c("96", s)
def mag(s):    return _c("95", s)


def _width(default: int = 80) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def rule(label: str = "", char: str = "─") -> str:
    """A horizontal divider, optionally titled."""
    width = min(_width(), 76)
    if not label:
        return dim("  " + char * (width - 2))
    head = f"  {char}{char} {bold(label)} "
    plain = len(label) + 5
    return head + dim(char * max(0, width - plain))


# --- lines ---------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg)


def ok(msg):   say(f"  {green('✓')} {msg}")
def warn(msg): say(f"  {yellow('⚠')} {msg}")
def err(msg):  say(f"  {red('✗')} {msg}")
def step(msg): say(f"  {blue('·')} {msg}")


# --- motion --------------------------------------------------------------

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Spinner:
    """Animated wait indicator with elapsed time.

    Used only where the tool genuinely blocks — stopping n8n, waiting for it to
    come back, taring gigabytes, vacuuming. On a non-terminal it prints one
    plain line at the start and one at the end, so logs stay readable.

        with Spinner("stopping n8n", "stopped n8n"):
            ...
    """

    def __init__(self, label: str, done: str | None = None):
        self.label = label
        self.done = done or label
        self.started = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.failed = False

    def __enter__(self) -> "Spinner":
        self.started = time.time()
        if _ANIMATE:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            say(f"  {blue('·')} {self.label}…")
        return self

    def _spin(self) -> None:
        for frame in itertools.cycle(SPINNER_FRAMES):
            if self._stop.is_set():
                return
            elapsed = time.time() - self.started
            sys.stdout.write(f"\r  {cyan(frame)} {self.label}… {dim(f'{elapsed:.1f}s')}")
            sys.stdout.flush()
            time.sleep(0.08)

    def update(self, label: str) -> None:
        """Change the message mid-wait without restarting the animation."""
        self.label = label

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            sys.stdout.write("\r" + " " * min(_width(), 100) + "\r")
            sys.stdout.flush()
        elapsed = time.time() - self.started
        if exc_type or self.failed:
            err(f"{self.label} failed after {elapsed:.1f}s")
        else:
            ok(f"{self.done} {dim(f'({elapsed:.1f}s)')}")
        return False


def track(items, label: str, width: int = 28):
    """Iterate with a progress bar. Falls back to a single line off-terminal."""
    items = list(items)
    total = len(items)
    if not _ANIMATE or total < 2:
        if items:
            say(f"  {blue('·')} {label}: {total}")
        yield from items
        return

    for index, item in enumerate(items, 1):
        filled = int(width * index / total)
        bar = green("█" * filled) + dim("░" * (width - filled))
        sys.stdout.write(f"\r  {bar} {index}/{total} {dim(label)}")
        sys.stdout.flush()
        yield item
    sys.stdout.write("\r" + " " * min(_width(), 100) + "\r")
    sys.stdout.flush()
    ok(f"{label}: {total}")


# --- tables --------------------------------------------------------------

def table(rows: list[dict], cols: list[str], headers: list[str] | None = None) -> str:
    """Render dicts as an aligned text table."""
    if not rows:
        return dim("    (none)")
    headers = headers or cols
    widths = [len(h) for h in headers]
    cells = []
    for r in rows:
        row = ["" if r.get(c) is None else str(r.get(c)) for c in cols]
        cells.append(row)
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))
    out = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    out.append("  " + dim("─" * (sum(widths) + 2 * (len(widths) - 1))))
    for row in cells:
        out.append("  " + "  ".join(v.ljust(widths[i]) for i, v in enumerate(row)).rstrip())
    return "\n".join(out)


# --- prompts -------------------------------------------------------------

def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say()
        raise Fatal("cancelled")
    return value or (default or "")


def confirm(prompt: str, default: bool = False) -> bool:
    if assume_yes():
        return True
    hint = "Y/n" if default else "y/N"
    value = ask(f"{prompt} ({hint})").lower()
    return default if not value else value.startswith("y")


def confirm_typed(expected: str, what: str) -> bool:
    """Require the operator to retype the name.

    Reserved for deletes whose blast radius extends past the thing named — a
    project delete also rewrites ownership of every workflow and credential it
    holds. `--yes` deliberately does NOT skip this: a blanket flag should not
    be able to authorise that.
    """
    warn(f"this deletes {what} and everything it owns")
    if expected != ask(f"type {expected!r} to confirm"):
        say(dim("    name did not match — nothing done"))
        return False
    return True


MAX_PROMPT_RETRIES = 3


def pick(items: list, label, prompt: str = "select", multi: bool = False,
         allow_none: bool = False):
    """Numbered picker.

    Accepts an index, or the option's text (so typing 'member' works as well
    as '1'). Multi-select accepts '1,3-5' or '*'. Bad input re-prompts rather
    than raising — a mistyped menu answer is not a reason to lose the session.
    """
    if not items:
        raise Fatal("nothing to choose from")

    # Choosing from a single option is not a choice worth making.
    if len(items) == 1 and not multi and not allow_none:
        say(f"    {dim('auto-selected')} {label(items[0])}")
        return items[0]

    for i, item in enumerate(items, 1):
        say(f"    {str(i).rjust(3)}  {label(item)}")
    if allow_none:
        say(f"    {'0'.rjust(3)}  {dim('(none)')}")

    hint = " (e.g. 1,3-5 or *)" if multi else ""
    for attempt in range(MAX_PROMPT_RETRIES):
        raw = ask(prompt + hint)
        if not raw:
            raise Fatal("cancelled")
        try:
            return (_parse_multi(raw, items) if multi
                    else _parse_single(raw, items, label, allow_none))
        except _BadChoice as e:
            if attempt == MAX_PROMPT_RETRIES - 1:
                raise Fatal(f"{e} — giving up after "
                            f"{MAX_PROMPT_RETRIES} attempts")
            err(str(e))
    raise Fatal("cancelled")


class _BadChoice(Exception):
    """Recoverable: the operator mistyped, so re-prompt."""


def _parse_single(raw: str, items: list, label, allow_none: bool):
    raw = raw.strip()
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        index = int(raw)
        if allow_none and index == 0:
            return None
        if not 1 <= index <= len(items):
            raise _BadChoice(f"choose a number between 1 and {len(items)}")
        return items[index - 1]

    # Not a number: match against what each option displays.
    lowered = raw.lower()
    rendered = [(i, str(label(item)).lower()) for i, item in enumerate(items)]
    exact = [i for i, text in rendered if text == lowered]
    partial = [i for i, text in rendered if lowered in text]
    matches = exact or partial
    if len(matches) == 1:
        return items[matches[0]]
    if not matches:
        raise _BadChoice(f"{raw!r} is not a number or a listed option")
    raise _BadChoice(f"{raw!r} matches {len(matches)} options — be more specific")


def _parse_multi(raw: str, items: list) -> list:
    raw = raw.strip()
    if raw == "*":
        return list(items)
    chosen: list[int] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        if "-" in part.lstrip("-"):
            lo, hi = part.split("-", 1)
            if not (lo.strip().isdigit() and hi.strip().isdigit()):
                raise _BadChoice(f"{part!r} is not a range like 3-5")
            chosen += list(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            chosen.append(int(part))
        else:
            raise _BadChoice(f"{part!r} is not a number, range, or '*'")
    picked = [items[i - 1] for i in chosen if 1 <= i <= len(items)]
    if not picked:
        raise _BadChoice(f"nothing in range 1-{len(items)} was selected")
    return picked
