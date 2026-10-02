"""Runs ``baton-detect usage`` and stores what it reports (ADR 0011)."""

from baton_herdr.collector.usage import UsageCollector, detector_binary

__all__ = ["UsageCollector", "detector_binary"]
