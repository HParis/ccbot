"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured iTerm2 pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt,
    RestoreCheckpoint) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output().
"""

import re
from dataclasses import dataclass


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Computer Use screen-control consent dialog. App list uses ◉/○
        # toggles (Space), confirm via a numbered ❯ menu ("Allow…"/"Deny…").
        # Bottom line is "Enter to confirm · Esc to cancel" — note "Esc to
        # cancel" sits mid-line, so the anchored PermissionPrompt bottom
        # never catches it.
        name="ComputerUse",
        top=(re.compile(r"^\s*Computer Use wants to control"),),
        bottom=(
            re.compile(r"Enter to confirm"),
            re.compile(r"Esc to cancel"),
        ),
    ),
    UIPattern(
        # /mcp "Manage MCP servers" modal — same ❯-list + footer family as
        # the Computer Use dialog (footer: "↑/↓ to navigate · Enter to
        # confirm · Esc to cancel", both markers mid-line).
        name="ManageMCP",
        top=(re.compile(r"^\s*Manage MCP servers"),),
        bottom=(
            re.compile(r"Enter to confirm"),
            re.compile(r"Esc to cancel"),
        ),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


# ── Post-processing ──────────────────────────────────────────────────────


def _normalize_pane(text: str) -> str:
    """Replace NUL bytes with spaces in captured pane text.

    iTerm2 renders unset/blank cells as NUL (``\\x00``), and these land
    *inside* lines — e.g. ``' \\x00Computer Use wants to control…'`` or
    ``'\\x00\\x00Enter to confirm'``.  NUL is not whitespace, so any
    line-anchored ``^\\s*`` pattern (and the status-line spinner check)
    silently fails to match.  Mapping NUL → space restores the visual
    blank the cell represents and makes matching robust.
    """
    return text.replace("\x00", " ")


_RE_LONG_DASH = re.compile(r"^[─▔━]{5,}$")

# Full-width bar characters Claude Code uses to frame modal screens.
_BAR_CHARS = frozenset("─▔━╌┄")

# Footer "key hint" line at the bottom of a Claude Code modal, e.g.
# "↑/↓ to navigate · Enter to select · Esc to back". Wording varies per
# screen (confirm/select/continue/save, cancel/back/exit/close), so match
# the action verbs rather than a fixed phrase.
_RE_MODAL_FOOTER = re.compile(
    r"(↑/↓ to navigate|Enter to (confirm|select|continue|save)"
    r"|Esc to (cancel|back|exit|close))"
)


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ bar characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


def _extract_generic_modal(lines: list[str]) -> InteractiveUIContent | None:
    """Fallback detector for any Claude Code modal screen.

    Catches the long tail of full-screen pickers (e.g. /mcp server detail,
    future settings sub-screens) without a dedicated pattern per screen.
    A modal is recognised when the last non-blank line is a key-hint footer
    (:data:`_RE_MODAL_FOOTER`) and a full-width separator bar sits above it;
    the region between them is the modal body.

    This cannot misfire on normal output: when Claude is idle or working the
    pane's last line is its bottom chrome (``⏵⏵ bypass permissions…``), never
    a footer hint — the modal replaces that chrome while it is open.
    """
    last_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_idx = i
            break
    if last_idx is None or not _RE_MODAL_FOOTER.search(lines[last_idx]):
        return None

    # Nearest separator bar above the footer marks the top of the modal.
    top_idx: int | None = None
    for i in range(last_idx - 1, -1, -1):
        stripped = lines[i].strip()
        if len(stripped) >= 5 and set(stripped) <= _BAR_CHARS:
            top_idx = i
            break
    if top_idx is None or last_idx - top_idx < 2:
        return None

    content = "\n".join(lines[top_idx : last_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name="Modal")


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = _normalize_pane(pane_text).strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    # Fallback: catch any modal screen not covered by an explicit pattern.
    return _extract_generic_modal(lines)


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears immediately above
    the chrome separator (a full line of ``─`` characters).  We locate
    the separator first, then check the line just above it — this avoids
    false positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    """
    if not pane_text:
        return None

    lines = _normalize_pane(pane_text).split("\n")

    # Find the chrome separator: topmost ──── line in the last 10 lines
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            chrome_idx = i
            break

    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured iTerm2 pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
