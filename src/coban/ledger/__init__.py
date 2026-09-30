"""Append-only event log and its projections. The only package that accesses the database."""

from coban.ledger.store import SqliteEventStore, open_event_store

__all__ = ["SqliteEventStore", "open_event_store"]
