"""Tests for the trickler.

The trickler modules use flat imports (`import helpers`) because they run from the
`trickler/` directory, so put that directory on the path before anything is imported.

Run them from the repository root with:

    python -m unittest discover -t . -s tests

The top-level directory has to be the repository root so that this package is imported
before the test modules, since it is what puts `trickler/` on the path.
"""
import logging
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, 'opentrickler_config.ini')

if os.path.join(ROOT, 'trickler') not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, 'trickler'))


def quiet_logging():
    """Silences the daemons' own logging so test results stay readable.

    Raises the bar rather than calling logging.disable(), so assertLogs() still works.
    Call it again after importing anything that runs logging.basicConfig() at import
    time, which resets the root level -- app.py does.
    """
    logging.getLogger().setLevel(logging.CRITICAL)


quiet_logging()
