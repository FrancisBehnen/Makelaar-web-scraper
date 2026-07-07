"""Shared test setup for the responder suite.

``tg.py`` and ``github_issues.py`` import ``requests`` at module level for live
HTTP. The unit tests monkeypatch every network call, so stub ``requests`` before
any test module is imported. This lets the suite run in an offline sandbox where
``requests`` isn't installed (it is present in the Docker/CI image).
"""

import os
import sys
import types

# The service runs from the responder/ WORKDIR but imports the repo-root
# ``shared`` package; put the repo root on sys.path so tests resolve it too.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

if "requests" not in sys.modules:
    sys.modules["requests"] = types.ModuleType("requests")
