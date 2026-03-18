from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
import re
from typing import Callable, Optional

try:
    from .codex_runner import CommandResult, run_command
except ImportError:
    from codex_runner import CommandResult, run_command


@dataclass(slots=True)
class BlockedCommitPath:
    path: str
    reason: str


@dataclass(slots=True)
class CommitResult:
    commit_hash: str | None
    staged_paths: tuple[str, ...] = ()
    blocked_paths: tuple[BlockedCommitPath, ...] = ()


class GitError(RuntimeError):
    pass


_LINE_ENDING_WARNING_RE = re.compile(
    r"warning: (?:LF|CRLF) will be replaced by (?:LF|CRLF) in [^\r\n]+\r?\n"
    r"The file will have its original line endings in your working directory\r?\n?",
)


def _strip_line_ending_warnings(output: str) -> str:
    return _LINE_ENDING_WARNING_RE.sub("", output)


class GitManager:
    def __init__(self, repo_path: Path, remote: str = "origin") -> None:
        self.repo_path = repo_path
        self.remote = remote

    def _git(self, args: list[str], should_stop: Callable[[], bool]) -> CommandResult:
        result = run_command(["git", *args], cwd=self.repo_path, should_stop=should_stop)
        cleaned_output = _strip_line_ending_warnings(result.output)
        if cleaned_output == result.output:
            return result
        return CommandResult(
            command=result.command,
            returncode=result.returncode,
            output=cleaned_output,
            duration_seconds=result.duration_seconds,
            stopped=result.stopped,
        )

    def _list_paths(self, args: list[str], should_stop: Callable[[], bool], error_message: str) -> list[str]:
        result = self._git(args, should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or error_message)
        return [part for part in result.output.split("\0") if part]

    def _changed_paths(self, should_stop: Callable[[], bool]) -> list[str]:
        ordered_paths: dict[str, None] = {}
        sources = [
            self._list_paths(
                ["diff", "--name-only", "-z", "--"],
                should_stop=should_stop,
                error_message="Failed to inspect unstaged changes",
            ),
            self._list_paths(
                ["diff", "--cached", "--name-only", "-z", "--"],
                should_stop=should_stop,
                error_message="Failed to inspect staged changes",
            ),
            self._list_paths(
                ["ls-files", "--others", "--exclude-standard", "-z", "--"],
                should_stop=should_stop,
                error_message="Failed to inspect untracked files",
            ),
        ]
        for group in sources:
            for rel_path in group:
                ordered_paths[rel_path] = None
        return list(ordered_paths)

    def _staged_paths(self, should_stop: Callable[[], bool]) -> set[str]:
        return set(
            self._list_paths(
                ["diff", "--cached", "--name-only", "-z", "--"],
                should_stop=should_stop,
                error_message="Failed to inspect staged changes",
            )
        )

    def _matches_block_pattern(self, rel_path: str, block_patterns: tuple[str, ...]) -> str | None:
        normalized_path = rel_path.replace("\\", "/")
        file_name = Path(normalized_path).name
        for pattern in block_patterns:
            if fnmatch(normalized_path, pattern) or fnmatch(file_name, pattern):
                return pattern
        return None

    def _contains_sensitive_content(
        self,
        rel_path: str,
        content_markers: tuple[str, ...],
        content_max_bytes: int,
    ) -> str | None:
        target_path = (self.repo_path / rel_path).resolve()
        if not target_path.exists() or not target_path.is_file():
            return None

        try:
            file_size = target_path.stat().st_size
            if file_size > content_max_bytes:
                return None
            content = target_path.read_bytes()
        except OSError:
            return None

        lowered_content = content.lower()
        for marker in content_markers:
            marker_bytes = marker.encode("utf-8", errors="ignore")
            if marker_bytes and marker_bytes.lower() in lowered_content:
                return marker
        return None

    def _blocked_commit_path(
        self,
        rel_path: str,
        block_patterns: tuple[str, ...],
        content_markers: tuple[str, ...],
        content_max_bytes: int,
    ) -> BlockedCommitPath | None:
        pattern = self._matches_block_pattern(rel_path, block_patterns)
        if pattern:
            return BlockedCommitPath(rel_path, f"matches protected pattern `{pattern}`")

        marker = self._contains_sensitive_content(rel_path, content_markers, content_max_bytes)
        if marker:
            return BlockedCommitPath(rel_path, f"contains sensitive marker `{marker}`")
        return None

    def _stage_paths(self, paths: list[str], should_stop: Callable[[], bool]) -> None:
        if not paths:
            return
        batch_size = 50
        for index in range(0, len(paths), batch_size):
            batch = paths[index : index + batch_size]
            result = self._git(["add", "-A", "--", *batch], should_stop=should_stop)
            if result.returncode != 0:
                raise GitError(result.output.strip() or "git add failed")

    def ensure_repository(self) -> None:
        if not (self.repo_path / ".git").exists():
            raise GitError(f"Not a git repository: {self.repo_path}")

    def has_uncommitted_changes(self, should_stop: Callable[[], bool]) -> bool:
        result = self._git(["status", "--porcelain"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to query git status")
        return bool(result.output.strip())

    def ensure_clean_worktree(self, should_stop: Callable[[], bool]) -> None:
        if self.has_uncommitted_changes(should_stop=should_stop):
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

    def push_current_branch(self, should_stop: Callable[[], bool], branch_name: str | None = None) -> str:
        branch = branch_name or self.current_branch(should_stop)
        self.push_branch(branch, should_stop=should_stop)
        return f"Pushed branch {branch} to {self.remote}"

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

    def local_branch_exists(self, branch_name: str, should_stop: Callable[[], bool]) -> bool:
        result = self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], should_stop=should_stop)
        if result.returncode in {0, 1}:
            return result.returncode == 0
        raise GitError(result.output.strip() or f"Failed to inspect local branch {branch_name}")

    def remote_branch_exists(self, branch_name: str, should_stop: Callable[[], bool]) -> bool:
        result = self._git(
            ["show-ref", "--verify", "--quiet", f"refs/remotes/{self.remote}/{branch_name}"],
            should_stop=should_stop,
        )
        if result.returncode in {0, 1}:
            return result.returncode == 0
        raise GitError(result.output.strip() or f"Failed to inspect remote branch {self.remote}/{branch_name}")

    def checkout_branch(self, branch_name: str, should_stop: Callable[[], bool]) -> str:
        if self.current_branch(should_stop) == branch_name:
            return f"Already on {branch_name}"

        if self.local_branch_exists(branch_name, should_stop=should_stop):
            result = self._git(["checkout", branch_name], should_stop=should_stop)
        elif self.remote_branch_exists(branch_name, should_stop=should_stop):
            result = self._git(["checkout", "-b", branch_name, "--track", f"{self.remote}/{branch_name}"], should_stop=should_stop)
        else:
            raise GitError(f"Branch {branch_name} does not exist locally or on {self.remote}")

        if result.returncode != 0:
            raise GitError(result.output.strip() or f"Failed to checkout branch {branch_name}")
        return result.output.strip() or f"Checked out {branch_name}"

    def sync_branch(self, branch_name: str, should_stop: Callable[[], bool]) -> tuple[str, str, str]:
        fetch_output = self.fetch(should_stop=should_stop)
        checkout_output = self.checkout_branch(branch_name, should_stop=should_stop)
        pull_output = self.pull_current_branch(should_stop=should_stop, branch_name=branch_name)
        return fetch_output, checkout_output, pull_output

    def has_changes(self, should_stop: Callable[[], bool]) -> bool:
        result = self._git(["status", "--porcelain"], should_stop=should_stop)
        if result.returncode != 0:
            raise GitError(result.output.strip() or "Failed to query git status")
        return bool(result.output.strip())

    def has_staged_changes(self, should_stop: Callable[[], bool]) -> bool:
        return bool(self._staged_paths(should_stop=should_stop))

    def commit_all(
        self,
        message: str,
        should_stop: Callable[[], bool],
        block_patterns: tuple[str, ...] = (),
        content_markers: tuple[str, ...] = (),
        content_max_bytes: int = 200_000,
    ) -> CommitResult:
        changed_paths = self._changed_paths(should_stop=should_stop)
        blocked_paths: list[BlockedCommitPath] = []
        allowed_paths: list[str] = []

        for rel_path in changed_paths:
            blocked = self._blocked_commit_path(
                rel_path,
                block_patterns=block_patterns,
                content_markers=content_markers,
                content_max_bytes=content_max_bytes,
            )
            if blocked is not None:
                blocked_paths.append(blocked)
            else:
                allowed_paths.append(rel_path)

        staged_paths = self._staged_paths(should_stop=should_stop)
        blocked_staged = [item.path for item in blocked_paths if item.path in staged_paths]
        if blocked_staged:
            blocked_preview = ", ".join(blocked_staged[:5])
            suffix = "" if len(blocked_staged) <= 5 else ", ..."
            raise GitError(
                "Sensitive files are already staged and would be unsafe to commit. "
                f"Please unstage them first: {blocked_preview}{suffix}"
            )

        self._stage_paths(allowed_paths, should_stop=should_stop)

        if not self.has_staged_changes(should_stop=should_stop):
            return CommitResult(
                commit_hash=None,
                staged_paths=tuple(allowed_paths),
                blocked_paths=tuple(blocked_paths),
            )

        commit_result = self._git(["commit", "-m", message], should_stop=should_stop)
        if commit_result.returncode != 0:
            raise GitError(commit_result.output.strip() or "git commit failed")

        hash_result = self._git(["rev-parse", "HEAD"], should_stop=should_stop)
        if hash_result.returncode != 0:
            raise GitError(hash_result.output.strip() or "Failed to read commit hash")
        return CommitResult(
            commit_hash=hash_result.output.strip().splitlines()[-1],
            staged_paths=tuple(allowed_paths),
            blocked_paths=tuple(blocked_paths),
        )

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

