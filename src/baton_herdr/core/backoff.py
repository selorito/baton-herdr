"""How long to wait before starting something again after it stopped with an error."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Backoff:
    first_s: float = 1
    max_s: float = 60
    # A run this long counts as healthy: the next failure starts from first_s again.
    healthy_s: float = 60
