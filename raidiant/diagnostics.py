"""Bounded runtime checks for helpers launched by a frozen executable."""

from contextlib import contextmanager
import multiprocessing
from multiprocessing import shared_memory
import os
import signal
import time


@contextmanager
def isolated_state(path):
    """Keep diagnostic recovery records out of the user's application state."""
    previous = os.environ.get("RAIDIANT_STATE_DIR")
    os.environ["RAIDIANT_STATE_DIR"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RAIDIANT_STATE_DIR", None)
        else:
            os.environ["RAIDIANT_STATE_DIR"] = previous


def _wait_tracker(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed, status = os.waitpid(pid, os.WNOHANG)
        if completed:
            return os.waitstatus_to_exitcode(status)
        time.sleep(.01)
    raise TimeoutError("Resource-tracker helper did not finish")


def _check_posix_tracker(timeout):
    # A separate tracker lets us verify its real exit status without stopping
    # a tracker already used by the application or its test runner. This uses
    # CPython's private helper only in the opt-in diagnostic.
    from multiprocessing.resource_tracker import ResourceTracker

    tracker = ResourceTracker()
    pid = None
    reaped = False
    try:
        tracker.ensure_running()
        pid = tracker._pid
        # Write directly: register()/unregister() may silently restart a dead
        # helper, hiding the very frozen-entry-point failure being checked.
        message = b"REGISTER:raidiant-diagnostic:noop\nUNREGISTER:raidiant-diagnostic:noop\n"
        if os.write(tracker._fd, message) != len(message):
            raise RuntimeError("Incomplete resource-tracker diagnostic request")
        os.close(tracker._fd)
        tracker._fd = None
        try:
            exitcode = _wait_tracker(pid, timeout)
        except ChildProcessError as exc:
            # Another waiter consumed the status. Do not risk signaling a PID
            # that is no longer our child; a missing status cannot pass.
            reaped = True
            raise RuntimeError("Resource-tracker helper exit status is unavailable") from exc
        reaped = True
        if exitcode != 0:
            raise RuntimeError(f"Resource-tracker helper failed (exit {exitcode})")
    finally:
        if tracker._fd is not None:
            os.close(tracker._fd)
            tracker._fd = None
        if pid is not None and not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                _wait_tracker(pid, timeout)
            except ChildProcessError:
                pass  # Already reaped during failed-helper cleanup.
        tracker._pid = None


def _shared_memory_worker(name, length, connection):
    """Importable spawn target: exercise the bundled child and shared memory."""
    memory = None
    try:
        memory = shared_memory.SharedMemory(name=name)
        # Windows rounds a reopened mapping up to an allocation boundary.
        payload = bytes(memory.buf[:length])
        memory.buf[:length] = payload[::-1]
        connection.send(payload)
    finally:
        if memory is not None:
            memory.close()
        connection.close()


def check_multiprocessing(timeout=15):
    """Raise on failed helpers, a timeout, or a wrong shared-resource result."""
    completed = []
    if os.name == "posix":
        _check_posix_tracker(timeout)
        completed.append("POSIX resource-tracker shutdown")

    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    payload = b"RAIDiant shared-memory diagnostic"
    memory = None
    process = None
    started = False
    try:
        memory = shared_memory.SharedMemory(create=True, size=len(payload))
        memory.buf[:] = payload
        process = context.Process(target=_shared_memory_worker, args=(memory.name, len(payload), sending))
        process.start()
        started = True
        sending.close()
        deadline = time.monotonic() + timeout
        if not receiving.poll(timeout):
            raise TimeoutError("Spawned diagnostic worker did not respond")
        if receiving.recv() != payload:
            raise RuntimeError("Spawned diagnostic worker read incorrect shared data")
        process.join(max(0, deadline - time.monotonic()))
        if process.is_alive():
            raise TimeoutError("Spawned diagnostic worker did not finish")
        if process.exitcode != 0:
            raise RuntimeError(f"Spawned diagnostic worker failed (exit {process.exitcode})")
        if bytes(memory.buf) != payload[::-1]:
            raise RuntimeError("Spawned diagnostic worker did not update shared data")
    finally:
        receiving.close()
        sending.close()
        if process is not None:
            if started and process.is_alive():
                process.kill()
                process.join(timeout)
            if not started or not process.is_alive():
                process.close()
        if memory is not None:
            memory.close()
            memory.unlink()
    completed.append("spawned worker/shared memory")
    return completed
