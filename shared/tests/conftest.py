import os
import sys

# Put the repo root on sys.path so ``import shared.*`` resolves when running
# ``pytest shared/tests/`` from anywhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
