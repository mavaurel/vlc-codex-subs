from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time


class Cancelled(RuntimeError):
    pass


class RateLimited(RuntimeError):
    def __init__(self, retry_after=30):
        self.retry_after = retry_after
        super().__init__(f'Codex rate limit; retry after {retry_after:.0f}s.')


def run(argv: list[str], *, stdin: str | None = None, cwd: Path | None = None,
        timeout=600, cancel: Path | None = None, stderr_callback=None) -> str:
    """No shell; bounded execution; terminate the child process group on cancellation."""
    if cancel and cancel.exists():
        raise Cancelled("Cancelled.")
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, start_new_session=os.name != "nt") as proc:
        started, first, last_error = time.monotonic(), True, None
        try:
            while True:
                if cancel and cancel.exists():
                    raise Cancelled("Cancelled.")
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f"{Path(argv[0]).name} exceeded {timeout}s.")
                try:
                    out, err = proc.communicate(stdin if first else None, timeout=0.25)
                    break
                except subprocess.TimeoutExpired as exc:
                    first = False
                    if stderr_callback and exc.stderr != last_error:
                        last_error = exc.stderr
                        value = exc.stderr or b''
                        stderr_callback(value.decode('utf-8','replace') if isinstance(value,bytes) else value)
            if proc.returncode:
                errors = [line for line in err.splitlines() if line.startswith("ERROR:")]
                detail = "\n".join(errors[-2:]) if errors else err[-1600:]
                raise RuntimeError(f"{Path(argv[0]).name} failed ({proc.returncode}): {detail}")
            return out
        except BaseException:
            if proc.poll() is None:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait()
            raise
