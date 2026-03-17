from __future__ import annotations

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


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    output: str
    duration_seconds: float
    stopped: bool = False


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


def run_command(
    command: list[str],
    cwd: Path,
    should_stop: Callable[[], bool],
    on_output_line: Optional[Callable[[str], None]] = None,
    env: Optional[dict[str, str]] = None,
) -> CommandResult:
    start = time.time()
    output_queue: queue.Queue[str] = queue.Queue()
    output_lines: list[str] = []
    stopped = False

    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
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


def run_codex(
    codex_binary: str,
    prompt: str,
    cwd: Path,
    should_stop: Callable[[], bool],
    on_output_line: Optional[Callable[[str], None]] = None,
    sandbox_mode: str = "workspace-write",
) -> CommandResult:
    resolved = codex_binary
    if not Path(codex_binary).is_absolute():
        maybe = shutil.which(codex_binary)
        if maybe:
            resolved = maybe
    return run_command(
        command=[resolved, "exec", "--full-auto", "--sandbox", sandbox_mode, prompt],
        cwd=cwd,
        should_stop=should_stop,
        on_output_line=on_output_line,
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
