"""Background-work plumbing: cancellation tokens and a QThreadPool task wrapper.

Rules every background job follows:

* never touch widgets from the worker thread - communicate through signals only
  (Qt queues them onto the GUI thread);
* poll ``cancel.cancelled`` / call ``cancel.raise_if_cancelled()`` regularly;
* exceptions never escape: they are logged and delivered through ``failed``.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class Cancelled(Exception):
    """Raised inside a worker when its job was cancelled."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; returns True if cancelled meanwhile."""
        return self._event.wait(timeout)


class TaskSignals(QObject):
    progress = Signal(float, str)   # fraction 0..1 (or -1 when unknown), status text
    result = Signal(object)
    failed = Signal(str)            # user-presentable message
    cancelled = Signal()
    finished = Signal()             # always emitted last


class Task(QRunnable):
    """Run ``fn(cancel, progress)`` on the global thread pool.

    ``fn`` receives the :class:`CancelToken` and a ``progress(fraction, text)``
    callable. Hold a reference to the Task (or its ``signals``) while it runs.
    """

    def __init__(self, fn: Callable[[CancelToken, Callable[[float, str], None]], Any],
                 name: str = "task") -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.name = name
        self.cancel_token = CancelToken()
        self.signals = TaskSignals()

    def cancel(self) -> None:
        self.cancel_token.cancel()

    def start(self, pool: Optional[QThreadPool] = None) -> "Task":
        (pool or QThreadPool.globalInstance()).start(self)
        return self

    def run(self) -> None:  # worker thread
        # Keep our own reference: a slot connected to ``finished`` may drop the last
        # outside reference to this Task while we are still inside ``run``.
        signals = self.signals
        try:
            value = self.fn(self.cancel_token, self._progress)
            if self.cancel_token.cancelled:
                signals.cancelled.emit()
            else:
                signals.result.emit(value)
        except Cancelled:
            signals.cancelled.emit()
        except RuntimeError as exc:
            if "deleted" not in str(exc):          # signal target gone at shutdown: ignore
                log.error("Background task %r failed: %s", self.name, exc, exc_info=True)
                signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - last line of defence for the GUI
            log.error("Background task %r failed: %s\n%s", self.name, exc, traceback.format_exc())
            signals.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            try:
                signals.finished.emit()
            except RuntimeError:
                pass

    def _progress(self, fraction: float, text: str = "") -> None:
        self.signals.progress.emit(float(fraction), text)
