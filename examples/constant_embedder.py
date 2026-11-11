"""Example embedder plugged in via ``embedder: examples path``.

Point ``plugins.entries.triage.settings.embedder`` at
``constant_embedder.ConstantEmbedder`` only after this directory is on
``PYTHONPATH``. It exists to show the extension point, not as a real model.
"""


class ConstantEmbedder:
    def score(self, query: str, skill) -> float:
        if skill.name == "weather-brief":
            return 1.0
        return 0.0
