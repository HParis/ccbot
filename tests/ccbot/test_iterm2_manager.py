"""Tests for iterm2_manager scaffolding (Unit 1).

Asserts the public surface mirrors the previous TmuxManager so callers
can swap their import without further changes. Live API calls are
deferred to later units.
"""

from __future__ import annotations

import inspect

import pytest

from ccbot.iterm2_manager import ITerm2Manager, ITermWindow, iterm2_manager


def test_singleton_exists() -> None:
    assert isinstance(iterm2_manager, ITerm2Manager)


def test_window_dataclass_fields() -> None:
    w = ITermWindow(
        window_id="UUID-1",
        window_name="proj",
        cwd="/tmp",
        pane_current_command="claude",
    )
    assert w.window_id == "UUID-1"
    assert w.window_name == "proj"
    assert w.cwd == "/tmp"
    assert w.pane_current_command == "claude"


@pytest.mark.parametrize(
    "method_name",
    [
        "list_windows",
        "find_window_by_name",
        "find_window_by_id",
        "capture_pane",
        "send_keys",
        "rename_window",
        "kill_window",
        "create_window",
    ],
)
def test_public_methods_present(method_name: str) -> None:
    """Each public method exists and is async."""
    method = getattr(ITerm2Manager, method_name, None)
    assert method is not None, f"missing: {method_name}"
    assert inspect.iscoroutinefunction(method), f"{method_name} must be async"


def test_signature_parity_with_tmux_manager() -> None:
    """Method signatures match the previous TmuxManager 1:1.

    During the migration window tmux_manager.py still exists; once
    Unit 6 deletes it this test is removed in the same change-set.
    """
    try:
        from ccbot.tmux_manager import TmuxManager  # type: ignore[attr-defined]
    except ImportError:
        pytest.skip("tmux_manager already removed")

    for name in (
        "list_windows",
        "find_window_by_name",
        "find_window_by_id",
        "capture_pane",
        "send_keys",
        "rename_window",
        "kill_window",
        "create_window",
    ):
        old = inspect.signature(getattr(TmuxManager, name))
        new = inspect.signature(getattr(ITerm2Manager, name))
        # Compare parameter names and kinds; ignore annotations because
        # the new module uses ITermWindow vs TmuxWindow returns.
        old_params = [(p.name, p.kind, p.default) for p in old.parameters.values()]
        new_params = [(p.name, p.kind, p.default) for p in new.parameters.values()]
        assert old_params == new_params, (
            f"{name}: signature drift — old={old_params} new={new_params}"
        )


@pytest.mark.parametrize(
    "method_name,args",
    [
        ("list_windows", ()),
        ("find_window_by_name", ("x",)),
        ("find_window_by_id", ("UUID",)),
        ("capture_pane", ("UUID",)),
        ("send_keys", ("UUID", "hi")),
        ("rename_window", ("UUID", "new")),
        ("kill_window", ("UUID",)),
        ("create_window", ("/tmp",)),
    ],
)
async def test_stubs_raise_not_implemented(
    method_name: str, args: tuple[object, ...]
) -> None:
    """Every stub raises NotImplementedError so accidental upper-layer
    use during the migration fails loudly instead of silently no-op.
    """
    method = getattr(iterm2_manager, method_name)
    with pytest.raises(NotImplementedError):
        await method(*args)
