"""Entry point for the packaged build.

PyInstaller runs its script as a top-level module, where the relative import
in ppfarmer/__main__.py has no parent package to resolve against. This file
imports by absolute name instead.
"""

import multiprocessing
import sys

from ppfarmer.cli import main

if __name__ == "__main__":
    # Harmless from source, required once frozen: without it a child process
    # would re-run the whole executable.
    multiprocessing.freeze_support()
    sys.exit(main())
