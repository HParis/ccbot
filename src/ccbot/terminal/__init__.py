"""Pluggable terminal-backend layer.

ccbot hosts each Claude Code session inside a GUI terminal tab and drives
it (capture output, send keys, create/kill/rename tabs). Originally this
was hardwired to iTerm2; this package abstracts the host terminal behind
a single contract so other terminals (Otty, Kitty, Ghostty, ...) can be
added by writing one backend file and registering it — no call-site
changes.

Public surface:
  - ``TerminalBackend``  — the contract every backend implements
  - ``TerminalSession``  — neutral per-session record (replaces ITermWindow)
  - ``Capabilities``     — per-backend feature flags for graceful degradation
  - ``ReconnectListener``— callback type for reconnect events

The wired singleton lives in ``terminal.manager`` (imported separately to
keep this package's ``__init__`` side-effect-free and cycle-safe).
"""

from __future__ import annotations

from .base import Capabilities, ReconnectListener, TerminalBackend, TerminalSession

__all__ = [
    "Capabilities",
    "ReconnectListener",
    "TerminalBackend",
    "TerminalSession",
]
