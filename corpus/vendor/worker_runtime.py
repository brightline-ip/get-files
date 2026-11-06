"""Bound extraction worker launcher; resource containment is not a filesystem/network sandbox."""
from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

POLICY_VERSION = "parser-worker-policy-v1"
MEMORY_LIMIT_BYTES = 1024 ** 3
WORKER_MODULE = "corpus.extraction_worker"
MAX_COUNTED_WAIT_SECONDS = 5.0
_GATE_WRAPPER = (
    "import os,runpy,sys;"
    "gate=sys.stdin.buffer.readline();"
    "sys.stdin.close();"
    "gate==b'go\\n' or sys.exit(2);"
    "os.name!='posix' or __import__('resource').setrlimit(__import__('resource').RLIMIT_AS,(1073741824,1073741824));"
    "module=sys.argv[1];sys.argv=[module,*sys.argv[2:]];"
    "runpy.run_module(module,run_name='__main__')"
)


class WorkerRuntimeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WorkerResult:
    containment_mode: str
    policy_version: str = POLICY_VERSION
    memory_limit_bytes: int = MEMORY_LIMIT_BYTES

    def metadata(self) -> dict:
        return {
            "containment_mode": self.containment_mode,
            "worker_policy_version": self.policy_version,
            "worker_memory_limit_bytes": self.memory_limit_bytes,
            "filesystem_sandbox": False,
            "network_sandbox": False,
        }


def _environment(temp_dir: Path) -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "LANG"}
    value = {key: item for key, item in os.environ.items() if key.upper() in allowed}
    value.update({"TEMP": str(temp_dir), "TMP": str(temp_dir), "PYTHONUTF8": "1"})
    return value


def _attach_windows_job(process):
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                    ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                    ("scheduling", w.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                    ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100 | 0x200 | 0x2000
    limits.process_memory = MEMORY_LIMIT_BYTES
    limits.job_memory = MEMORY_LIMIT_BYTES
    if (not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(handle, int(process._handle))):
        if handle:
            kernel.CloseHandle(handle)
        raise WorkerRuntimeError("attach_failed")
    closed = False

    def close():
        nonlocal closed
        if not closed:
            kernel.CloseHandle(handle)
            closed = True

    return close


def _terminate_tree(process, close_job) -> None:
    if process.poll() is None:
        if os.name == "nt":
            process.kill()
            close_job()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_while_awake(process, timeout: float) -> None:
    """Wait up to timeout seconds of running time. Sleep or standby freezes this loop, and a
    long gap between checks counts as one short interval, so a closed laptop can't use up the limit."""
    remaining = timeout
    last = time.monotonic()
    while True:
        try:
            process.wait(timeout=min(1.0, max(remaining, 0.01)))
            return
        except subprocess.TimeoutExpired:
            current = time.monotonic()
            remaining -= min(current - last, MAX_COUNTED_WAIT_SECONDS)
            last = current
            if remaining <= 0:
                raise


def run_extraction_worker(python_executable: Path, arguments: list[str], cwd: Path,
                          temp_dir: Path, timeout: float) -> WorkerResult:
    executable = Path(python_executable).resolve(strict=True)
    cwd = Path(cwd).resolve(strict=True)
    temp_dir = Path(temp_dir).resolve(strict=True)
    if not isinstance(arguments, list) or not arguments or any(not isinstance(value, str) for value in arguments):
        raise ValueError("Worker arguments must be a nonempty string list")
    command = [str(executable), "-m", WORKER_MODULE, *arguments]
    process = subprocess.Popen(command, cwd=cwd, env=_environment(temp_dir), stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                               close_fds=True,
                               start_new_session=os.name != "nt",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    close_job = lambda: None
    mode = "process-bounds-v1"
    try:
        if os.name == "nt":
            try:
                close_job = _attach_windows_job(process)
            except WorkerRuntimeError:
                _terminate_tree(process, close_job)
                raise
            mode = "job-object-v1"
        assert process.stdin is not None
        process.stdin.write(b"go\n")
        process.stdin.flush()
        process.stdin.close()
        try:
            _wait_while_awake(process, timeout)
        except subprocess.TimeoutExpired:
            _terminate_tree(process, close_job)
            raise WorkerRuntimeError("timeout") from None
        if process.returncode:
            raise WorkerRuntimeError("worker_failed")
        return WorkerResult(mode)
    finally:
        if process.poll() is None:
            _terminate_tree(process, close_job)
        close_job()


def read_extraction_output(path: Path, maximum_bytes: int) -> dict:
    path = Path(path)
    if not path.is_file():
        raise WorkerRuntimeError("output_missing")
    size = path.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise WorkerRuntimeError("output_oversize")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerRuntimeError("output_malformed") from exc
    if not isinstance(value, dict) or set(value) != {"sections", "metadata", "warnings"}:
        raise WorkerRuntimeError("output_invalid")
    sections, metadata, warnings = value["sections"], value["metadata"], value["warnings"]
    if (not isinstance(sections, list) or len(sections) > 10000 or not isinstance(metadata, dict)
            or not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings)):
        raise WorkerRuntimeError("output_invalid")
    total = 0
    for section in sections:
        if (not isinstance(section, dict) or set(section) != {"locator", "text", "heading"}
                or any(not isinstance(section[key], str) for key in ("locator", "text", "heading"))):
            raise WorkerRuntimeError("output_invalid")
        total += len(section["text"]) + len(section["heading"])
        if total > 8_000_000:
            raise WorkerRuntimeError("output_oversize")
    return value
