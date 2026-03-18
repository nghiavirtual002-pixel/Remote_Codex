from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CONFIRMATION_MARKER = "TELEBOT_CONFIRM_REQUIRED:"


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    output: str
    duration_seconds: float
    stopped: bool = False


@dataclass(slots=True)
class CodexRunResult(CommandResult):
    thread_id: str | None = None
    last_agent_message: str = ""
    confirmation_request: str | None = None


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    time.sleep(2)
    if proc.poll() is None:
        proc.terminate()
    time.sleep(2)
    if proc.poll() is None:
        proc.kill()


def _vscode_extension_roots() -> list[Path]:
    home = Path.home()
    roots = [
        home / ".vscode" / "extensions",
        home / ".vscode-insiders" / "extensions",
    ]
    return [root for root in roots if root.exists()]


def _candidate_codex_names() -> tuple[str, ...]:
    return ("codex.exe",) if os.name == "nt" else ("codex",)


def _discover_vscode_codex_binary() -> str | None:
    candidates: list[Path] = []
    binary_names = set(_candidate_codex_names())
    for root in _vscode_extension_roots():
        for extension_dir in root.glob("openai.chatgpt-*"):
            if not extension_dir.is_dir():
                continue
            bin_dir = extension_dir / "bin"
            if not bin_dir.exists():
                continue
            for candidate in bin_dir.rglob("*"):
                if candidate.is_file() and candidate.name in binary_names:
                    candidates.append(candidate)
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return str(newest)


def resolve_codex_binary(codex_binary: str) -> str:
    requested = (codex_binary or "codex").strip() or "codex"
    requested_path = Path(requested).expanduser()
    if requested_path.is_absolute() and requested_path.exists():
        return str(requested_path)

    maybe = shutil.which(requested)
    if maybe:
        return maybe

    discovered = _discover_vscode_codex_binary()
    if discovered:
        return discovered

    return requested


def run_command(
    command: list[str],
    cwd: Path,
    should_stop: Callable[[], bool],
    on_output_line: Optional[Callable[[str], None]] = None,
    env: Optional[dict[str, str]] = None,
    stdin_text: str | None = None,
) -> CommandResult:
    start = time.time()
    output_queue: queue.Queue[str] = queue.Queue()
    output_lines: list[str] = []
    stopped = False

    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["preexec_fn"] = os.setsid

    try:
        proc = subprocess.Popen(command, **popen_kwargs)  # type: ignore[arg-type]
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Executable not found: {command[0]}. "
            "Install it or set an absolute path in configuration."
        ) from exc

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except Exception:
            pass

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output_queue.put(line)
        proc.stdout.close()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    while True:
        try:
            line = output_queue.get(timeout=0.3)
            output_lines.append(line)
            if on_output_line:
                on_output_line(line)
        except queue.Empty:
            pass

        if should_stop():
            stopped = True
            _terminate_process(proc)

        if proc.poll() is not None:
            break

    while not output_queue.empty():
        output_lines.append(output_queue.get_nowait())

    reader_thread.join(timeout=1.0)
    duration = time.time() - start
    return CommandResult(
        command=command,
        returncode=proc.returncode if proc.returncode is not None else 1,
        output="".join(output_lines),
        duration_seconds=duration,
        stopped=stopped,
    )


def _extract_text_from_json_event(event: dict) -> list[str]:
    texts: list[str] = []
    event_type = str(event.get("type", ""))
    if event_type == "item.completed":
        item = event.get("item") or {}
        item_type = str(item.get("type", ""))
        if item_type == "agent_message":
            text = str(item.get("text", "")).strip()
            if text:
                texts.append(text)
        elif item_type == "command_execution":
            command = str(item.get("command", "")).strip()
            output = str(item.get("aggregated_output", "")).strip()
            if command:
                texts.append(f"$ {command}")
            if output:
                texts.extend(line for line in output.splitlines() if line.strip())
    elif event_type == "event_msg":
        payload = event.get("payload") or {}
        if payload.get("type") == "agent_message":
            message = str(payload.get("message", "")).strip()
            if message:
                texts.append(message)
    return texts


def _parse_codex_json_output(raw_output: str) -> tuple[str | None, str, str | None, list[str]]:
    thread_id: str | None = None
    last_agent_message = ""
    confirmation_request: str | None = None
    display_lines: list[str] = []

    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            display_lines.append(raw_line.rstrip())
            continue

        if event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id") or thread_id or "") or thread_id

        for text in _extract_text_from_json_event(event):
            display_lines.append(text)
            last_agent_message = text
            for candidate in text.splitlines():
                clean = candidate.strip()
                if clean.startswith(CONFIRMATION_MARKER):
                    confirmation_request = clean[len(CONFIRMATION_MARKER) :].strip() or "Please confirm the requested action."

    return thread_id, last_agent_message, confirmation_request, display_lines


def _build_confirmation_prompt(user_prompt: str, read_only: bool) -> str:
    action_scope = "read-only inspection" if read_only else "code changes, commands, or external actions"
    return (
        "Telegram bridge rule: if you need any user confirmation before continuing, do not ask interactively and do not wait forever. "
        f"Before attempting {action_scope} that needs explicit approval, reply with exactly one line starting with "
        f"{CONFIRMATION_MARKER} followed by a short Vietnamese question, then stop. "
        "Do not perform the action until the user replies in a later resume message.\n\n"
        f"User request: {user_prompt.strip()}"
    )


def run_codex_session(
    codex_binary: str,
    prompt: str,
    cwd: Path,
    should_stop: Callable[[], bool],
    on_output_line: Optional[Callable[[str], None]] = None,
    sandbox_mode: str = "workspace-write",
    session_id: str | None = None,
    read_only: bool = False,
) -> CodexRunResult:
    resolved = resolve_codex_binary(codex_binary)
    effective_prompt = _build_confirmation_prompt(prompt, read_only=read_only)
    if session_id:
        if read_only:
            command = [resolved, "exec", "resume", "--json", session_id, effective_prompt]
        else:
            command = [resolved, "exec", "resume", "--json", "--full-auto", session_id, effective_prompt]
    else:
        command = [resolved, "exec", "--json", "--full-auto", "--sandbox", sandbox_mode, effective_prompt]

    raw_result = run_command(
        command=command,
        cwd=cwd,
        should_stop=should_stop,
        on_output_line=None,
    )
    thread_id, last_agent_message, confirmation_request, display_lines = _parse_codex_json_output(raw_result.output)
    if on_output_line:
        for line in display_lines:
            on_output_line(line)
    return CodexRunResult(
        command=raw_result.command,
        returncode=raw_result.returncode,
        output=raw_result.output,
        duration_seconds=raw_result.duration_seconds,
        stopped=raw_result.stopped,
        thread_id=thread_id,
        last_agent_message=last_agent_message,
        confirmation_request=confirmation_request,
    )


def run_codex(
    codex_binary: str,
    prompt: str,
    cwd: Path,
    should_stop: Callable[[], bool],
    on_output_line: Optional[Callable[[str], None]] = None,
    sandbox_mode: str = "workspace-write",
) -> CommandResult:
    return run_codex_session(
        codex_binary=codex_binary,
        prompt=prompt,
        cwd=cwd,
        should_stop=should_stop,
        on_output_line=on_output_line,
        sandbox_mode=sandbox_mode,
        read_only=sandbox_mode == "read-only",
    )


def discover_test_commands(repo_path: Path) -> list[list[str]]:
    candidates: list[list[str]] = []

    has_py = any((repo_path / name).exists() for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")) or (
        repo_path / "tests"
    ).exists()
    if has_py and shutil.which("pytest"):
        candidates.append(["pytest"])

    if (repo_path / "package.json").exists() and shutil.which("npm"):
        candidates.append(["npm", "test"])

    if (repo_path / "go.mod").exists() and shutil.which("go"):
        candidates.append(["go", "test", "./..."])

    return candidates

