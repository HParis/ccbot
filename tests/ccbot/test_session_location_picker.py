"""Where a new session is allowed to run, chosen by backend capability.

``_build_session_location_picker`` is the one place that decides between the
filesystem browser and the workspace picker. It branches on
``Capabilities.arbitrary_cwd``, never on a backend name, so a backend that
hosts sessions only inside registered projects (Orca) degrades by showing
what it will accept instead of letting the user pick a path that is refused
afterwards.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

from ccbot import bot as bot_mod
from ccbot.handlers.directory_browser import (
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_WORKSPACE,
    WORKSPACES_KEY,
)
from ccbot.terminal.base import Workspace


class _Ctx:
    def __init__(self) -> None:
        self.user_data: dict = {}


# Snapshot the real capabilities once, before any patch: reading the property
# from inside its own replacement would recurse.
_REAL_CAPS = bot_mod.terminal_manager.capabilities


def _caps(arbitrary_cwd: bool):
    return replace(_REAL_CAPS, arbitrary_cwd=arbitrary_cwd)


async def test_arbitrary_cwd_backend_gets_the_directory_browser() -> None:
    ctx = _Ctx()
    with patch.object(
        type(bot_mod.terminal_manager),
        "capabilities",
        property(lambda self: _caps(True)),
    ):
        text, keyboard = await bot_mod._build_session_location_picker(ctx)  # type: ignore[arg-type]

    assert ctx.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert "Working Directory" in text
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "db:confirm" in datas


async def test_constrained_backend_gets_the_workspace_picker() -> None:
    ctx = _Ctx()
    spaces = [
        Workspace(path="/p/ccbot", label="ccbot", detail="main"),
        Workspace(path="/p/app", label="app", detail="dev"),
    ]
    with (
        patch.object(
            type(bot_mod.terminal_manager),
            "capabilities",
            property(lambda self: _caps(False)),
        ),
        patch.object(
            bot_mod.terminal_manager,
            "list_workspaces",
            AsyncMock(return_value=spaces),
        ),
    ):
        text, keyboard = await bot_mod._build_session_location_picker(ctx)  # type: ignore[arg-type]

    assert ctx.user_data[STATE_KEY] == STATE_SELECTING_WORKSPACE
    # The paths are cached for the index-addressed callbacks.
    assert ctx.user_data[WORKSPACES_KEY] == ["/p/ccbot", "/p/app"]
    assert "Select a Project" in text
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "ws:sel:0" in datas
    # No filesystem navigation is offered — there is nothing to navigate.
    assert not any(d and d.startswith("db:") for d in datas)


async def test_constrained_backend_with_no_projects_says_so() -> None:
    """An empty list is a real state (Orca with no registered repo), and it
    has to explain the fix rather than render an empty keyboard."""
    ctx = _Ctx()
    with (
        patch.object(
            type(bot_mod.terminal_manager),
            "capabilities",
            property(lambda self: _caps(False)),
        ),
        patch.object(
            bot_mod.terminal_manager, "list_workspaces", AsyncMock(return_value=[])
        ),
    ):
        text, keyboard = await bot_mod._build_session_location_picker(ctx)  # type: ignore[arg-type]

    assert "No projects available" in text
    assert ctx.user_data[WORKSPACES_KEY] == []
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert datas == ["ws:cancel"]
