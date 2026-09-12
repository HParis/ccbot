class TestPickerLabel:
    """Adopt-tab picker labels: tab name first, cwd basename as fallback."""

    def test_prefers_named_tab_over_stale_home_cwd(self) -> None:
        from pathlib import Path

        from ccbot.handlers.directory_browser import picker_label

        # Without shell integration iTerm2 reports the start dir (~).
        assert picker_label("Surge", str(Path.home())) == "Surge"

    def test_generic_name_falls_back_to_cwd_basename(self) -> None:
        from ccbot.handlers.directory_browser import picker_label

        assert picker_label("Default", "/x/y/quin-ios") == "quin-ios"
        assert picker_label("zsh", "/x/y/dev") == "dev"
        assert picker_label("", "/x/y/dev") == "dev"

    def test_home_basename_is_not_a_label(self) -> None:
        from pathlib import Path

        from ccbot.handlers.directory_browser import picker_label

        home = Path.home()
        assert picker_label("Default", str(home)) == "Default"
        assert picker_label(home.name, str(home)) == home.name
        assert picker_label("", str(home)) == "(unnamed)"

    def test_unnamed_without_cwd(self) -> None:
        from ccbot.handlers.directory_browser import picker_label

        assert picker_label("", "") == "(unnamed)"


class TestWorkspacePicker:
    """The picker shown for backends that cannot host a session in an
    arbitrary directory (Capabilities.arbitrary_cwd=False)."""

    @staticmethod
    def _ws(n: int):
        from ccbot.terminal.base import Workspace

        return [
            Workspace(path=f"/p/proj{i}", label=f"proj{i}", detail="main")
            for i in range(n)
        ]

    def test_buttons_carry_an_index_not_a_path(self) -> None:
        """Telegram caps callback_data at 64 bytes; a worktree path can be
        far longer than that (iCloud paths in particular)."""
        from ccbot.handlers.directory_browser import build_workspace_picker

        _, keyboard, paths = build_workspace_picker(self._ws(3))
        datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        assert "ws:sel:0" in datas
        assert paths == ["/p/proj0", "/p/proj1", "/p/proj2"]
        assert all(len(d.encode()) <= 64 for d in datas if d)

    def test_label_shows_project_and_branch(self) -> None:
        from ccbot.handlers.directory_browser import build_workspace_picker

        _, keyboard, _ = build_workspace_picker(self._ws(1))
        assert "proj0 · main" in keyboard.inline_keyboard[0][0].text

    def test_paginates_and_indexes_globally(self) -> None:
        from ccbot.handlers.directory_browser import build_workspace_picker

        _, keyboard, paths = build_workspace_picker(self._ws(8), page=1)
        datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        # Page 2 holds the 7th and 8th entries, still addressed by global index.
        assert "ws:sel:6" in datas and "ws:sel:7" in datas
        assert len(paths) == 8  # full list is cached, not just the page

    def test_empty_list_explains_instead_of_offering_nothing(self) -> None:
        from ccbot.handlers.directory_browser import build_workspace_picker

        text, keyboard, paths = build_workspace_picker([])
        assert paths == []
        assert "registered projects only" in text
        # Only Cancel — no dead buttons.
        datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        assert datas == ["ws:cancel"]


class TestWindowPickerEscapeHatch:
    """The 'instead' button under the adopt-tab picker must describe what it
    opens, which depends on the backend's capability."""

    @staticmethod
    def _windows():
        return [("W1", "main", "/p/main", True)]

    def test_browse_label_when_any_directory_works(self) -> None:
        from ccbot.handlers import directory_browser as db

        # Default backend (iTerm2) declares arbitrary_cwd=True.
        assert db.terminal_manager.capabilities.arbitrary_cwd is True
        _, keyboard, _ = db.build_window_picker(self._windows())
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        assert any("Browse directories" in x for x in labels)

    def test_project_label_when_cwd_is_constrained(self, monkeypatch) -> None:
        from dataclasses import replace

        from ccbot.handlers import directory_browser as db

        caps = replace(db.terminal_manager.capabilities, arbitrary_cwd=False)
        monkeypatch.setattr(
            type(db.terminal_manager), "capabilities", property(lambda self: caps)
        )
        _, keyboard, _ = db.build_window_picker(self._windows())
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        assert any("Pick a project" in x for x in labels)
        assert not any("Browse directories" in x for x in labels)
