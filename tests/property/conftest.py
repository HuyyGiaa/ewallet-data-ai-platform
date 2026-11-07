"""Hypothesis profiles for the property suites.

``HYPOTHESIS_PROFILE`` selects one profile:

* ``b1-ci`` (default): 50 derandomized examples, identical on every run.
* ``b1-thorough``: 300 randomized examples; pair with ``--hypothesis-seed`` to
  replay a run.
* ``b1-mutation``: 15 derandomized examples, fast enough to run per mutant.

Deadlines are disabled because a wall-clock limit would make results depend on
machine load. ``print_blob`` prints a ``@reproduce_failure`` line on failure.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings


_SHARED = {
    "deadline": None,
    "print_blob": True,
    "database": None,
    "suppress_health_check": (HealthCheck.too_slow, HealthCheck.data_too_large),
}

settings.register_profile("b1-ci", max_examples=50, derandomize=True, **_SHARED)
settings.register_profile("b1-thorough", max_examples=300, **_SHARED)
settings.register_profile("b1-mutation", max_examples=15, derandomize=True, **_SHARED)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "b1-ci"))
