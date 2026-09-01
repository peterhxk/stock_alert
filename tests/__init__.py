"""Test package.

Point the app at a throwaway data directory *before* anything imports
`src.config`, which reads DATA_DIR at import time. Without this the suite runs
against the real database and deletes whatever alerts you actually have.
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="price-alerts-test-"))
os.environ.setdefault("VALIDATE_TICKERS", "0")
