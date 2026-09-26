"""
demo_app/tests/test_flaky_sleep.py — flaky test: timing category.

The test sleeps for a random duration in [0.0, 0.15) seconds and asserts the
elapsed time is less than 0.1 s.  It fails ~33% of the time (when the sleep
lands in the 0.10–0.15 s window).

FlakeHunter hint: timing  (time.sleep + import time detected by scanner)
"""

import time
import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_timing_sensitive():
    """Assert a sleep completes within a tight deadline — fails ~33% of runs."""
    start = time.time()
    time.sleep(random.uniform(0.0, 0.15))
    elapsed = time.time() - start
    assert elapsed < 0.1, f"Took {elapsed:.3f}s, expected < 0.1s"
