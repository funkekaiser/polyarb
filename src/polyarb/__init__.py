"""polyarb — read-only Polymarket structural-arbitrage scanner.

Detection is the product. Execution is a separate, default-OFF module.
See SPEC.md for the math and the phased plan; docs/API_NOTES.md for verified API facts.
"""

# Versioning: PEP 440; the MINOR bumps at each milestone. 1.0 is reserved for execution-live +
# battle-tested. Each build is git-tagged `v<version>`; see the git history for the changelog.
__version__ = "0.6.0"
