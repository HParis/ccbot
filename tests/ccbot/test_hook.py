"""Tests for Claude Code session tracking hook."""

import io
import json
import sys

import pytest

from ccbot.hook import _UUID_RE, _is_hook_installed, hook_main


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestIsHookInstalled:
    def test_hook_present(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "ccbot hook", "timeout": 5}
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True

    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) is False

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) is False

    def test_full_path_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/ccbot hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True


class TestHookMainValidation:
    def _run_hook_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict,
        *,
        iterm_session_id: str | None = None,
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        # The hook reads ITERM_SESSION_ID; tests that need to suppress
        # it pass None.  TMUX_PANE is no longer consulted.
        monkeypatch.delenv("TMUX_PANE", raising=False)
        if iterm_session_id is None:
            monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("ITERM_SESSION_ID", iterm_session_id)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()


class TestHookMainItermKey:
    """End-to-end checks against ITERM_SESSION_ID-driven session_map writes."""

    _CLAUDE_ID = "550e8400-e29b-41d4-a716-446655440000"
    _ITERM_UUID = "9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB"

    def _payload(self, cwd: str = "/tmp/proj") -> dict:
        return {
            "session_id": self._CLAUDE_ID,
            "cwd": cwd,
            "hook_event_name": "SessionStart",
        }

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict,
        *,
        iterm_session_id: str | None,
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.delenv("TMUX_PANE", raising=False)
        if iterm_session_id is None:
            monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("ITERM_SESSION_ID", iterm_session_id)
        hook_main()

    def test_writes_iterm_keyed_session_map(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run(
            monkeypatch,
            self._payload(),
            iterm_session_id=f"w0t1p0:{self._ITERM_UUID}",
        )

        data = json.loads((tmp_path / "session_map.json").read_text())
        key = f"iterm:{self._ITERM_UUID}"
        assert key in data
        assert data[key]["session_id"] == self._CLAUDE_ID
        assert data[key]["cwd"] == "/tmp/proj"
        # window_name is left empty — bot resolves it via its own
        # iTerm2 connection at read time.
        assert data[key]["window_name"] == ""

    def test_skips_when_iterm_session_id_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        monkeypatch.delenv("CCBOT_SESSION_KEY", raising=False)
        self._run(monkeypatch, self._payload(), iterm_session_id=None)
        assert not (tmp_path / "session_map.json").exists()

    def test_skips_when_iterm_session_id_has_no_colon(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A malformed env var without the wXtYpZ:UUID structure must
        not crash the hook or write garbage."""
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run(monkeypatch, self._payload(), iterm_session_id="garbage")
        assert not (tmp_path / "session_map.json").exists()

    def test_skips_when_iterm_uuid_is_not_a_uuid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run(monkeypatch, self._payload(), iterm_session_id="w0t0p0:not-uuid")
        assert not (tmp_path / "session_map.json").exists()

    def test_overwrites_previous_entry_for_same_uuid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Re-running Claude in the same iTerm2 tab updates the entry
        rather than appending a duplicate."""
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        env = f"w0t1p0:{self._ITERM_UUID}"
        self._run(monkeypatch, self._payload(cwd="/tmp/old"), iterm_session_id=env)
        new_payload = {
            "session_id": "00000000-1111-2222-3333-444444444444",
            "cwd": "/tmp/new",
            "hook_event_name": "SessionStart",
        }
        self._run(monkeypatch, new_payload, iterm_session_id=env)

        data = json.loads((tmp_path / "session_map.json").read_text())
        assert len(data) == 1
        entry = data[f"iterm:{self._ITERM_UUID}"]
        assert entry["session_id"] == "00000000-1111-2222-3333-444444444444"
        assert entry["cwd"] == "/tmp/new"

    def test_preserves_unrelated_entries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Other sessions' map entries (different UUIDs, including
        legacy tmux-prefix keys) must survive an unrelated write."""
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        existing = {
            "iterm:OTHER-UUID": {
                "session_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "cwd": "/tmp/other",
                "window_name": "",
            },
            "ccbot:@5": {
                "session_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "cwd": "/tmp/legacy",
                "window_name": "legacy",
            },
        }
        (tmp_path / "session_map.json").write_text(json.dumps(existing))

        self._run(
            monkeypatch,
            self._payload(),
            iterm_session_id=f"w0t1p0:{self._ITERM_UUID}",
        )

        data = json.loads((tmp_path / "session_map.json").read_text())
        assert "iterm:OTHER-UUID" in data
        assert "ccbot:@5" in data  # Unit 6 prunes legacy entries on read
        assert f"iterm:{self._ITERM_UUID}" in data


class TestHookMainCcbotKey:
    """Checks for ccbot-injected CCBOT_SESSION_KEY (Orca and other backends
    with no per-session env id)."""

    _CLAUDE_ID = "550e8400-e29b-41d4-a716-446655440000"

    def _payload(self, cwd: str = "/tmp/proj") -> dict:
        return {
            "session_id": self._CLAUDE_ID,
            "cwd": cwd,
            "hook_event_name": "SessionStart",
        }

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict,
        *,
        ccbot_key: str | None,
        iterm_session_id: str | None = None,
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.delenv("TMUX_PANE", raising=False)
        if ccbot_key is None:
            monkeypatch.delenv("CCBOT_SESSION_KEY", raising=False)
        else:
            monkeypatch.setenv("CCBOT_SESSION_KEY", ccbot_key)
        if iterm_session_id is None:
            monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("ITERM_SESSION_ID", iterm_session_id)
        hook_main()

    def test_writes_ccbot_key_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run(
            monkeypatch,
            self._payload(),
            ccbot_key="orca:term_b562359d-824f-4c97-b755-bc0ef914cffc",
        )

        data = json.loads((tmp_path / "session_map.json").read_text())
        assert "orca:term_b562359d-824f-4c97-b755-bc0ef914cffc" in data
        assert (
            data["orca:term_b562359d-824f-4c97-b755-bc0ef914cffc"]["session_id"]
            == self._CLAUDE_ID
        )
        assert (
            data["orca:term_b562359d-824f-4c97-b755-bc0ef914cffc"]["cwd"] == "/tmp/proj"
        )

    def test_ccbot_key_takes_priority_over_iterm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run(
            monkeypatch,
            self._payload(),
            ccbot_key="orca:term_3bd32ef3-fed3-4809-b342-662f60934e84",
            iterm_session_id="w0t1p0:9F2E3A1B-DEAD-BEEF-CAFE-0123456789AB",
        )
        data = json.loads((tmp_path / "session_map.json").read_text())
        assert list(data.keys()) == ["orca:term_3bd32ef3-fed3-4809-b342-662f60934e84"]

    def test_malformed_ccbot_key_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run(monkeypatch, self._payload(), ccbot_key="no-colon-here")
        assert not (tmp_path / "session_map.json").exists()
