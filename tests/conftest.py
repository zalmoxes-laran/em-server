"""Import paths for the test suite, in one place.

Two entries, and the order is the point:

* the repo root, so ``app`` is importable without installing em-server;
* **the s3Dgraphy checkout ahead of any installed wheel**, because em-server is
  developed against the reference implementation as it is *now*. A test that
  silently validated against last week's published dev would pass while the thing
  it describes was already different.

This used to live at the top of each test module. One copy per file is one copy too
many: the day the layout changes, the file nobody remembered keeps the old path and
fails in a way that looks like a code problem.
"""

from __future__ import annotations

import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_CHECKOUT = _REPO.parent / "s3Dgraphy" / "src"
if _CHECKOUT.is_dir() and str(_CHECKOUT) not in sys.path:
    sys.path.insert(0, str(_CHECKOUT))
