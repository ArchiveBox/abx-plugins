"""SingleFile helper supervision and best-effort gallery timing."""

import os
import subprocess
import sys
import time
from threading import Thread

# Gallery already opts into bounded failure artifacts with this variable. Use
# it only as a switch; hooks must never access that directory.
TIMELINE_ENABLED = sys.platform == "linux" and os.path.isabs(
    os.environ.get("SCREENSHOT_FAILURE_DIR", ""),
)


def emit_timeline(event: str, monotonic_ns: int) -> None:
    # Sample now, not when stderr is writable. Diagnostics are best-effort:
    # never let an unread pipe or filesystem hold capture or helper cleanup.
    record = f"[timeline] event={event} monotonic_ns={monotonic_ns}\n".encode()

    def write_record() -> None:
        try:
            os.write(2, record)
        except OSError:
            pass

    try:
        Thread(target=write_record, name="singlefile-timeline", daemon=True).start()
    except (OSError, RuntimeError):
        pass


def wait_for_singlefile_helper(
    helper: subprocess.Popen,
    reader: Thread,
    deadline: float,
    timeout: int,
) -> None:
    try:
        helper.wait(timeout=max(0, deadline - time.monotonic()))
        reader.join(timeout=max(0, deadline - time.monotonic()))
        if reader.is_alive():
            raise subprocess.TimeoutExpired(helper.args, timeout)
    except BaseException as exc:
        timed_out_at = (
            time.monotonic_ns()
            if TIMELINE_ENABLED and isinstance(exc, subprocess.TimeoutExpired)
            else None
        )
        helper.kill()
        helper.wait()
        if timed_out_at is not None:
            emit_timeline("singlefile_wrapper_timeout", timed_out_at)
        raise
