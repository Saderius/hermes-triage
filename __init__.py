"""triage — native Hermes plugin entry.

Skills live in this plugin's store. Hermes sees a skill body only after the
runtime judge shortlists probability-tagged choices and the user taps one
(or auto-accept clears the configured threshold).
"""

from .plugin import register

__all__ = ["register"]
