"""
Repo location and a safe working directory, for every suite.

Two problems this solves.

**Hardcoded paths.** Every suite used to open paths under a hardcoded checkout
literally, which meant the tests only ran on the machine they were written on -
not on the VPS, not in a fresh clone, not from a different checkout. The repo
root is derived from this file's own location instead.

**Tests writing into the live logs.** Some suites exercise code that resolves
paths relative to the CURRENT DIRECTORY - `save_trades_log()` appends to
`logs/trade_history.csv`, the signal journal writes `logs/signal_journal.csv`.
Two suites used to `os.chdir` to the repo root before doing that, which is
harmless on a dev box with throwaway logs and destructive on the VPS, where
those exact files are the live trade history and the source for every report
and for ANALYSIS_LOG.md. `sandbox_cwd()` gives them a throwaway directory with
the same shape instead, so a relative write lands somewhere disposable.
"""

import atexit
import os
import pathlib
import shutil
import sys
import tempfile

REPO = str(pathlib.Path(__file__).resolve().parent.parent)
CONFIG = os.path.join(REPO, "config.yaml")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Importing src.main attaches a logging FileHandler on logs/trading.log, resolved
# against the CURRENT directory - so a suite run from anywhere without a logs/
# dir dies on import, before a single check runs. Create it wherever we are.
os.makedirs("logs", exist_ok=True)


def repo_file(*parts):
    """Absolute path to a file in the repo, for suites that read source text."""
    return os.path.join(REPO, *parts)


def sandbox_cwd():
    """
    chdir into a throwaway directory shaped like the repo.

    Call this INSTEAD of chdir-ing to the repo root. Returns the path; the
    directory is removed at interpreter exit.
    """
    d = tempfile.mkdtemp(prefix="market-bot-test-")
    os.makedirs(os.path.join(d, "logs", "reports"), exist_ok=True)
    try:
        shutil.copy2(CONFIG, os.path.join(d, "config.yaml"))
    except OSError:
        pass
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    os.chdir(d)
    os.makedirs("logs", exist_ok=True)
    return d
