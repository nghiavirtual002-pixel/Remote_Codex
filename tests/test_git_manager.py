from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from ai_dev_agent.codex_runner import CommandResult
from ai_dev_agent.git_manager import GitManager


class GitManagerLineEndingWarningTests(TestCase):
    def setUp(self) -> None:
        self.manager = GitManager(Path("."))

    def _result(self, output: str, returncode: int = 0) -> CommandResult:
        return CommandResult(
            command=["git"],
            returncode=returncode,
            output=output,
            duration_seconds=0.01,
        )

    @patch("ai_dev_agent.git_manager.run_command")
    def test_list_paths_ignores_line_ending_warning_noise(self, run_command_mock) -> None:
        run_command_mock.return_value = self._result(
            "warning: LF will be replaced by CRLF in client/viewer.html\n"
            "The file will have its original line endings in your working directory\n"
            "client/viewer.html\0"
        )

        paths = self.manager._list_paths(
            ["diff", "--cached", "--name-only", "-z", "--"],
            should_stop=lambda: False,
            error_message="Failed to inspect staged changes",
        )

        self.assertEqual(paths, ["client/viewer.html"])

    @patch("ai_dev_agent.git_manager.run_command")
    def test_git_preserves_real_errors_after_warning_cleanup(self, run_command_mock) -> None:
        run_command_mock.return_value = self._result(
            "warning: LF will be replaced by CRLF in client/viewer.html\n"
            "The file will have its original line endings in your working directory\n"
            "fatal: pathspec 'missing.txt' did not match any files\n",
            returncode=128,
        )

        result = self.manager._git(["add", "-A", "--", "missing.txt"], should_stop=lambda: False)

        self.assertEqual(result.output, "fatal: pathspec 'missing.txt' did not match any files\n")
