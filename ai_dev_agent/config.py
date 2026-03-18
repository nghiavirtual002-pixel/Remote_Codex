from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SAFE_COMMIT_BLOCK_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "*.crt",
    "*.cer",
    "*.der",
    "*.jks",
    "*id_rsa*",
    "*id_ed25519*",
    "ai_dev_agent.log",
    "ai_dev_agent_state.json",
    "ai_dev_agent_repos.json",
    ".venv/*",
    "__pycache__/*",
    ".pytest_cache/*",
    ".mypy_cache/*",
    ".ruff_cache/*",
)

DEFAULT_SAFE_COMMIT_CONTENT_MARKERS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN=",
    "TELEGRAM_ALLOWED_CHAT_IDS=",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
)


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    repositories: dict[str, Path]
    env_repository_aliases: frozenset[str]
    default_repository_alias: str
    repo_root: Path
    repo_registry_file: Path
    git_remote: str = "origin"
    github_owner: str = ""
    github_remote_protocol: str = "https"
    state_file: Path = Path("ai_dev_agent_state.json")
    log_file: Path = Path("ai_dev_agent.log")
    queue_maxsize: int = 100
    poll_interval_seconds: float = 1.0
    codex_binary: str = "codex"
    task_branch_prefix: str = "ai-task"
    max_saved_output_lines: int = 200
    max_diff_chars: int = 3500
    allowed_chat_ids: tuple[int, ...] = ()
    auto_stash_when_dirty: bool = False
    auto_stash_include_untracked: bool = True
    auto_sync_main_after_task: bool = True
    auto_sync_main_branch: str = "main"
    safe_commit_block_patterns: tuple[str, ...] = DEFAULT_SAFE_COMMIT_BLOCK_PATTERNS
    safe_commit_content_markers: tuple[str, ...] = DEFAULT_SAFE_COMMIT_CONTENT_MARKERS
    safe_commit_content_max_bytes: int = 200_000

    @property
    def repository_path(self) -> Path:
        return self.repositories[self.default_repository_alias]

    def resolve_repository(self, alias: str | None = None) -> tuple[str, Path]:
        selected_alias = normalize_repository_alias(alias or self.default_repository_alias)
        repository = self.repositories.get(selected_alias)
        if repository is None:
            available = ", ".join(sorted(self.repositories))
            raise ValueError(f"Unknown repository alias: {alias}. Available aliases: {available}")
        return selected_alias, repository

    def register_repository(self, alias: str, path: Path) -> tuple[str, Path]:
        normalized_alias = normalize_repository_alias(alias)
        resolved_path = path.expanduser().resolve()
        existing = self.repositories.get(normalized_alias)
        if existing is not None and existing != resolved_path:
            raise ValueError(f"Repository alias already exists: {normalized_alias} -> {existing}")

        self.repositories[normalized_alias] = resolved_path
        if normalized_alias not in self.env_repository_aliases:
            self._persist_dynamic_repositories()
        return normalized_alias, resolved_path

    def build_github_remote_url(self, repository_name: str) -> str | None:
        owner = self.github_owner.strip()
        if not owner:
            return None
        repo_name = repository_name.strip()
        if not repo_name:
            raise ValueError("Repository name for GitHub remote cannot be empty")
        protocol = self.github_remote_protocol.strip().lower() or "https"
        if protocol == "ssh":
            return f"git@github.com:{owner}/{repo_name}.git"
        if protocol == "https":
            return f"https://github.com/{owner}/{repo_name}.git"
        raise ValueError("GITHUB_REMOTE_PROTOCOL must be either 'https' or 'ssh'")

    def _persist_dynamic_repositories(self) -> None:
        dynamic_repositories = {
            alias: str(path)
            for alias, path in sorted(self.repositories.items())
            if alias not in self.env_repository_aliases
        }
        self.repo_registry_file.parent.mkdir(parents=True, exist_ok=True)
        self.repo_registry_file.write_text(
            json.dumps(dynamic_repositories, indent=2),
            encoding="utf-8",
        )


def _parse_allowed_chat_ids(raw: str) -> tuple[int, ...]:
    if not raw.strip():
        return ()
    ids: list[int] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        ids.append(int(candidate))
    return tuple(ids)


def _parse_bool(raw: str, default: bool) -> bool:
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_pattern_list(raw: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if not raw.strip():
        return default
    values = [
        part.strip()
        for part in re.split(r"[,\n;]+", raw)
        if part.strip()
    ]
    return tuple(values) if values else default


def normalize_repository_alias(alias: str) -> str:
    normalized = alias.strip().lower()
    if not normalized:
        raise ValueError("Repository alias cannot be empty")
    if not re.fullmatch(r"[a-z0-9._-]+", normalized):
        raise ValueError(
            "Repository alias may only contain letters, numbers, dot, underscore, or dash"
        )
    return normalized


def _parse_repository_map(raw: str) -> dict[str, Path]:
    repositories: dict[str, Path] = {}
    for part in raw.split(";"):
        entry = part.strip()
        if not entry:
            continue
        alias, separator, path_text = entry.partition("=")
        if not separator:
            raise ValueError(
                "Invalid REPO_PATHS entry. Use the format alias=/path/to/repo;alias2=/path/to/other-repo"
            )
        normalized_alias = normalize_repository_alias(alias)
        path_value = path_text.strip()
        if not path_value:
            raise ValueError(f"Missing path for repository alias: {normalized_alias}")
        repositories[normalized_alias] = Path(path_value).expanduser().resolve()
    return repositories


def _load_repository_registry(registry_file: Path) -> dict[str, Path]:
    if not registry_file.exists():
        return {}
    raw = json.loads(registry_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Repository registry file must contain a JSON object")

    repositories: dict[str, Path] = {}
    for alias, path_value in raw.items():
        normalized_alias = normalize_repository_alias(str(alias))
        path_text = str(path_value).strip()
        if not path_text:
            raise ValueError(f"Repository registry contains empty path for alias: {normalized_alias}")
        repositories[normalized_alias] = Path(path_text).expanduser().resolve()
    return repositories


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    repo = os.getenv("REPO_PATH", "").strip()
    repo_map_raw = os.getenv("REPO_PATHS", "").strip()
    default_repo_alias_raw = os.getenv("DEFAULT_REPO", "").strip()
    repo_root_raw = os.getenv("REPO_ROOT", "").strip()
    repo_registry_file = Path(
        os.getenv("REPO_REGISTRY_FILE", "ai_dev_agent_repos.json")
    ).expanduser().resolve()
    if not token:
        raise ValueError("Missing required environment variable: TELEGRAM_BOT_TOKEN")

    repositories = _parse_repository_map(repo_map_raw) if repo_map_raw else {}
    if repo:
        repositories.setdefault("default", Path(repo).expanduser().resolve())
    env_repository_aliases = frozenset(repositories.keys())
    for alias, path in _load_repository_registry(repo_registry_file).items():
        repositories.setdefault(alias, path)
    if not repositories:
        raise ValueError("Missing required environment variable: REPO_PATH or REPO_PATHS")

    if repo_root_raw:
        repo_root = Path(repo_root_raw).expanduser().resolve()
    else:
        repo_root = next(iter(repositories.values())).parent

    if default_repo_alias_raw:
        default_repository_alias = normalize_repository_alias(default_repo_alias_raw)
        if default_repository_alias not in repositories:
            available = ", ".join(sorted(repositories))
            raise ValueError(
                f"DEFAULT_REPO points to unknown alias: {default_repo_alias_raw}. Available aliases: {available}"
            )
    elif "default" in repositories:
        default_repository_alias = "default"
    else:
        default_repository_alias = next(iter(repositories))

    return Settings(
        telegram_bot_token=token,
        repositories=repositories,
        env_repository_aliases=env_repository_aliases,
        default_repository_alias=default_repository_alias,
        repo_root=repo_root,
        repo_registry_file=repo_registry_file,
        git_remote=os.getenv("GIT_REMOTE", "origin").strip() or "origin",
        github_owner=os.getenv("GITHUB_OWNER", "").strip(),
        github_remote_protocol=os.getenv("GITHUB_REMOTE_PROTOCOL", "https").strip() or "https",
        state_file=Path(os.getenv("STATE_FILE", "ai_dev_agent_state.json")).expanduser().resolve(),
        log_file=Path(os.getenv("LOG_FILE", "ai_dev_agent.log")).expanduser().resolve(),
        queue_maxsize=int(os.getenv("QUEUE_MAXSIZE", "100")),
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "1.0")),
        codex_binary=os.getenv("CODEX_BINARY", "codex").strip() or "codex",
        task_branch_prefix=os.getenv("TASK_BRANCH_PREFIX", "ai-task").strip() or "ai-task",
        max_saved_output_lines=int(os.getenv("MAX_SAVED_OUTPUT_LINES", "200")),
        max_diff_chars=int(os.getenv("MAX_DIFF_CHARS", "3500")),
        allowed_chat_ids=_parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")),
        auto_stash_when_dirty=_parse_bool(os.getenv("AUTO_STASH_WHEN_DIRTY", ""), default=False),
        auto_stash_include_untracked=_parse_bool(os.getenv("AUTO_STASH_INCLUDE_UNTRACKED", ""), default=True),
        auto_sync_main_after_task=_parse_bool(os.getenv("AUTO_SYNC_MAIN_AFTER_TASK", ""), default=True),
        auto_sync_main_branch=os.getenv("AUTO_SYNC_MAIN_BRANCH", "main").strip() or "main",
        safe_commit_block_patterns=_parse_pattern_list(
            os.getenv("SAFE_COMMIT_BLOCK_PATTERNS", ""),
            DEFAULT_SAFE_COMMIT_BLOCK_PATTERNS,
        ),
        safe_commit_content_markers=_parse_pattern_list(
            os.getenv("SAFE_COMMIT_CONTENT_MARKERS", ""),
            DEFAULT_SAFE_COMMIT_CONTENT_MARKERS,
        ),
        safe_commit_content_max_bytes=int(os.getenv("SAFE_COMMIT_CONTENT_MAX_BYTES", "200000")),
    )


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
