from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

try:
    from .codex_runner import CommandResult, run_command
except ImportError:
    from codex_runner import CommandResult, run_command


class GitError(RuntimeError):
    pass


class GitManager:
    def __init__(self, repo_path: Path, remote: str = "origin") -> None:
        self.repo_path = repo_path
        self.remote = remote

    def _git(self, args: list[str], should_stop: Callable[[], bool]) -> CommandResult:
        return run_command(["git", *args], cwd=self.repo_path, should_stop=should_stop)

    def ensure_repository(self) -> None:
        if not (self.repo_path / ".git").exists():
            raise GitError(f"Not a git repository: {self.repo_path}")

    def ensure_clean_worktree(self, should_stop: Callable[[], bool]) -> None:
        result = self._git(["status", "--porcelain"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to query git status")
        if result.output.strip():
            raise GitError(
                "Repository has uncommitted changes. Commit/stash before starting task "
                "or enable AUTO_STASH_WHEN_DIRTY=true."
            )

    def current_branch(self, should_stop: Callable[[], bool]) -> str:
        result = self._git(["rev-parse", "--abbrev-ref", "HEAD"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to read current branch")
        branch = result.output.strip().splitlines()
        return branch[-1] if branch else "HEAD"

    def status_short(self, should_stop: Callable[[], bool]) -> str:
        result = self._git(["status", "--short", "--branch"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to query git status")
        return result.output.strip() or "Working tree clean"

    def list_branches(self, should_stop: Callable[[], bool]) -> str:
        result = self._git(["branch", "--all", "--verbose", "--no-abbrev"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to list branches")
        return result.output.strip() or "No branches found"

    def recent_commits(self, should_stop: Callable[[], bool], limit: int = 5) -> str:
        safe_limit = max(1, min(limit, 20))
        result = self._git(
            ["log", f"--max-count={safe_limit}", "--pretty=format:%h %ad %s", "--date=short"],
            should_stop=should_stop,
        )
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to read git log")
        return result.output.strip() or "No commits found"

    def fetch(self, should_stop: Callable[[], bool]) -> str:
        result = self._git(["fetch", self.remote, "--prune"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or f"git fetch failed for remote {self.remote}")
        return result.output.strip() or f"Fetched from {self.remote}"

    def pull_current_branch(self, should_stop: Callable[[], bool], branch_name: str | None = None) -> str:
        branch = branch_name or self.current_branch(should_stop)
        result = self._git(["pull", self.remote, branch], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or f"git pull failed for {self.remote}/{branch}")
        return result.output.strip() or f"Already up to date with {self.remote}/{branch}"

    def get_remote_url(self, should_stop: Callable[[], bool], remote_name: str | None = None) -> str | None:
        remote = remote_name or self.remote
        result = self._git(["remote", "get-url", remote], should_stop=should_stop)
        if result.returncode != 0:
            output = result.output.strip().lower()
            if "no such remote" in output or "not a valid remote name" in output:
                return None
            raise GitError(result.output.strip() or f"Failed to read remote URL for {remote}")
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        return lines[-1] if lines else None

    def set_remote_url(self, remote_url: str, should_stop: Callable[[], bool], remote_name: str | None = None) -> str:
        remote = remote_name or self.remote
        current_url = self.get_remote_url(should_stop=should_stop, remote_name=remote)
        if current_url == remote_url:
            return f"Remote {remote} already points to {remote_url}"

        if current_url:
            result = self._git(["remote", "set-url", remote, remote_url], should_stop=should_stop)
            action = "updated"
        else:
            result = self._git(["remote", "add", remote, remote_url], should_stop=should_stop)
            action = "added"
        if result.returncode != 0:
            raise GitError(result.output.strip() or f"Failed to configure remote {remote}")
        return f"Remote {remote} {action}: {remote_url}"

    def stash_if_dirty(
        self,
        should_stop: Callable[[], bool],
        message: str,
        include_untracked: bool = True,
    ) -> Optional[str]:
        if not self.has_changes(should_stop=should_stop):
            return None

        args = ["stash", "push"]
        if include_untracked:
            args.append("-u")
        args.extend(["-m", message])

        stash_result = self._git(args, should_stop=should_stop)
        if stash_result.returncode != 0:
            raise GitError(stash_result.output.strip() or "git stash failed")
        if "No local changes to save" in stash_result.output:
            return None

        ref_result = self._git(["stash", "list", "-n", "1", "--format=%gd"], should_stop=should_stop)
        if ref_result.returncode != 0:
            raise GitError(ref_result.output.strip() or "Unable to read latest stash ref")

        ref_lines = [line.strip() for line in ref_result.output.splitlines() if line.strip()]
        return ref_lines[0] if ref_lines else "stash@{0}"

    def checkout_new_branch(self, branch_name: str, should_stop: Callable[[], bool]) -> None:
        result = self._git(["checkout", "-b", branch_name], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or f"Failed to create branch {branch_name}")

    def has_changes(self, should_stop: Callable[[], bool]) -> bool:
        result = self._git(["status", "--porcelain"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to query git status")
        return bool(result.output.strip())

    def commit_all(self, message: str, should_stop: Callable[[], bool]) -> Optional[str]:
        add_result = self._git(["add", "."], should_stop=should_stop)
        if add_result.returncode != 0:
            raise GitError(add_result.output.strip() or "git add failed")

        if not self.has_changes(should_stop=should_stop):
            return None

        commit_result = self._git(["commit", "-m", message], should_stop=should_stop)
        if commit_result.returncode != 0:
            raise GitError(commit_result.output.strip() or "git commit failed")

        hash_result = self._git(["rev-parse", "HEAD"], should_stop=should_stop)
        if hash_result.returncode != 0:
            raise GitError(hash_result.output.strip() or "Failed to read commit hash")
        return hash_result.output.strip().splitlines()[-1]

    def push_branch(self, branch_name: str, should_stop: Callable[[], bool]) -> None:
        result = self._git(["push", "-u", self.remote, branch_name], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or f"git push failed for {branch_name}")

    def commit_stats(self, commit_hash: str, should_stop: Callable[[], bool]) -> tuple[int, int, int]:
        result = self._git(["show", "--numstat", "--format=", commit_hash], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Unable to build commit stats")

        files_changed = 0
        lines_added = 0
        lines_removed = 0
        for line in result.output.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 3:
                continue
            files_changed += 1
            add_raw, del_raw = parts[0], parts[1]
            if add_raw.isdigit():
                lines_added += int(add_raw)
            if del_raw.isdigit():
                lines_removed += int(del_raw)
        return files_changed, lines_added, lines_removed

    def commit_diff(self, commit_hash: str, should_stop: Callable[[], bool], max_chars: int = 3500) -> str:
        result = self._git(["show", "--no-color", commit_hash], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Unable to generate commit diff")
        diff = result.output
        if len(diff) > max_chars:
            return diff[:max_chars] + "\n... (truncated)"
        return diff
