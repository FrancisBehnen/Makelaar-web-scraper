import os
import sys

# Make sales_scraper importable when running pytest from anywhere.
_SALES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SALES_DIR)

# The service imports the repo-root ``shared`` package (copied beside the
# entrypoint in the image); put the repo root on sys.path so tests resolve it.
_REPO_ROOT = os.path.dirname(_SALES_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
