"""OS-held, crash-released run ownership without PID liveness signals."""

import os
from pathlib import Path
from typing import BinaryIO

from unified_web_parser.core.exceptions import PipelineError


class RunLockedError(PipelineError):
    """Another process owns this run's writable state."""


class RunLease:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir).resolve()
        self._stream: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._stream is not None

    def acquire(self) -> "RunLease":
        if self.held:
            return self
        self.run_dir.mkdir(parents=True, exist_ok=True)
        stream = (self.run_dir / "run.lock").open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RunLockedError("Run is already active in another process") from exc
        self._stream = stream
        return self

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> "RunLease":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def is_run_active(run_dir: Path) -> bool:
    if not (Path(run_dir) / "run.lock").is_file():
        return False
    lease = RunLease(run_dir)
    try:
        lease.acquire()
    except RunLockedError:
        return True
    else:
        lease.release()
        return False
