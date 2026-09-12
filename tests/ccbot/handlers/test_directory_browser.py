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
