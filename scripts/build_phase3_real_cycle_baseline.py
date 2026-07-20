"""Compatibility entry point for the Phase 3A.2 forensic builder.

The former Phase 3A generator synthesized suspected occurrences and ran a
parallel counterfactual simulator.  This entry point intentionally delegates
only to the source-grounded builder and exposes no simulator API.
"""
from scripts.phase3a2_forensics import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())
