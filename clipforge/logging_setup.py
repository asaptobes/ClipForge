"""Rotating log file plus a global excepthook so nothing fails silently."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths

_configured = False


def log_file() -> Path:
    return paths.log_dir() / "clipforge.log"


def setup_logging(debug: bool = False) -> Path:
    global _configured
    path = log_file()
    if _configured:
        return path
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    handler.setFormatter(fmt)
    root.addHandler(handler)
    if sys.stderr is not None and not paths.is_frozen():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("clipforge.crash").critical("Unhandled exception",
                                                      exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        logging.getLogger("clipforge.crash").critical(
            "Unhandled exception in thread %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    _configured = True
    logging.getLogger(__name__).info("Logging started (%s)", path)
    return path
