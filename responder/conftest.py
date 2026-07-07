"""Shared test setup for the responder suite.

``tg.py`` and ``github_issues.py`` import ``requests`` at module level for live
HTTP. The unit tests monkeypatch every network call, so stub ``requests`` before
any test module is imported. This lets the suite run in an offline sandbox where
``requests`` isn't installed (it is present in the Docker/CI image).
"""

import sys
import types

if "requests" not in sys.modules:
    sys.modules["requests"] = types.ModuleType("requests")
