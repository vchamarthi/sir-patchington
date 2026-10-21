"""Resolve a frozen candidate separately from its latest verification generation."""

from __future__ import annotations


def candidate_record(store, tenant: str, candidate_id: str) -> dict | None:
    frozen = store.get_record(tenant, "candidate", candidate_id)
    current = store.get_record(tenant, "candidate-current", candidate_id)
    if current and (not frozen or current["candidate"] != frozen["candidate"]):
        raise ValueError("candidate association differs from frozen artifact")
    return current or frozen
