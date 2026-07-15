"""Tiny deterministic model used by the A-02 regression fixture."""


def score(features: list[float], weight: float = 0.5) -> float:
    """Return a deterministic weighted mean for baseline testing."""
    if not features:
        return 0.0
    return sum(features) * weight / len(features)
