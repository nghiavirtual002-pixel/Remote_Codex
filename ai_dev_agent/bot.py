from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

try:
    from .codex_runner import run_codex
    from .config import configure_logging, load_settings
    from .git_manager import GitError, GitManager
    from .task_manager import TaskManager
    from .worker import Worker
except ImportError:
    from codex_runner import run_codex
    from config import configure_logging, load_settings
    from git_manager import GitError, GitManager
    from task_manager import TaskManager
    from worker import Worker

LOGGER: Final = logging.getLogger("ai_dev_agent.bot")


class TelegramTaskBot:
    def __init__(self) -> None:
        load_dotenv()
        self.settings = load_settings()
        configure_logging(self.settings.log_file)
        self.task_manager = TaskManager(
            state_file=self.settings.state_file,
            queue_maxsize=self.settings.queue_maxsize,
            max_saved_output_lines=self.settings.max_saved_output_lines,
        )
        self.worker = Worker(settings=self.settings, task_manager=self.task_manager)
        self._notification_task: asyncio.Task | None = None
        self.application: Application = (
            ApplicationBuilder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.application.add_handler(CommandHandler("start", self.help_cmd))
        self.application.add_handler(CommandHandler("help", self.help_cmd))
        self.application.add_handler(CommandHandler("repos", self.repos_cmd))
        self.application.add_handler(CommandHandler("repoinfo", self.repoinfo_cmd))
        self.application.add_handler(CommandHandler("use", self.use_cmd))
        self.application.add_handler(CommandHandler("newrepo", self.newrepo_cmd))
        self.application.add_handler(CommandHandler("setremote", self.setremote_cmd))
        self.application.add_handler(CommandHandler("clone", self.clone_cmd))
        self.application.add_handler(CommandHandler("gitstatus", self.gitstatus_cmd))
        self.application.add_handler(CommandHandler("branches", self.branches_cmd))
        self.application.add_handler(CommandHandler("gitlog", self.gitlog_cmd))
        self.application.add_handler(CommandHandler("pull", self.pull_cmd))
        self.application.add_handler(CommandHandler("commit", self.commit_cmd))
        self.application.add_handler(CommandHandler("push", self.push_cmd))
        self.application.add_handler(CommandHandler("ask", self.ask_cmd))
        self.application.add_handler(CommandHandler("task", self.task_cmd))
        self.application.add_handler(CommandHandler("status", self.status_cmd))
        self.application.add_handler(CommandHandler("diff", self.diff_cmd))
        self.application.add_handler(CommandHandler("stop", self.stop_cmd))

    async def _post_init(self, application: Application) -> None:
        self._notification_task = asyncio.create_task(self._notification_loop(application))

    async def _post_shutdown(self, application: Application) -> None:
        if self._notification_task:
            self._notification_task.cancel()
            try:
                await self._notification_task
            except asyncio.CancelledError:
                pass

    def _general_help_text(self) -> str:
        return (
            "Available commands:\n"
            "/help - show all commands\n"
            "/help <command> - explain one command in detail\n"
            "/repos - list configured repositories\n"
            "/repoinfo - show current repo path, branch, remote, status\n"
            "/use <repo> - choose the active repo for this chat\n"
            "/newrepo <alias> [folder_name] - create a local git repo under the configured root\n"
            "/setremote <repo_alias> [github_repo_name_or_url] - set origin for an existing repo\n"
            "/clone <alias> <git_url> [folder_name] - clone a remote repo under the configured root\n"
            "/gitstatus - show branch and working tree status\n"
            "/branches - list local and remote branches\n"
            "/gitlog [n] - show recent commits, default 5\n"
            "/pull [branch] - fetch and pull the selected repo\n"
            "/commit <message> - safely commit allowed local changes on the selected repo\n"
            "/push [branch] - push current branch or a specific branch\n"
            "/ask <question> - ask Codex about the current project in read-only mode\n"
            "/task <prompt> - queue a Codex coding task on the selected repo\n"
            "/task --repo <repo> <prompt> - queue a task on a specific repo\n"
            "/status - show current worker status\n"
            "/diff - show diff from latest completed task\n"
            "/stop - request stop for the running task"
        )

    def _command_help(self, command_name: str) -> str:
        command = command_name.strip().lower().lstrip("/")
        details = {
            "newrepo": (
                "`/newrepo <alias> [folder_name]`\n\n"
                "Lenh nay tao local git repo trong `D:/Nghia/...`, dang ky repo vao bot, va co the gan san remote `origin`.\n\n"
                "Flow hien tai:\n"
                "1. `/newrepo demo_app`\n"
                "2. bot tao folder + `git init` + branch `main`\n"
                "3. neu ban da cau hinh `GITHUB_OWNER`, bot se tu gan `origin` thanh repo GitHub cung ten `demo_app`\n"
                "4. ban tu tao repo `demo_app` tren GitHub\n"
                "5. `/task ...` co the commit va push len remote do\n\n"
                "Neu chua co `GITHUB_OWNER`, ban van co the tao repo local va dung `/setremote demo_app` sau khi bo sung owner, hoac `/setremote demo_app <git_url>` de tro thang den URL cu the.\n\n"
                "Dieu kien de push thanh cong:\n"
                "- repo GitHub da ton tai\n"
                "- may cua ban da dang nhap GitHub hoac co quyen push qua HTTPS/SSH"
            ),
            "setremote": (
                "`/setremote <repo_alias> [github_repo_name_or_url]`\n\n"
                "Gan hoac cap nhat `origin` cho repo da duoc bot quan ly.\n"
                "- Neu tham so thu hai la URL day du, bot dung URL do\n"
                "- Neu tham so thu hai la ten repo, bot tao URL GitHub tu `GITHUB_OWNER`\n"
                "- Neu bo qua tham so thu hai, bot dung chinh `repo_alias` lam ten repo GitHub\n\n"
                "Vi du:\n"
                "- `/setremote demo_app`\n"
                "- `/setremote demo_app my-repo-name`\n"
                "- `/setremote demo_app https://github.com/user/demo_app.git`"
            ),
            "ask": (
                "`/ask <question>`\n\n"
                "Lenh nay chay Codex o che do read-only tren repo dang chon de tra loi cau hoi ve du an.\n"
                "Phu hop de hoi ve luong xu ly, file lien quan, module nao goi module nao, logic business, diem can sua.\n\n"
                "Vi du:\n"
                "- `/ask luong login di qua nhung file nao?`\n"
                "- `/ask api upload anh duoc goi tu dau?`\n"
                "- `/ask giai thich flow tao task trong project nay`"
            ),
            "task": (
                "`/task <prompt>` hoac `/task --repo <repo_alias> <prompt>`\n\n"
                "Lenh nay giao Codex sua code tren repo dang chon, tu dong tao branch task, chay test neu co, commit va push.\n"
                "Neu repo moi da duoc gan `origin` dung cach thi push se len duoc ngay sau khi task xong."
            ),
            "pull": (
                "`/pull [branch]`\n\n"
                "Fetch + pull repo dang chon. Bot se chan lenh nay neu repo dang co task chay, hoac worktree dang ban."
            ),
            "commit": (
                "`/commit <message>`\n\n"
                "Lenh nay chi commit nhung file duoc coi la an toan trong repo dang chon.\n"
                "Bot se bo qua `.env`, file state, log, key, va cac file co dau hieu chua token/secret.\n"
                "Phu hop khi ban sua code thu cong hom truoc va quen commit.\n\n"
                "Vi du:\n"
                "- `/commit fix api upload bug`\n"
                "- `/commit initial local changes before task`"
            ),
            "push": (
                "`/push [branch]`\n\n"
                "Day branch hien tai, hoac branch ban chi dinh, len remote `origin`.\n"
                "Neu repo dang co task chay thi bot se chan lenh nay de tranh xung dot."
            ),
            "repoinfo": (
                "`/repoinfo`\n\n"
                "Cho ban biet repo dang chon, path local, branch hien tai, remote `origin`, repo co dang ban khong, va trang thai worktree."
            ),
        }
        return details.get(command, self._general_help_text())

    def _truncate(self, text: str, max_chars: int | None = None) -> str:
        limit = max_chars or min(self.settings.max_diff_chars, 3800)
        if len(text) <= limit:
            return text
        return text[:limit] + "\n... (truncated)"

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

    def _is_authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None:
            return False
        if not self.settings.allowed_chat_ids:
            return True
        return chat.id in self.settings.allowed_chat_ids

    async def _reject_if_unauthorized(self, update: Update) -> bool:
        if self._is_authorized(update):
            return False
        if update.effective_message:
            await update.effective_message.reply_text("Unauthorized chat. Configure TELEGRAM_ALLOWED_CHAT_IDS.")
        return True

    def _selected_repo_alias(self, chat_id: int) -> str:
        return self.task_manager.get_selected_repo(chat_id, self.settings.default_repository_alias)

    def _resolve_repo_for_chat(self, chat_id: int, alias: str | None = None) -> tuple[str, Path, GitManager]:
        selected_alias = alias or self._selected_repo_alias(chat_id)
        repository_alias, repo_path = self.settings.resolve_repository(selected_alias)
        git_manager = GitManager(repo_path, remote=self.settings.git_remote)
        git_manager.ensure_repository()
        return repository_alias, repo_path, git_manager

    def _ensure_repo_not_busy(self, repository_alias: str) -> None:
        if self.task_manager.is_repo_busy(repository_alias):
            raise RuntimeError(f"Repository `{repository_alias}` is busy with a running task")

    def _format_repo_list(self, chat_id: int) -> str:
        selected_alias = self._selected_repo_alias(chat_id)
        lines = [f"Configured repositories under root: {self.settings.repo_root}"]
        for alias, path in sorted(self.settings.repositories.items()):
            markers: list[str] = []
            if alias == self.settings.default_repository_alias:
                markers.append("default")
            if alias == selected_alias:
                markers.append("selected")
            if alias not in self.settings.env_repository_aliases:
                markers.append("dynamic")
            if self.task_manager.is_repo_busy(alias):
                markers.append("busy")
            suffix = f" [{', '.join(markers)}]" if markers else ""
            lines.append(f"- {alias}: {path}{suffix}")
        return "\n".join(lines)

    def _parse_task_request(self, args: list[str]) -> tuple[str | None, str]:
        if not args:
            return None, ""
        if args[0] == "--repo":
            if len(args) < 3:
                return None, ""
            return args[1], " ".join(args[2:]).strip()
        if args[0].startswith("--repo="):
            return args[0].split("=", 1)[1], " ".join(args[1:]).strip()
        return None, " ".join(args).strip()

    def _build_repo_path(self, folder_name: str) -> Path:
        cleaned = folder_name.strip()
        if not cleaned:
            raise ValueError("Repository folder name cannot be empty")
        candidate = (self.settings.repo_root / cleaned).resolve()
        repo_root = self.settings.repo_root.resolve()
        if candidate != repo_root and repo_root not in candidate.parents:
            raise ValueError(f"Repository folder must stay inside {repo_root}")
        return candidate

    def _resolve_remote_url(self, repository_name_or_url: str) -> str:
        candidate = repository_name_or_url.strip()
        if not candidate:
            raise ValueError("Remote URL or GitHub repo name cannot be empty")
        if "://" in candidate or candidate.startswith("git@"):
            return candidate
        remote_url = self.settings.build_github_remote_url(candidate)
        if not remote_url:
            raise ValueError(
                "Missing GITHUB_OWNER. Set it in .env or provide a full git URL when using /setremote."
            )
        return remote_url

    def _create_repository(self, alias: str, folder_name: str) -> tuple[str, Path, str | None]:
        repo_path = self._build_repo_path(folder_name)
        repo_path.mkdir(parents=True, exist_ok=True)

        has_contents = any(repo_path.iterdir())
        git_dir = repo_path / ".git"
        if has_contents and not git_dir.exists():
            raise ValueError(f"Target folder already exists and is not an empty git repo: {repo_path}")

        if not git_dir.exists():
            init_result = subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if init_result.returncode != 0:
                fallback_result = subprocess.run(
                    ["git", "init"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if fallback_result.returncode != 0:
                    detail = (fallback_result.stdout + fallback_result.stderr).strip() or "git init failed"
                    raise RuntimeError(detail)
                subprocess.run(
                    ["git", "branch", "-M", "main"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

        registered_alias, registered_path = self.settings.register_repository(alias, repo_path)
        remote_message: str | None = None
        github_remote = self.settings.build_github_remote_url(registered_alias)
        if github_remote:
            git_manager = GitManager(registered_path, remote=self.settings.git_remote)
            git_manager.ensure_repository()
            remote_message = git_manager.set_remote_url(github_remote, should_stop=lambda: False)
        return registered_alias, registered_path, remote_message

    def _configure_remote(self, repository_alias: str, repository_name_or_url: str | None = None) -> tuple[str, Path, str]:
        alias, repo_path = self.settings.resolve_repository(repository_alias)
        self._ensure_repo_not_busy(alias)
        git_manager = GitManager(repo_path, remote=self.settings.git_remote)
        git_manager.ensure_repository()
        remote_input = repository_name_or_url or alias
        remote_url = self._resolve_remote_url(remote_input)
        message = git_manager.set_remote_url(remote_url, should_stop=lambda: False)
        return alias, repo_path, message

    def _clone_repository(self, alias: str, git_url: str, folder_name: str | None = None) -> tuple[str, Path]:
        target_name = folder_name or alias
        repo_path = self._build_repo_path(target_name)
        if repo_path.exists() and any(repo_path.iterdir()):
            raise ValueError(f"Target folder already exists and is not empty: {repo_path}")
        repo_path.parent.mkdir(parents=True, exist_ok=True)

        clone_result = subprocess.run(
            ["git", "clone", git_url, str(repo_path)],
            cwd=self.settings.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if clone_result.returncode != 0:
            detail = (clone_result.stdout + clone_result.stderr).strip() or "git clone failed"
            raise RuntimeError(detail)

        return self.settings.register_repository(alias, repo_path)

    def _ask_project_sync(self, repo_path: Path, question: str) -> str:
        prompt = (
            "You are reading the current repository in read-only mode. "
            "Answer the user's question using the project files in this repo. "
            "Do not modify any file. Be concrete: mention important files, flows, and assumptions. "
            "If the answer is uncertain, say what is missing.\n\n"
            f"User question: {question.strip()}"
        )
        result = run_codex(
            codex_binary=self.settings.codex_binary,
            prompt=prompt,
            cwd=repo_path,
            should_stop=lambda: False,
            sandbox_mode="read-only",
        )
        if result.returncode != 0:
            raise RuntimeError(f"Codex ask failed with code {result.returncode}")
        answer = result.output.strip()
        if not answer:
            raise RuntimeError("Codex returned no answer")
        return answer

    async def _reply_error(self, message, exc: Exception, prefix: str = "Error") -> None:
        await message.reply_text(f"{prefix}: {exc}")

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if not update.effective_message:
            return
        if context.args:
            target = context.args[0]
            await update.effective_message.reply_text(self._command_help(target))
            return
        await update.effective_message.reply_text(self._general_help_text())

    async def repos_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        await message.reply_text(self._format_repo_list(chat.id))

    async def repoinfo_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        try:
            repository_alias, repo_path, git_manager = self._resolve_repo_for_chat(chat.id)
            branch = git_manager.current_branch(lambda: False)
            status = git_manager.status_short(lambda: False)
            remote_url = git_manager.get_remote_url(lambda: False, self.settings.git_remote) or "not configured"
            busy = self.task_manager.is_repo_busy(repository_alias)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to read repo info")
            return

        text = (
            f"Repo: `{repository_alias}`\n"
            f"Path: {repo_path}\n"
            f"Remote name: {self.settings.git_remote}\n"
            f"Remote URL: {remote_url}\n"
            f"Branch: {branch}\n"
            f"Busy: {'yes' if busy else 'no'}\n"
            f"Status:\n{self._truncate(status, 2500)}"
        )
        await message.reply_text(text)

    async def use_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        alias = " ".join(context.args).strip()
        if not alias:
            await message.reply_text("Usage: /use <repo_alias>")
            return
        try:
            selected_alias, repo_path = self.settings.resolve_repository(alias)
        except ValueError as exc:
            await self._reply_error(message, exc, prefix="Unable to select repo")
            return
        self.task_manager.set_selected_repo(chat.id, selected_alias)
        await message.reply_text(f"Selected repo `{selected_alias}` -> {repo_path}")

    async def newrepo_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if not context.args:
            await message.reply_text("Usage: /newrepo <alias> [folder_name]")
            return

        alias = context.args[0]
        folder_name = context.args[1] if len(context.args) > 1 else alias
        try:
            registered_alias, repo_path, remote_message = self._create_repository(alias, folder_name)
            self.task_manager.set_selected_repo(chat.id, registered_alias)
        except (RuntimeError, ValueError, OSError, GitError) as exc:
            await self._reply_error(message, exc, prefix="Unable to create repo")
            return

        lines = [
            f"Created repo `{registered_alias}` at {repo_path}",
            f"Root: {self.settings.repo_root}",
            "This repo is now selected for the current chat.",
        ]
        if remote_message:
            lines.append(remote_message)
            lines.append(
                f"Create a GitHub repo named `{registered_alias}` under `{self.settings.github_owner}` and the next `/task` can push there."
            )
        else:
            lines.append(
                "GitHub remote was not configured automatically. Set `GITHUB_OWNER` in `.env` or use `/setremote` later."
            )
        await message.reply_text("\n".join(lines))

    async def setremote_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        if message is None:
            return
        if not context.args:
            await message.reply_text("Usage: /setremote <repo_alias> [github_repo_name_or_url]")
            return
        repository_alias = context.args[0]
        remote_input = context.args[1] if len(context.args) > 1 else None
        try:
            alias, repo_path, remote_message = self._configure_remote(repository_alias, remote_input)
        except (RuntimeError, ValueError, GitError) as exc:
            await self._reply_error(message, exc, prefix="Unable to set remote")
            return
        await message.reply_text(f"Repo: `{alias}`\nPath: {repo_path}\n{remote_message}")

    async def clone_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if len(context.args) < 2:
            await message.reply_text("Usage: /clone <alias> <git_url> [folder_name]")
            return

        alias = context.args[0]
        git_url = context.args[1]
        folder_name = context.args[2] if len(context.args) > 2 else None
        try:
            registered_alias, repo_path = self._clone_repository(alias, git_url, folder_name)
            self.task_manager.set_selected_repo(chat.id, registered_alias)
        except (RuntimeError, ValueError, OSError) as exc:
            await self._reply_error(message, exc, prefix="Unable to clone repo")
            return

        await message.reply_text(
            f"Cloned repo `{registered_alias}` to {repo_path}\n"
            f"Remote URL: {git_url}\n"
            "This repo is now selected for the current chat."
        )

    async def gitstatus_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        try:
            repository_alias, _, git_manager = self._resolve_repo_for_chat(chat.id)
            status = git_manager.status_short(lambda: False)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to read git status")
            return
        await message.reply_text(f"Repo: `{repository_alias}`\n{self._truncate(status, 3500)}")

    async def branches_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        try:
            repository_alias, _, git_manager = self._resolve_repo_for_chat(chat.id)
            branches = git_manager.list_branches(lambda: False)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to list branches")
            return
        await message.reply_text(f"Repo: `{repository_alias}`\n{self._truncate(branches, 3500)}")

    async def gitlog_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        try:
            limit = int(context.args[0]) if context.args else 5
        except ValueError:
            await message.reply_text("Usage: /gitlog [n]")
            return
        try:
            repository_alias, _, git_manager = self._resolve_repo_for_chat(chat.id)
            log_text = git_manager.recent_commits(lambda: False, limit=limit)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to read git log")
            return
        await message.reply_text(f"Repo: `{repository_alias}`\n{self._truncate(log_text, 3500)}")

    async def pull_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        branch_name = context.args[0].strip() if context.args else None
        try:
            repository_alias, _, git_manager = self._resolve_repo_for_chat(chat.id)
            self._ensure_repo_not_busy(repository_alias)
            git_manager.ensure_clean_worktree(lambda: False)
            fetch_output = git_manager.fetch(lambda: False)
            pull_output = git_manager.pull_current_branch(lambda: False, branch_name=branch_name)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to pull")
            return
        response = (
            f"Repo: `{repository_alias}`\n"
            f"Fetch:\n{self._truncate(fetch_output, 1400)}\n\n"
            f"Pull:\n{self._truncate(pull_output, 1400)}"
        )
        await message.reply_text(response)

    async def commit_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        commit_message = " ".join(context.args).strip()
        if not commit_message:
            await message.reply_text("Usage: /commit <message>")
            return
        try:
            repository_alias, _, git_manager = self._resolve_repo_for_chat(chat.id)
            self._ensure_repo_not_busy(repository_alias)
            commit_result = git_manager.commit_all(
                commit_message,
                should_stop=lambda: False,
                block_patterns=self.settings.safe_commit_block_patterns,
                content_markers=self.settings.safe_commit_content_markers,
                content_max_bytes=self.settings.safe_commit_content_max_bytes,
            )
            branch_name = git_manager.current_branch(lambda: False)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to commit")
            return
        blocked_text = self._format_blocked_commit_paths(commit_result.blocked_paths)
        if commit_result.commit_hash is None:
            response = f"Repo: {repository_alias}\nNo safe file changes to commit."
            if blocked_text:
                response = f"{response}\n{blocked_text}"
            await message.reply_text(response)
            return
        response = (
            f"Repo: {repository_alias}\n"
            f"Branch: {branch_name}\n"
            f"Commit: {commit_result.commit_hash}\n"
            f"Message: {commit_message}\n"
            f"Safe files included: {len(commit_result.staged_paths)}"
        )
        if blocked_text:
            response = f"{response}\n{blocked_text}"
        response = f"{response}\nUse /push if you also want to send this commit to remote."
        await message.reply_text(response)

    async def push_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        branch_name = context.args[0].strip() if context.args else None
        try:
            repository_alias, _, git_manager = self._resolve_repo_for_chat(chat.id)
            self._ensure_repo_not_busy(repository_alias)
            remote_url = git_manager.get_remote_url(lambda: False, self.settings.git_remote)
            if not remote_url:
                raise RuntimeError("Remote origin is not configured. Use /setremote first.")
            push_output = git_manager.push_current_branch(lambda: False, branch_name=branch_name)
            active_branch = branch_name or git_manager.current_branch(lambda: False)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to push")
            return
        await message.reply_text(
            f"Repo: `{repository_alias}`\n"
            f"Branch: {active_branch}\n"
            f"Remote: {remote_url}\n"
            f"Result: {push_output}"
        )

    async def ask_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        question = " ".join(context.args).strip()
        if not question:
            await message.reply_text("Usage: /ask <question>")
            return
        try:
            repository_alias, repo_path, _ = self._resolve_repo_for_chat(chat.id)
        except (GitError, RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to inspect repo")
            return

        await message.reply_text(f"Codex is reading repo `{repository_alias}` to answer your question...")
        try:
            answer = await asyncio.to_thread(self._ask_project_sync, repo_path, question)
        except (RuntimeError, ValueError) as exc:
            await self._reply_error(message, exc, prefix="Unable to answer project question")
            return
        await message.reply_text(f"Repo: `{repository_alias}`\n{self._truncate(answer, 3800)}")

    async def task_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        repo_override, prompt = self._parse_task_request(context.args)
        if not prompt:
            await message.reply_text("Usage: /task <prompt> or /task --repo <repo_alias> <prompt>")
            return

        requested_alias = repo_override or self._selected_repo_alias(chat.id)
        try:
            repository_alias, repo_path = self.settings.resolve_repository(requested_alias)
            task = self.task_manager.submit_task(chat.id, prompt, repository_alias=repository_alias)
        except ValueError as exc:
            await self._reply_error(message, exc, prefix="Unable to queue task")
            return
        except RuntimeError as exc:
            await self._reply_error(message, exc, prefix="Unable to queue task")
            return
        await message.reply_text(
            f"Queued task {task.task_id}.\n"
            f"Repo: `{repository_alias}`\n"
            f"Path: {repo_path}\n"
            f"Queue depth: {self.task_manager.queue_size()}\n"
            "Use /status for live progress."
        )

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        selected_repo = self._selected_repo_alias(chat.id)
        await message.reply_text(f"Selected repo: {selected_repo}\n{self.task_manager.status_report()}")

    async def diff_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        diff = self.task_manager.get_last_task_diff()
        if not diff:
            response = "No diff available yet."
        else:
            response = f"Last task diff:\n{self._truncate(diff, 3500)}"
        if update.effective_message:
            await update.effective_message.reply_text(response)

    async def stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_unauthorized(update):
            return
        stopped = self.task_manager.request_stop_current()
        text = "Stop signal sent to running task." if stopped else "No running task to stop."
        if update.effective_message:
            await update.effective_message.reply_text(text)

    async def _notification_loop(self, application: Application) -> None:
        while True:
            notifications = self.task_manager.pop_notifications(max_items=20)
            for chat_id, text in notifications:
                try:
                    await application.bot.send_message(chat_id=chat_id, text=text)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("Failed to send Telegram notification to chat_id=%s", chat_id)
            await asyncio.sleep(2.0)

    def run(self) -> None:
        self.worker.start()
        try:
            self.application.run_polling(drop_pending_updates=False)
        finally:
            self.worker.stop()
            self.worker.join(timeout=5.0)


if __name__ == "__main__":
    TelegramTaskBot().run()
















