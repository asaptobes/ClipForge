"""Launcher used by PyInstaller and for running from a source checkout."""

import multiprocessing
import sys

from clipforge.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
