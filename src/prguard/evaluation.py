from __future__ import annotations

from typing import Any

from .retrieval import retrieve_similar_comments
from .roles import ROLES


def role_coverage(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {role.role_id: 0 for role in ROLES}
    for row in rows:
        role_id = row.get("role_id")
        if role_id in counts:
            counts[role_id] += 1
    return counts


def evaluate_retrieval(role_id: str, query: str, expected_ids: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    role = next(role for role in ROLES if role.role_id == role_id)
    top = retrieve_similar_comments(role, query, top_k=5, rows=rows)
    found = {row.get("source", {}).get("comment_id") for row in top}
    gold = set(expected_ids)
    return {
        "recall_at_5": len(found & gold) / len(gold) if gold else 0.0,
        "retrieved": len(found),
        "expected": len(gold),
    }

