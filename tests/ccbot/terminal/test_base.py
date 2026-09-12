"""Tests for the neutral terminal-layer helpers in ``terminal/base.py``."""

from ccbot.terminal.base import TerminalSession, is_running_claude


def _sess(job: str = "", title: str = "") -> TerminalSession:
    return TerminalSession(
        window_id="W",
        window_name="Main",
        cwd="/p",
        pane_current_command=job,
        job_title=title,
    )


class TestIsRunningClaude:
    def test_version_string_job_name_is_recognised_via_process_title(self) -> None:
        """iTerm2 reports a running Claude's jobName as its version string,
        so the job name alone identifies nothing — the process title does."""
        assert is_running_claude(_sess(job="2.1.269", title="claude"))

    def test_legacy_node_runtime_job_still_counts(self) -> None:
        assert is_running_claude(_sess(job="node-runtime", title="node-runtime"))

    def test_plain_shell_is_not_claude(self) -> None:
        assert not is_running_claude(_sess(job="zsh", title="zsh"))

    def test_either_signal_alone_is_enough(self) -> None:
        assert is_running_claude(_sess(job="claude"))
        assert is_running_claude(_sess(title="claude"))

    def test_no_signals_gets_the_benefit_of_the_doubt(self) -> None:
        """A backend that can't introspect its jobs must not have every
        caller treat its sessions as "not Claude"."""
        assert is_running_claude(_sess())
