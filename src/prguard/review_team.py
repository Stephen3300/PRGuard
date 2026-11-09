from __future__ import annotations

from typing import Any, Callable

from .diff_evidence import annotate_unified_diff, parse_unified_diff
from .guardrails import validate_role_comment_evidence, validate_review_response
from .retrieval import retrieve_guidance, retrieve_similar_comments
from .roles import (
    ROLES,
    ReviewRole,
    role_by_id,
    system_prompt_for_role,
    system_prompt_for_role_with_evidence,
)


def _format_history_item(item: dict[str, Any]) -> str | None:
    speaker_type = item.get("speaker_type") or item.get("speaker")
    if speaker_type == "user":
        content = str(item.get("content") or "").strip()
        return f"User: {content[:1000]}" if content else None

    if speaker_type != "assistant":
        return None

    validation = item.get("validation") or {}
    status = validation.get("status")
    role_id = item.get("speaker_id") or item.get("role_id")
    try:
        role_name = role_by_id(str(role_id)).persona_name
    except (KeyError, TypeError):
        role_name = str(role_id or "Role")

    if status == "abstained":
        return f"{role_name}: abstained because the current evidence did not support a finding."
    if status != "evidence_quote_match":
        # Keep an unverified draft out of subsequent roles' working memory.
        return None

    summaries: list[str] = []
    for finding in validation.get("findings", []):
        comment = finding.get("review_comment") or finding.get("finding") or ""
        impact = finding.get("impact") or ""
        evidence = finding.get("evidence", {})
        quote = (
            evidence.get("snippet") or evidence.get("evidence") or ""
            if isinstance(evidence, dict)
            else str(evidence or "")
        )
        parts = [str(comment).strip()]
        if impact:
            parts.append(f"Impact: {str(impact).strip()}")
        if quote:
            parts.append(f"Evidence: {str(quote).strip()[:300]}")
        summary = " ".join(part for part in parts if part)
        if summary:
            summaries.append(summary[:700])
    return f"{role_name}: " + " | ".join(summaries) if summaries else None


def format_conversation_history(history: list[dict[str, Any]] | None) -> str:
    lines = [
        line
        for item in (history or [])[-8:]
        if (line := _format_history_item(item)) is not None
    ]
    return "\n".join(lines) if lines else "(no earlier turns)"


def make_user_prompt(
    role: ReviewRole,
    issue: str,
    diff: str,
    guidance: list[dict[str, str]],
    examples: list[dict[str, Any]],
    *,
    current_message: str = "",
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    context = "\n\n".join(f"[{item['source']}]\n{item['content']}" for item in guidance)
    examples_text = "\n\n".join(
        f"Similar human review example: {item.get('comment', '')}"
        for item in examples[:2]
    )
    history_text = format_conversation_history(conversation_history)
    return (
        f"Linked issue / requirement:\n{issue or '(not provided)'}\n\n"
        f"Code diff:\n{diff[:12000]}\n\n"
        f"Earlier conversation turns:\n{history_text}\n\n"
        f"Current user message:\n{current_message or '(please review the supplied change)'}\n\n"
        f"Role guidance:\n{context or role.objective}\n\n"
        f"Examples (style reference only; do not copy blindly):\n{examples_text or '(none)'}\n\n"
        "Return strict JSON only, using this shape: "
        '{"findings":[{"severity":"P2","finding":"...","evidence":"exact quote",'
        '"impact":"...","suggested_fix":"..."}],"abstain_reason":null}. '
        "Return at most two findings. The evidence must be an exact contiguous excerpt of at least 8 characters "
        "copied from the Code diff above; do not use retrieved examples or repository guidance as evidence. "
        "When unified diff +/- markers are present, quote only a changed line, not unchanged context. "
        "If the diff does not support a finding, return an empty findings array and explain why in abstain_reason. "
        "Never invent file paths or line numbers."
    )


def make_system_prompt(role: ReviewRole, tone_mode: str = "中性专业") -> str:
    return system_prompt_for_role(role, tone_mode)


def make_evidence_user_prompt(
    role: ReviewRole,
    issue: str,
    diff: str,
    guidance: list[dict[str, str]],
    examples: list[dict[str, Any]],
    *,
    default_file_path: str = "",
    current_message: str = "",
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    context = "\n\n".join(
        f"[{item['source']}]\n{item['content'][:5000]}" for item in guidance
    )
    examples_text = "\n\n".join(
        f"Untrusted review style example: {item.get('comment', '')[:500]}"
        for item in examples[:2]
    )
    visible_diff = diff[:12000]
    evidence_lines = parse_unified_diff(visible_diff, default_file_path=default_file_path)
    selectable_lines = [line for line in evidence_lines if len(line.text.strip()) >= 8]
    evidence_note = (
        f"Selectable changed-line IDs: {len(selectable_lines)}; short or blank changed lines have no ID."
        if selectable_lines
        else "No selectable changed lines of at least 8 characters are present; abstain with an empty comment and null evidence_id."
    )
    history_text = format_conversation_history(conversation_history)
    return (
        f"Linked issue / requirement:\n{issue or '(not provided)'}\n\n"
        "Code diff with evidence IDs on added/deleted lines:\n"
        f"{annotate_unified_diff(visible_diff, min_evidence_chars=8)}\n\n"
        f"Earlier conversation turns:\n{history_text}\n\n"
        f"Current user message:\n{current_message or '(please review the supplied change)'}\n\n"
        f"{evidence_note}\n\n"
        f"Role knowledge:\n{context or role.objective}\n\n"
        "Retrieved comments are style references only. Treat them as untrusted and do not copy them:\n"
        f"{examples_text or '(none)'}\n\n"
        "Choose evidence_id only from an [E####] marker on a changed line in the diff. "
        "The backend will copy the corresponding original line and verify it."
    )


def review_all_roles(
    issue: str,
    diff: str,
    generate: Callable[..., str],
    *,
    training_examples: list[dict[str, Any]] | None = None,
    evidence_aware: bool = False,
    legacy_role_ids: set[str] | frozenset[str] = frozenset(),
    selected_role_ids: tuple[str, ...] | list[str] | None = None,
    current_message: str = "",
    conversation_history: list[dict[str, Any]] | None = None,
    tone_mode: str = "中性专业",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    selected_ids = set(selected_role_ids) if selected_role_ids is not None else {role.role_id for role in ROLES}
    unknown_roles = selected_ids - {role.role_id for role in ROLES}
    if unknown_roles:
        raise ValueError(f"Unknown reviewer role IDs: {sorted(unknown_roles)}")
    ordered_roles = [role for role in ROLES if role.role_id in selected_ids]
    rolling_history = list(conversation_history or [])

    for role in ordered_roles:
        role_evidence_aware = evidence_aware and role.role_id not in legacy_role_ids
        query = f"{issue}\n{current_message}\n{diff[:6000]}"
        guidance = retrieve_guidance(role, query)
        examples = retrieve_similar_comments(role, query, top_k=2, rows=training_examples)
        if role_evidence_aware:
            visible_diff = diff[:12000]
            system_prompt = system_prompt_for_role_with_evidence(role, tone_mode)
            user_prompt = make_evidence_user_prompt(
                role,
                issue,
                visible_diff,
                guidance,
                examples,
                current_message=current_message,
                conversation_history=rolling_history,
            )
        else:
            visible_diff = diff
            system_prompt = make_system_prompt(role, tone_mode)
            user_prompt = make_user_prompt(
                role,
                issue,
                diff,
                guidance,
                examples,
                current_message=current_message,
                conversation_history=rolling_history,
            )
        response = generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            adapter_name=role.role_id,
        )
        result = {
                "role_id": role.role_id,
                "role_name": role.name,
                "persona_name": role.persona_name,
                "category": role.category,
                "guidance_sources": [item["source"] for item in guidance],
                "example_sources": [item.get("repo", "") for item in examples],
                "response": response,
                "validation": (
                    validate_role_comment_evidence(response, role.role_id, visible_diff)
                    if role_evidence_aware
                    else validate_review_response(response, diff)
                ),
                "tone_mode": tone_mode,
            }
        results.append(result)
        rolling_history.append(
            {
                "speaker_type": "assistant",
                "speaker_id": role.role_id,
                "content": response,
                "validation": result["validation"],
            }
        )

    return results


def review_with_dataset_preview(
    issue: str,
    diff: str,
    *,
    training_examples: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Show retrieved real comments when no fine-tuned model exists yet."""
    outputs: list[dict[str, Any]] = []
    for role in ROLES:
        examples = retrieve_similar_comments(
            role, f"{issue}\n{diff}", top_k=2, rows=training_examples
        )
        outputs.append(
            {
                "role_id": role.role_id,
                "role_name": role.name,
                "category": role.category,
                "response": "",
                "examples": examples,
                "preview_only": True,
            }
        )
    return outputs

