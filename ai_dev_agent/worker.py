from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    from .codex_runner import discover_test_commands, run_codex, run_command
    from .config import Settings
    from .git_manager import GitError, GitManager
    from .task_manager import TaskManager, TaskRecord
except ImportError:
    from codex_runner import discover_test_commands, run_codex, run_command
    from config import Settings
    from git_manager import GitError, GitManager
    from task_manager import TaskManager, TaskRecord


class Worker(threading.Thread):
    def __init__(self, settings: Settings, task_manager: TaskManager) -> None:
        super().__init__(daemon=True)
        self.settings = settings
        self.task_manager = task_manager
        self.logger = logging.getLogger("ai_dev_agent.worker")
        self._shutdown = threading.Event()

    def stop(self) -> None:
        self._shutdown.set()

    def run(self) -> None:
        self.logger.info("Worker started")
        while not self._shutdown.is_set():
            task = self.task_manager.reserve_next_task(timeout_seconds=self.settings.poll_interval_seconds)
            if task is None:
                continue
            self._process_task(task)
        self.logger.info("Worker stopped")

    def _process_task(self, task: TaskRecord) -> None:
        self.logger.info("Starting task %s for repo %s", task.task_id, task.repository_alias)
        try:
            repo_alias, repo_path = self.settings.resolve_repository(task.repository_alias)
            git_manager = GitManager(repo_path, remote=self.settings.git_remote)
            self.task_manager.enqueue_notification(
                task.chat_id,
                f"Task queued execution: `{task.task_id}` on repo `{repo_alias}`",
            )
            git_manager.ensure_repository()
            self._ensure_not_stopped(task)
            self._prepare_clean_worktree(task, git_manager)

            branch_name = self._build_branch_name(repo_alias)
            self.task_manager.set_branch(task.task_id, branch_name)
            self.task_manager.update_progress(task.task_id, f"Creating branch {branch_name} in repo {repo_alias}")
            git_manager.checkout_new_branch(branch_name, self._should_stop_factory(task.task_id))
            self.task_manager.enqueue_notification(
                task.chat_id,
                f"Task `{task.task_id}` running on repo `{repo_alias}` branch `{branch_name}`",
            )

            self._run_codex_task(task, task.prompt, repo_path)
            if self._run_tests_with_recovery(task, repo_path):
                summary, commit_hash, diff = self._finalize_and_publish(task, branch_name, git_manager)
                self.task_manager.set_last_task_diff(task.task_id, diff)
                self.task_manager.mark_completed(task.task_id, summary=summary, commit_hash=commit_hash)
                self.task_manager.enqueue_notification(task.chat_id, summary)
            else:
                raise RuntimeError("Tests failed after auto-fix attempt.")
        except TaskStoppedError:
            self.logger.info("Task %s stopped by user", task.task_id)
            self.task_manager.mark_stopped(task.task_id)
            self.task_manager.enqueue_notification(task.chat_id, f"Task `{task.task_id}` stopped.")
        except (GitError, RuntimeError, ValueError) as exc:
            self.logger.exception("Task %s failed", task.task_id)
            self.task_manager.mark_failed(task.task_id, str(exc))
            self.task_manager.enqueue_notification(task.chat_id, f"Task `{task.task_id}` failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Unexpected error in task %s", task.task_id)
            self.task_manager.mark_failed(task.task_id, f"Unexpected error: {exc}")
            self.task_manager.enqueue_notification(task.chat_id, f"Task `{task.task_id}` failed unexpectedly: {exc}")

    def _build_branch_name(self, repo_alias: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
        sanitized_alias = re.sub(r"[^a-zA-Z0-9._-]+", "-", repo_alias).strip("-") or "repo"
        return f"{self.settings.task_branch_prefix}-{sanitized_alias}-{timestamp}"

    def _run_codex_task(self, task: TaskRecord, prompt: str, repo_path: Path) -> None:
        self._ensure_not_stopped(task)
        self.task_manager.update_progress(task.task_id, f"Running Codex CLI in repo {task.repository_alias}")
        result = run_codex(
            codex_binary=self.settings.codex_binary,
            prompt=prompt,
            cwd=repo_path,
            should_stop=self._should_stop_factory(task.task_id),
            on_output_line=lambda line: self.task_manager.append_output_line(task.task_id, line),
        )
        if result.stopped or self.task_manager.should_stop(task.task_id):
            raise TaskStoppedError()
        if result.returncode != 0:
            raise RuntimeError(f"Codex command failed with code {result.returncode}")

    def _prepare_clean_worktree(self, task: TaskRecord, git_manager: GitManager) -> None:
        should_stop = self._should_stop_factory(task.task_id)
        if not self.settings.auto_stash_when_dirty:
            git_manager.ensure_clean_worktree(should_stop)
            return

        self.task_manager.update_progress(task.task_id, "Checking repository status for auto-stash")
        stash_ref = git_manager.stash_if_dirty(
            should_stop=should_stop,
            message=f"auto-stash-before-{task.task_id}",
            include_untracked=self.settings.auto_stash_include_untracked,
        )
        if stash_ref:
            message = (
                f"Repository `{task.repository_alias}` was dirty. "
                f"Auto-stashed to `{stash_ref}` before task start."
            )
            self.task_manager.enqueue_notification(task.chat_id, message)
            self.task_manager.append_output_line(task.task_id, message)
        git_manager.ensure_clean_worktree(should_stop)

    def _run_tests_with_recovery(self, task: TaskRecord, repo_path: Path) -> bool:
        test_commands = discover_test_commands(repo_path)
        if not test_commands:
            self.task_manager.update_progress(task.task_id, "No tests detected; continuing")
            return True

        self.task_manager.update_progress(task.task_id, "Running tests")
        if self._run_any_test_command(task, test_commands, repo_path):
            return True

        self.task_manager.enqueue_notification(
            task.chat_id,
            f"Task `{task.task_id}` tests failed on repo `{task.repository_alias}`. Asking Codex to fix tests.",
        )
        self._run_codex_task(task, "Fix failing tests", repo_path)
        self.task_manager.update_progress(task.task_id, "Re-running tests after Codex fix")
        return self._run_any_test_command(task, test_commands, repo_path)

    def _run_any_test_command(self, task: TaskRecord, commands: list[list[str]], repo_path: Path) -> bool:
        for cmd in commands:
            self._ensure_not_stopped(task)
            self.task_manager.update_progress(task.task_id, f"Running tests: {' '.join(cmd)}")
            result = run_command(
                command=cmd,
                cwd=repo_path,
                should_stop=self._should_stop_factory(task.task_id),
                on_output_line=lambda line: self.task_manager.append_output_line(task.task_id, line),
            )
            if result.stopped or self.task_manager.should_stop(task.task_id):
                raise TaskStoppedError()
            if result.returncode == 0:
                return True
        return False

    def _format_blocked_commit_paths(self, blocked_paths, limit: int = 5) -> str:
        if not blocked_paths:
            return ""
        lines = ["Skipped sensitive files:"]
        for item in blocked_paths[:limit]:
            lines.append(f"- {item.path} ({item.reason})")
        remaining = len(blocked_paths) - limit
        if remaining > 0:
            lines.append(f"- ... and {remaining} more")
        return "\n".join(lines)

    def _finalize_and_publish(
        self,
        task: TaskRecord,
        branch_name: str,
        git_manager: GitManager,
    ) -> tuple[str, str | None, str]:
        self._ensure_not_stopped(task)
        self.task_manager.update_progress(task.task_id, "Committing changes")
        commit_result = git_manager.commit_all(
            "AI task result",
            self._should_stop_factory(task.task_id),
            block_patterns=self.settings.safe_commit_block_patterns,
            content_markers=self.settings.safe_commit_content_markers,
            content_max_bytes=self.settings.safe_commit_content_max_bytes,
        )
        blocked_text = self._format_blocked_commit_paths(commit_result.blocked_paths)
        commit_hash = commit_result.commit_hash
        if commit_hash is None:
            summary = (
                f"Task `{task.task_id}` finished on repo `{task.repository_alias}` branch `{branch_name}`.\n"
                "No safe file changes were produced, so commit/push were skipped."
            )
            if blocked_text:
                summary = f"{summary}\n{blocked_text}"
            return summary, None, "No commit diff available (no changes)."

        self._ensure_not_stopped(task)
        self.task_manager.update_progress(task.task_id, f"Pushing branch {branch_name}")
        git_manager.push_branch(branch_name, self._should_stop_factory(task.task_id))

        files_changed, lines_added, lines_removed = git_manager.commit_stats(
            commit_hash, self._should_stop_factory(task.task_id)
        )
        diff = git_manager.commit_diff(
            commit_hash, self._should_stop_factory(task.task_id), max_chars=self.settings.max_diff_chars
        )
        summary = (
            f"Task `{task.task_id}` completed.\n"
            f"Repo: `{task.repository_alias}`\n"
            f"Branch: `{branch_name}`\n"
            f"Commit: `{commit_hash}`\n"
            f"Files changed: {files_changed}\n"
            f"Lines added: {lines_added}\n"
            f"Lines removed: {lines_removed}"
        )
        if blocked_text:
            summary = f"{summary}\n{blocked_text}"
        return summary, commit_hash, diff

    def _ensure_not_stopped(self, task: TaskRecord) -> None:
        if self.task_manager.should_stop(task.task_id):
            raise TaskStoppedError()

    def _should_stop_factory(self, task_id: str):
        return lambda: self._shutdown.is_set() or self.task_manager.should_stop(task_id)


class TaskStoppedError(RuntimeError):
    pass
