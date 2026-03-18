from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    chat_id: int
    prompt: str
    repository_alias: str = "default"
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    branch_name: Optional[str] = None
    progress: str = "Queued"
    commit_hash: Optional[str] = None
    summary: Optional[str] = None
    error: Optional[str] = None
    last_diff: Optional[str] = None
    output_tail: list[str] = field(default_factory=list)
    stop_requested: bool = False
    codex_thread_id: Optional[str] = None
    pending_confirmation: Optional[str] = None
    resume_prompt: Optional[str] = None
    baseline_snapshot: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        return cls(**data)


@dataclass(slots=True)
class PendingCodexSession:
    session_id: str
    chat_id: int
    repository_alias: str
    kind: str
    prompt: str
    codex_thread_id: str
    pending_confirmation: str
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingCodexSession":
        return cls(**data)


class TaskManager:
    def __init__(self, state_file: Path, queue_maxsize: int = 100, max_saved_output_lines: int = 200) -> None:
        self.state_file = state_file
        self.max_saved_output_lines = max_saved_output_lines
        self._lock = threading.RLock()
        self._task_queue: queue.Queue[str] = queue.Queue(maxsize=queue_maxsize)
        self._notification_queue: queue.Queue[tuple[int, str]] = queue.Queue()
        self._tasks: dict[str, TaskRecord] = {}
        self._pending_sessions: dict[str, PendingCodexSession] = {}
        self._chat_repo_aliases: dict[str, str] = {}
        self._current_task_id: Optional[str] = None
        self._last_task_id: Optional[str] = None
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        for task_id, payload in raw.get("tasks", {}).items():
            task = TaskRecord.from_dict(payload)
            if task.status in {"running", "queued"}:
                task.status = "failed"
                task.finished_at = _utc_now()
                task.error = "Marked failed after service restart."
                task.progress = "Failed after restart"
            self._tasks[task_id] = task
        for session_id, payload in raw.get("pending_sessions", {}).items():
            self._pending_sessions[session_id] = PendingCodexSession.from_dict(payload)
        self._chat_repo_aliases = {
            str(chat_id): str(alias)
            for chat_id, alias in raw.get("chat_repo_aliases", {}).items()
            if str(alias).strip()
        }
        self._last_task_id = raw.get("last_task_id")

    def _persist(self) -> None:
        payload = {
            "tasks": {task_id: task.to_dict() for task_id, task in self._tasks.items()},
            "pending_sessions": {
                session_id: session.to_dict() for session_id, session in self._pending_sessions.items()
            },
            "chat_repo_aliases": self._chat_repo_aliases,
            "last_task_id": self._last_task_id,
        }
        tmp_path = self.state_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_file)

    def submit_task(self, chat_id: int, prompt: str, repository_alias: str) -> TaskRecord:
        task = TaskRecord(
            task_id=f"task-{int(time.time() * 1000)}",
            chat_id=chat_id,
            prompt=prompt.strip(),
            repository_alias=repository_alias,
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._persist()
            try:
                self._task_queue.put_nowait(task.task_id)
            except queue.Full as exc:
                del self._tasks[task.task_id]
                self._persist()
                raise RuntimeError("Task queue is full") from exc
        return task

    def reserve_next_task(self, timeout_seconds: float = 1.0) -> Optional[TaskRecord]:
        try:
            task_id = self._task_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.status = "running"
            if task.started_at is None:
                task.started_at = _utc_now()
            task.progress = f"Starting worker for repo {task.repository_alias}"
            self._current_task_id = task_id
            self._persist()
            return TaskRecord.from_dict(task.to_dict())

    def update_progress(self, task_id: str, progress: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.progress = progress
            self._persist()

    def append_output_line(self, task_id: str, line: str) -> None:
        clean = line.rstrip()
        if not clean:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.output_tail.append(clean)
            if len(task.output_tail) > self.max_saved_output_lines:
                task.output_tail = task.output_tail[-self.max_saved_output_lines :]
            self._persist()

    def set_branch(self, task_id: str, branch_name: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.branch_name = branch_name
            self._persist()

    def set_codex_thread(self, task_id: str, thread_id: str | None) -> None:
        if not thread_id:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.codex_thread_id = thread_id
            self._persist()

    def set_baseline_snapshot(self, task_id: str, snapshot: dict[str, str]) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.baseline_snapshot = dict(snapshot)
            self._persist()

    def mark_waiting_input(self, task_id: str, question: str, thread_id: str | None) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "waiting_input"
            task.progress = "Waiting for user confirmation"
            task.pending_confirmation = question
            task.resume_prompt = None
            if thread_id:
                task.codex_thread_id = thread_id
            self._current_task_id = None
            self._last_task_id = task_id
            self._persist()

    def queue_task_resume(self, task_id: str, resume_prompt: str) -> TaskRecord:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"Unknown task id: {task_id}")
            if task.status != "waiting_input" or not task.codex_thread_id:
                raise RuntimeError(f"Task {task_id} is not waiting for confirmation")
            task.status = "queued"
            task.progress = "Resume queued"
            task.resume_prompt = resume_prompt.strip()
            task.pending_confirmation = None
            task.stop_requested = False
            self._persist()
            try:
                self._task_queue.put_nowait(task_id)
            except queue.Full as exc:
                task.status = "waiting_input"
                task.progress = "Waiting for user confirmation"
                task.pending_confirmation = "Queue is full while trying to resume task."
                task.resume_prompt = None
                self._persist()
                raise RuntimeError("Task queue is full") from exc
            return TaskRecord.from_dict(task.to_dict())

    def mark_completed(self, task_id: str, summary: str, commit_hash: Optional[str]) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "completed"
            task.finished_at = _utc_now()
            task.progress = "Completed"
            task.summary = summary
            task.commit_hash = commit_hash
            task.error = None
            task.pending_confirmation = None
            task.resume_prompt = None
            self._current_task_id = None
            self._last_task_id = task_id
            self._persist()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "failed"
            task.finished_at = _utc_now()
            task.progress = "Failed"
            task.error = error
            task.pending_confirmation = None
            task.resume_prompt = None
            self._current_task_id = None
            self._last_task_id = task_id
            self._persist()

    def mark_stopped(self, task_id: str, reason: str = "Stopped by user") -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "stopped"
            task.finished_at = _utc_now()
            task.progress = "Stopped"
            task.error = reason
            task.pending_confirmation = None
            task.resume_prompt = None
            self._current_task_id = None
            self._last_task_id = task_id
            self._persist()

    def request_stop_current(self) -> bool:
        with self._lock:
            if not self._current_task_id:
                return False
            task = self._tasks.get(self._current_task_id)
            if task is None:
                return False
            task.stop_requested = True
            task.progress = "Stop requested"
            self._persist()
            return True

    def should_stop(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            return bool(task and task.stop_requested)

    def queue_size(self) -> int:
        return self._task_queue.qsize()

    def get_selected_repo(self, chat_id: int, default_alias: str) -> str:
        with self._lock:
            return self._chat_repo_aliases.get(str(chat_id), default_alias)

    def set_selected_repo(self, chat_id: int, repository_alias: str) -> None:
        with self._lock:
            self._chat_repo_aliases[str(chat_id)] = repository_alias
            self._persist()

    def get_current_task(self) -> Optional[TaskRecord]:
        with self._lock:
            if not self._current_task_id:
                return None
            task = self._tasks.get(self._current_task_id)
            if task is None:
                return None
            return TaskRecord.from_dict(task.to_dict())

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return TaskRecord.from_dict(task.to_dict())

    def is_repo_busy(self, repository_alias: str) -> bool:
        with self._lock:
            for task in self._tasks.values():
                if task.repository_alias != repository_alias:
                    continue
                if task.status in {"running", "waiting_input"}:
                    return True
            return False

    def create_pending_session(
        self,
        chat_id: int,
        repository_alias: str,
        kind: str,
        prompt: str,
        codex_thread_id: str,
        pending_confirmation: str,
    ) -> PendingCodexSession:
        session = PendingCodexSession(
            session_id=f"{kind}-{int(time.time() * 1000)}",
            chat_id=chat_id,
            repository_alias=repository_alias,
            kind=kind,
            prompt=prompt.strip(),
            codex_thread_id=codex_thread_id,
            pending_confirmation=pending_confirmation,
        )
        with self._lock:
            self._pending_sessions[session.session_id] = session
            self._persist()
        return session

    def get_pending_session(self, session_id: str) -> Optional[PendingCodexSession]:
        with self._lock:
            session = self._pending_sessions.get(session_id)
            if session is None:
                return None
            return PendingCodexSession.from_dict(session.to_dict())

    def consume_pending_session(self, session_id: str) -> Optional[PendingCodexSession]:
        with self._lock:
            session = self._pending_sessions.pop(session_id, None)
            self._persist()
            if session is None:
                return None
            return session

    def status_report(self) -> str:
        with self._lock:
            if self._current_task_id:
                task = self._tasks[self._current_task_id]
                tail = task.output_tail[-5:]
                lines = [
                    f"Task: {task.task_id}",
                    f"Repo: {task.repository_alias}",
                    f"Status: {task.status}",
                    f"Branch: {task.branch_name or 'N/A'}",
                    f"Progress: {task.progress}",
                    f"Queued: {self._task_queue.qsize()}",
                ]
                if tail:
                    lines.append("Recent output:")
                    lines.extend(f"- {line}" for line in tail)
                return "\n".join(lines)

            if self._last_task_id and self._last_task_id in self._tasks:
                task = self._tasks[self._last_task_id]
                return (
                    "No task running.\n"
                    f"Last task: {task.task_id}\n"
                    f"Repo: {task.repository_alias}\n"
                    f"Status: {task.status}\n"
                    f"Summary: {task.summary or task.pending_confirmation or task.error or 'N/A'}\n"
                    f"Queued: {self._task_queue.qsize()}"
                )
            return f"No task running.\nQueued: {self._task_queue.qsize()}"

    def get_last_task_diff(self) -> Optional[str]:
        with self._lock:
            if not self._last_task_id:
                return None
            task = self._tasks.get(self._last_task_id)
            if not task:
                return None
            return task.last_diff

    def set_last_task_diff(self, task_id: str, diff_text: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.last_diff = diff_text
            self._persist()

    def enqueue_notification(self, chat_id: int, message: str) -> None:
        self._notification_queue.put((chat_id, message))

    def pop_notifications(self, max_items: int = 20) -> list[tuple[int, str]]:
        items: list[tuple[int, str]] = []
        for _ in range(max_items):
            try:
                items.append(self._notification_queue.get_nowait())
            except queue.Empty:
                break
        return items


