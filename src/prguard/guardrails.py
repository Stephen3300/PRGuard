from __future__ import annotations

import json
import re
from typing import Any

from .diff_evidence import parse_unified_diff

from .diff_evidence import parse_unified_diff

SEVERITIES = {"P0", "P1", "P2", "P3"}
REQUIRED_FINDING_FIELDS = ("severity", "finding", "evidence", "impact", "suggested_fix")


def _extract_json(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    return json.loads(candidate)


def _evidence_is_in_diff(evidence: str, diff: str) -> bool:
    lines = diff.splitlines()
    changed_lines = [
        line[1:]
        for line in lines
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    ]
    if changed_lines:
        return evidence in "\n".join(changed_lines)
    return evidence in diff


def validate_review_response(response: str, diff: str) -> dict[str, Any]:
    """Require a bounded JSON response and exact evidence from the submitted diff."""
    try:
        payload = _extract_json(response)
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "unverified",
            "reasons": ["response_not_valid_json"],
            "findings": [],
            "rejected_findings": 0,
        }

    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        return {
            "status": "unverified",
            "reasons": ["missing_findings_array"],
            "findings": [],
            "rejected_findings": 0,
        }
    if len(payload["findings"]) > 2:
        return {
            "status": "unverified",
            "reasons": ["too_many_findings"],
            "findings": [],
            "rejected_findings": len(payload["findings"]),
        }

    accepted: list[dict[str, str]] = []
    reasons: list[str] = []
    rejected = 0
    for finding in payload["findings"]:
        if not isinstance(finding, dict) or any(
            not isinstance(finding.get(key), str) or not finding[key].strip()
            for key in REQUIRED_FINDING_FIELDS
        ):
            rejected += 1
            reasons.append("finding_missing_required_fields")
            continue
        severity = finding["severity"].strip().upper()
        evidence = finding["evidence"].strip()
        if severity not in SEVERITIES:
            rejected += 1
            reasons.append("invalid_severity")
            continue
        if len(evidence) < 8 or not _evidence_is_in_diff(evidence, diff):
            rejected += 1
            reasons.append("evidence_not_an_exact_diff_excerpt")
            continue
        accepted.append(
            {
                "severity": severity,
                "finding": finding["finding"].strip(),
                "evidence": evidence,
                "impact": finding["impact"].strip(),
                "suggested_fix": finding["suggested_fix"].strip(),
            }
        )

    if not payload["findings"] and not payload.get("abstain_reason"):
        reasons.append("empty_findings_without_abstain_reason")
    if not reasons and rejected == 0:
        status = "abstained" if not accepted else "evidence_quote_match"
    else:
        status = "unverified"
    return {
        "status": status,
        "reasons": sorted(set(reasons)),
        "findings": accepted,
        "abstain_reason": payload.get("abstain_reason"),
        "rejected_findings": rejected,
        "check_scope": "json_shape_and_exact_diff_excerpt_only",
    }


def validate_role_comment_evidence(
    response: str,
    expected_role_id: str,
    diff: str,
    *,
    default_file_path: str = "",
) -> dict[str, Any]:
    """Validate the additive role-comment schema against an exact changed-line ID.

    The legacy findings validator above remains unchanged. This route only accepts
    IDs produced by parsing the submitted unified diff and materializes the quote
    from that source line; the model never supplies arbitrary evidence text.
    """
    try:
        payload = _extract_json(response)
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "unverified",
            "reasons": ["response_not_valid_json"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    required_keys = {"role_id", "review_comment", "evidence_id"}
    if not isinstance(payload, dict) or set(payload) != required_keys:
        return {
            "status": "unverified",
            "reasons": ["role_comment_schema_mismatch"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    if payload.get("role_id") != expected_role_id:
        return {
            "status": "unverified",
            "reasons": ["role_id_mismatch"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    comment = payload.get("review_comment")
    evidence_id = payload.get("evidence_id")
    if not isinstance(comment, str):
        return {
            "status": "unverified",
            "reasons": ["review_comment_not_string"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    comment = comment.strip()
    if not comment and evidence_id is None:
        return {
            "status": "abstained",
            "reasons": [],
            "findings": [],
            "abstain_reason": "No supported review comment was generated.",
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    if not comment or not isinstance(evidence_id, str):
        return {
            "status": "unverified",
            "reasons": ["comment_or_evidence_id_missing"],
            "findings": [],
            "rejected_findings": 1,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    line_by_id = {
        line.evidence_id: line
        for line in parse_unified_diff(diff, default_file_path=default_file_path)
    }
    evidence_line = line_by_id.get(evidence_id)
    if evidence_line is None:
        return {
            "status": "unverified",
            "reasons": ["evidence_id_not_in_changed_lines"],
            "findings": [],
            "rejected_findings": 1,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    if len(evidence_line.text.strip()) < 8 or not _evidence_is_in_diff(evidence_line.text, diff):
        return {
            "status": "unverified",
            "reasons": ["evidence_line_failed_exact_diff_check"],
            "findings": [],
            "rejected_findings": 1,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    return {
        "status": "evidence_quote_match",
        "reasons": [],
        "findings": [
            {
                "role_id": expected_role_id,
                "review_comment": comment,
                "evidence_id": evidence_line.evidence_id,
                "evidence": {
                    "file_path": evidence_line.file_path,
                    "line_number": evidence_line.line_number,
                    "side": evidence_line.side,
                    "snippet": evidence_line.text,
                },
            }
        ],
        "rejected_findings": 0,
        "check_scope": "role_json_and_exact_changed_line_id",
        "semantic_correctness_verified": False,
    }


def validate_role_comment_evidence(
    response: str,
    expected_role_id: str,
    diff: str,
    *,
    default_file_path: str = "",
) -> dict[str, Any]:
    """Validate the additive role-comment contract without weakening legacy findings checks."""
    try:
        payload = _extract_json(response)
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "unverified",
            "reasons": ["response_not_valid_json"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    expected_keys = {"role_id", "review_comment", "evidence_id"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        return {
            "status": "unverified",
            "reasons": ["role_comment_schema_mismatch"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    if payload.get("role_id") != expected_role_id:
        return {
            "status": "unverified",
            "reasons": ["role_id_mismatch"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    comment = payload.get("review_comment")
    evidence_id = payload.get("evidence_id")
    if not isinstance(comment, str):
        return {
            "status": "unverified",
            "reasons": ["review_comment_not_string"],
            "findings": [],
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    comment = comment.strip()
    if not comment and evidence_id is None:
        return {
            "status": "abstained",
            "reasons": [],
            "findings": [],
            "abstain_reason": "No supported review comment was generated.",
            "rejected_findings": 0,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    if not comment or not isinstance(evidence_id, str):
        return {
            "status": "unverified",
            "reasons": ["comment_or_evidence_id_missing"],
            "findings": [],
            "rejected_findings": 1,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    line_by_id = {
        line.evidence_id: line
        for line in parse_unified_diff(diff, default_file_path=default_file_path)
    }
    evidence_line = line_by_id.get(evidence_id)
    if evidence_line is None:
        return {
            "status": "unverified",
            "reasons": ["evidence_id_not_in_changed_lines"],
            "findings": [],
            "rejected_findings": 1,
            "check_scope": "role_json_and_exact_changed_line_id",
        }
    if len(evidence_line.text.strip()) < 8 or not _evidence_is_in_diff(evidence_line.text, diff):
        return {
            "status": "unverified",
            "reasons": ["evidence_line_failed_exact_diff_check"],
            "findings": [],
            "rejected_findings": 1,
            "check_scope": "role_json_and_exact_changed_line_id",
        }

    return {
        "status": "evidence_quote_match",
        "reasons": [],
        "findings": [
            {
                "role_id": expected_role_id,
                "review_comment": comment,
                "evidence_id": evidence_line.evidence_id,
                "evidence": {
                    "file_path": evidence_line.file_path,
                    "line_number": evidence_line.line_number,
                    "side": evidence_line.side,
                    "snippet": evidence_line.text,
                },
            }
        ],
        "rejected_findings": 0,
        "check_scope": "role_json_and_exact_changed_line_id",
        "semantic_correctness_verified": False,
    }
