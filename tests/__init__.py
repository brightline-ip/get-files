"""Test package. Run from the repository root:  python -m unittest discover -s tests -t .

All scratch data lives under the system temp directory and is removed at exit;
nothing is written into the source tree.
"""
import atexit
import shutil
import tempfile
from pathlib import Path

_BASE = Path(tempfile.mkdtemp(prefix="gf-"))
TEST_PARENT = _BASE  # kept short: Windows MAX_PATH applies to nested staging paths
atexit.register(shutil.rmtree, _BASE, ignore_errors=True)


def scratch_dir(prefix):
    """Create a fresh scratch directory under the temp area (not the source tree)."""
    return Path(tempfile.mkdtemp(prefix=prefix + "-", dir=_BASE))
