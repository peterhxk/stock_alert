"""Run every suite: python -m tests"""
import sys

from tests import test_engine, test_web

if __name__ == "__main__":
    rc = test_engine.main()
    print()
    rc |= test_web.main()
    sys.exit(rc)
