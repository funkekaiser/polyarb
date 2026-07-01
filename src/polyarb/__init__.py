"""polyarb — read-only Polymarket structural-arbitrage scanner.

Detection is the product. Execution is a separate, default-OFF module.
See SPEC.md for the math and the phased plan; docs/API_NOTES.md for verified API facts.
"""

# Versioning: PEP 440. Pre-1.0 alpha — bump the alpha (a1→a2…) per working build, the MINOR
# when a milestone/phase lands; 1.0 is reserved for execution-live + battle-tested. Tag each
# build `v<version>`. 0.5.0a2: streaming scan path wired end-to-end (trigger + REST-confirm,
# R1/R7/R6-partial); remaining phase-3 hardening (R2 freshness, R5 watchdog, R6 full resub,
# R8 metrics) + the Docker streaming default + execution (phase 5) pending.
__version__ = "0.5.0a8"
