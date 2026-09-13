"""Reentrant thread/process locks for files shared by the API, MQTT and CLI."""

import os
import threading
from contextlib import contextmanager
from functools import wraps

import config

_registry_lock = threading.Lock()
_mutexes = {}
_held = threading.local()


@contextmanager
def file_lock(path):
    # flock alone does not serialize threads sharing a descriptor. Keep the
    # descriptor open for the outermost call and never unlink its lock file.
    path = os.path.abspath(path)
    with _registry_lock:
        mutex = _mutexes.setdefault(path, threading.RLock())
    with mutex:
        held = getattr(_held, "paths", {})
        if path in held:
            yield
            return
        with open(path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            held[path] = handle
            _held.paths = held
            try:
                yield
            finally:
                del held[path]
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def pump_locked(function):
    """Serialize a pump decision and its PWM/state writes across processes."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with file_lock(config.STATE_FILE + ".pump.lock"):
            return function(*args, **kwargs)

    return wrapped
