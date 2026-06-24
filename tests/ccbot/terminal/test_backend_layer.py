"""Tests for the pluggable terminal-backend layer.

Locks in the contract that lets ccbot swap host terminals: the registry
resolves a backend by name to a single shared instance, the iTerm2 backend
satisfies the structural ``TerminalBackend`` protocol and declares its
capabilities, and ``ITermWindow`` remains an alias of the neutral
``TerminalSession`` for backward compatibility.
"""

from __future__ import annotations

import pytest

from ccbot.terminal import Capabilities, TerminalBackend, TerminalSession
from ccbot.terminal import registry


def test_iterm2_registered_and_resolves_to_singleton() -> None:
    from ccbot.iterm2_manager import iterm2_manager

    assert "iterm2" in registry.available()
    # get() returns the module singleton, and is memoized (same object twice).
    assert registry.get("iterm2") is iterm2_manager
    assert registry.get("iterm2") is registry.get("iterm2")


def test_terminal_manager_is_the_iterm2_singleton_by_default() -> None:
    from ccbot.iterm2_manager import iterm2_manager
    from ccbot.terminal.manager import terminal_manager

    # Default backend is iterm2, so the wired manager IS the singleton —
    # consumers and the test suite share one instance and its connection.
    assert terminal_manager is iterm2_manager


def test_iterm2_satisfies_backend_protocol() -> None:
    from ccbot.iterm2_manager import iterm2_manager

    assert isinstance(iterm2_manager, TerminalBackend)


def test_iterm2_declares_full_capabilities() -> None:
    from ccbot.iterm2_manager import iterm2_manager

    caps = iterm2_manager.capabilities
    assert isinstance(caps, Capabilities)
    assert caps.ansi_capture
    assert caps.native_tagging
    assert caps.reconnect_events
    assert caps.screenshot


def test_itermwindow_is_terminalsession_alias() -> None:
    from ccbot.iterm2_manager import ITermWindow

    assert ITermWindow is TerminalSession
    w = TerminalSession(window_id="UUID-1", window_name="proj", cwd="/tmp")
    assert w.pane_current_command == ""
    assert w.is_ccbot is False
    assert w.has_claude is False


def test_unknown_backend_raises_with_available_list() -> None:
    with pytest.raises(KeyError, match="iterm2"):
        registry.get("does-not-exist")
