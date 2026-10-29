"""pubmed_dl — automated, entitlement-aware PDF retrieval for PubMed records.

Design constraints (important):
  * Only retrieves content the institution's subscription (or open access) allows.
  * Never bypasses access control; a blocked anti-bot wall is reported, not defeated.
  * Uses a real Chrome instance on the campus network *without* the local proxy,
    because publisher entitlement is tied to the campus egress IP.
"""

__version__ = "0.1.0"
