from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

__test__ = False

from corpus.vendor import worker_runtime as runtime


def _worker_main() -> None:
    if sys.stdin.buffer.readline() != b"go\n":
        raise SystemExit(2)
    sys.stdin.close()
    mode, output = sys.argv[1], Path(sys.argv[2])
    if mode == "success":
        output.write_text(json.dumps({"sections": [{"locator": "document", "text": "ok", "heading": ""}],
                                      "metadata": {}, "warnings": []}), encoding="utf-8")
    elif mode == "descendant":
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        output.write_text(str(child.pid), encoding="ascii")
        time.sleep(60)
    elif mode == "sleep":
        time.sleep(60)


class WorkerRuntimeTests(unittest.TestCase):
    def test_successful_gated_child_and_fixed_policy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            output = root / "out.json"
            with patch.object(runtime, "WORKER_MODULE", "tests.test_worker_runtime"):
                result = runtime.run_extraction_worker(Path(sys.executable), ["success", str(output)],
                                                       Path(__file__).parents[1], root, 10)
            payload = runtime.read_extraction_output(output, 1024)
            self.assertEqual(payload["sections"][0]["text"], "ok")
            self.assertEqual(result.memory_limit_bytes, 1024 ** 3)
            self.assertEqual(result.policy_version, "parser-worker-policy-v1")
            self.assertEqual(result.containment_mode,
                             "job-object-v1" if os.name == "nt" else "process-bounds-v1")
            self.assertFalse(result.metadata()["filesystem_sandbox"])
            self.assertFalse(result.metadata()["network_sandbox"])

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Object behavior")
    def test_timeout_stops_worker_tree(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            pid_file = root / "pid.txt"
            with patch.object(runtime, "WORKER_MODULE", "tests.test_worker_runtime"):
                with self.assertRaises(runtime.WorkerRuntimeError) as caught:
                    runtime.run_extraction_worker(Path(sys.executable), ["descendant", str(pid_file)],
                                                  Path(__file__).parents[1], root, 0.5)
            self.assertEqual(caught.exception.code, "timeout")
            if pid_file.exists():
                pid = int(pid_file.read_text(encoding="ascii"))
                for _ in range(30):
                    if not _pid_exists(pid):
                        break
                    time.sleep(0.1)
                self.assertFalse(_pid_exists(pid))

    def test_missing_malformed_oversize_and_type_invalid_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            cases = [(root / "missing.json", "output_missing")]
            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            cases.append((malformed, "output_malformed"))
            invalid = root / "invalid.json"
            invalid.write_text('{"sections":"bad","metadata":{},"warnings":[]}', encoding="utf-8")
            cases.append((invalid, "output_invalid"))
            oversized = root / "oversized.json"
            with oversized.open("wb") as stream:
                stream.truncate(1025)
            cases.append((oversized, "output_oversize"))
            for path, code in cases:
                with self.subTest(code=code), self.assertRaises(runtime.WorkerRuntimeError) as caught:
                    runtime.read_extraction_output(path, 1024)
                self.assertEqual(caught.exception.code, code)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object behavior")
    def test_windows_attach_failure_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            with patch.object(runtime, "WORKER_MODULE", "tests.test_worker_runtime"), patch.object(
                    runtime, "_attach_windows_job", side_effect=runtime.WorkerRuntimeError("attach_failed")):
                with self.assertRaises(runtime.WorkerRuntimeError) as caught:
                    runtime.run_extraction_worker(Path(sys.executable), ["sleep", str(root / "unused")],
                                                  Path(__file__).parents[1], root, 5)
            self.assertEqual(caught.exception.code, "attach_failed")

    def test_launcher_has_no_module_or_shell_escape_hatch(self):
        parameters = inspect.signature(runtime.run_extraction_worker).parameters
        self.assertNotIn("module", parameters)
        self.assertNotIn("command", parameters)
        self.assertNotIn("shell", parameters)
        self.assertEqual(runtime.MEMORY_LIMIT_BYTES, 1024 ** 3)


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(["tasklist.exe", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True, timeout=5)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] in {"success", "descendant", "sleep"}:
    _worker_main()
elif __name__ == "__main__":
    unittest.main()
