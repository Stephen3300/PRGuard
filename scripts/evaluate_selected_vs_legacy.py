from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from prepare_evidence_sft import make_evidence_example  # noqa: E402
from prguard.data import read_jsonl  # noqa: E402
from prguard.diff_evidence import EvidenceLine, parse_unified_diff  # noqa: E402
from prguard.guardrails import validate_review_response, validate_role_comment_evidence  # noqa: E402
from prguard.local_model import ROLE_IDS, generate_review, load_local_model  # noqa: E402
from prguard.review_team import (  # noqa: E402
    make_evidence_user_prompt,
    make_system_prompt,
    make_user_prompt,
)
from prguard.retrieval import retrieve_guidance, retrieve_similar_comments  # noqa: E402
from prguard.roles import role_by_id, system_prompt_for_role_with_evidence  # noqa: E402


def _parse_json_payload(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _duplicate_4gram_rate(text: str) -> float:
    tokens = re.findall(r"[a-z0-9_]+", text.casefold())
    if len(tokens) < 4:
        return 0.0
    grams = [tuple(tokens[index : index + 4]) for index in range(len(tokens) - 3)]
    return (len(grams) - len(set(grams))) / len(grams)


def read_external_test(path: Path) -> dict[str, list[dict[str, Any]]]:
    raw_rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_rows, list):
        raise ValueError(f"Expected a JSON list in {path}")
    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for raw in raw_rows:
        example = make_evidence_example(raw)
        if not example:
            continue
        role_id = example["role_id"]
        source = example["source"]
        key = (str(source.get("repo", "")).lower(), str(source.get("pr_number", "")))
        if key in seen[role_id]:
            continue
        seen[role_id].add(key)
        example["raw_code"] = str(raw.get("code") or "")
        by_role[role_id].append(example)
    for role_id in by_role:
        by_role[role_id].sort(
            key=lambda row: hashlib.sha256(
                f"{row['source'].get('repo','')}:{row['source'].get('pr_number','')}".encode("utf-8")
            ).hexdigest()
        )
    return by_role


def select_disjoint_test_rows(
    rows_by_role: dict[str, list[dict[str, Any]]], samples_per_role: int
) -> dict[str, list[dict[str, Any]]]:
    selected: dict[str, list[dict[str, Any]]] = {role_id: [] for role_id in ROLE_IDS}
    positions = {role_id: 0 for role_id in ROLE_IDS}
    used_prs: set[tuple[str, str]] = set()
    while any(len(selected[role_id]) < samples_per_role for role_id in ROLE_IDS):
        made_progress = False
        for role_id in ROLE_IDS:
            if len(selected[role_id]) >= samples_per_role:
                continue
            candidates = rows_by_role.get(role_id, [])
            while positions[role_id] < len(candidates):
                row = candidates[positions[role_id]]
                positions[role_id] += 1
                source = row["source"]
                key = (str(source.get("repo", "")).lower(), str(source.get("pr_number", "")))
                if key in used_prs:
                    continue
                used_prs.add(key)
                selected[role_id].append(row)
                made_progress = True
                break
        if not made_progress and any(
            len(selected[role_id]) < samples_per_role for role_id in ROLE_IDS
        ):
            raise RuntimeError(
                "Raw test.json has too few disjoint repo/PR examples with mapped evidence "
                "for the requested samples per role."
            )
    return selected


def line_matches_gold(line: EvidenceLine | None, gold: dict[str, Any] | None) -> bool:
    if line is None or gold is None:
        return False
    return (
        (not line.file_path or line.file_path == gold.get("file_path"))
        and line.line_number == gold.get("line_number")
        and line.side == gold.get("side")
        and line.text == gold.get("text")
    )


def run_legacy(
    tokenizer: Any,
    model: Any,
    selected: dict[str, list[dict[str, Any]]],
    train_rows: list[dict[str, Any]],
    max_new_tokens: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summaries: dict[str, Any] = {}
    outputs: list[dict[str, Any]] = []
    for role_id in ROLE_IDS:
        print(f"Evaluating legacy Adapter: {role_id}", flush=True)
        role = role_by_id(role_id)
        evidence_pass = anchor_match = nonempty = 0
        for row in selected[role_id]:
            source = row["source"]
            diff = row["raw_code"]
            issue = f"Repository: {source.get('repo', '')}\nFile: {source.get('file_path', '')}"
            query = f"{issue}\n{diff}"
            guidance = retrieve_guidance(role, query)
            examples = retrieve_similar_comments(role, query, top_k=2, rows=train_rows)
            response = generate_review(
                tokenizer,
                model,
                system_prompt=make_system_prompt(role),
                user_prompt=make_user_prompt(role, issue, diff, guidance, examples),
                adapter_name=role_id,
                max_new_tokens=max_new_tokens,
                deterministic=True,
            )
            validation = validate_review_response(response, diff)
            findings = validation.get("findings", [])
            evidence_pass += validation.get("status") == "evidence_quote_match"
            nonempty += bool(findings)
            gold = source.get("evidence_line")
            matched_line = None
            if gold:
                for line in parse_unified_diff(diff, str(source.get("file_path", ""))):
                    if any(
                        item.get("evidence", "") in line.text
                        for item in findings
                        if item.get("evidence")
                    ):
                        matched_line = line
                        break
            anchor_match += line_matches_gold(matched_line, gold)
            outputs.append(
                {
                    "model": "legacy_by_role",
                    "role_id": role_id,
                    "repo": source.get("repo", ""),
                    "pr_number": source.get("pr_number", ""),
                    "file_path": source.get("file_path", ""),
                    "human_reference_comment": source.get("review_comment", ""),
                    "model_response": response,
                    "validation": validation,
                    "gold_evidence_line": gold,
                    "evidence_anchor_matches_gold": bool(line_matches_gold(matched_line, gold)),
                }
            )
        total = len(selected[role_id])
        summaries[role_id] = {
            "samples": total,
            "nonempty_rate": nonempty / total,
            "legacy_exact_evidence_gate_rate": evidence_pass / total,
            "evidence_anchor_match_rate": anchor_match / total,
            "semantic_correctness_manually_verified": False,
        }
        print(f"Completed legacy Adapter: {role_id}", flush=True)
    return summaries, outputs


def run_selected(
    tokenizer: Any,
    model: Any,
    selected: dict[str, list[dict[str, Any]]],
    train_rows: list[dict[str, Any]],
    max_new_tokens: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summaries: dict[str, Any] = {}
    outputs: list[dict[str, Any]] = []
    for role_id in ROLE_IDS:
        print(f"Evaluating selected Adapter: {role_id}", flush=True)
        role = role_by_id(role_id)
        evidence_pass = anchor_match = nonempty = 0
        exact_schema = role_match = length_pass = non_repetitive = parseable_nonempty = 0
        repetition_rates: list[float] = []
        for row in selected[role_id]:
            source = row["source"]
            diff = row["raw_code"]
            issue = f"Repository: {source.get('repo', '')}\nFile: {source.get('file_path', '')}"
            query = f"{issue}\n{diff}"
            guidance = retrieve_guidance(role, query)
            examples = retrieve_similar_comments(role, query, top_k=2, rows=train_rows)
            response = generate_review(
                tokenizer,
                model,
                system_prompt=system_prompt_for_role_with_evidence(role),
                user_prompt=make_evidence_user_prompt(
                    role,
                    issue,
                    diff,
                    guidance,
                    examples,
                    default_file_path=str(source.get("file_path", "")),
                ),
                adapter_name=role_id,
                max_new_tokens=max_new_tokens,
                deterministic=True,
            )
            validation = validate_role_comment_evidence(
                response,
                role_id,
                diff,
                default_file_path=str(source.get("file_path", "")),
            )
            findings = validation.get("findings", [])
            evidence_pass += validation.get("status") == "evidence_quote_match"
            nonempty += bool(findings)
            payload = _parse_json_payload(response)
            comment = ""
            if payload is not None:
                comment_value = payload.get("review_comment")
                comment = comment_value.strip() if isinstance(comment_value, str) else ""
                exact_schema += set(payload) == {"role_id", "review_comment", "evidence_id"}
                role_match += payload.get("role_id") == role_id
            parseable_nonempty += bool(comment)
            length_pass += 40 <= len(comment) <= 500
            repetition_rate = _duplicate_4gram_rate(comment)
            if comment:
                repetition_rates.append(repetition_rate)
                non_repetitive += repetition_rate <= 0.08
            gold = source.get("evidence_line")
            predicted = None
            if findings:
                evidence = findings[0]["evidence"]
                predicted = EvidenceLine(
                    evidence_id=findings[0]["evidence_id"],
                    file_path=evidence["file_path"],
                    line_number=evidence["line_number"],
                    side=evidence["side"],
                    text=evidence["snippet"],
                )
            anchor_match += line_matches_gold(predicted, gold)
            outputs.append(
                {
                    "model": "selected_evidence_adapter",
                    "role_id": role_id,
                    "repo": source.get("repo", ""),
                    "pr_number": source.get("pr_number", ""),
                    "file_path": source.get("file_path", ""),
                    "human_reference_comment": source.get("review_comment", ""),
                    "model_response": response,
                    "validation": validation,
                    "gold_evidence_line": gold,
                    "predicted_evidence_line": predicted.to_dict() if predicted else None,
                    "evidence_anchor_matches_gold": bool(line_matches_gold(predicted, gold)),
                }
            )
        total = len(selected[role_id])
        summaries[role_id] = {
            "samples": total,
            "nonempty_rate": nonempty / total,
            "selected_schema_exact_evidence_gate_rate": evidence_pass / total,
            "exact_role_comment_evidence_schema_rate": exact_schema / total,
            "role_id_match_rate": role_match / total,
            "parseable_nonempty_comment_rate": parseable_nonempty / total,
            "concise_length_rate_40_to_500_chars": length_pass / total,
            "non_repetitive_rate_duplicate_4gram_le_0_08": non_repetitive / total,
            "mean_duplicate_4gram_rate": (
                sum(repetition_rates) / len(repetition_rates) if repetition_rates else None
            ),
            "evidence_anchor_match_rate": anchor_match / total,
            "semantic_correctness_manually_verified": False,
        }
        print(f"Completed selected Adapter: {role_id}", flush=True)
    return summaries, outputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One paired test pass over raw CRC-PY test.json for legacy and validation-selected adapters"
    )
    parser.add_argument("--base-model", default=str(ROOT / "models" / "base" / "Qwen3-1.7B"))
    parser.add_argument("--legacy-adapter-root", default=str(ROOT / "outputs" / "adapters" / "by-role"))
    parser.add_argument("--selected-adapter-root", default=str(ROOT / "outputs" / "adapters" / "by-role-selected"))
    parser.add_argument(
        "--external-test",
        default=str(ROOT / "data" / "raw" / "crc-py-dataset" / "data" / "test" / "test.json"),
    )
    parser.add_argument(
        "--train-file",
        default=str(ROOT / "data" / "processed" / "evidence_selected" / "train.jsonl"),
    )
    parser.add_argument("--samples-per-role", type=int, default=10)
    parser.add_argument("--max-new-tokens", type=int, default=180)
    parser.add_argument(
        "--output",
        default=str(ROOT / "outputs" / "evaluations" / "selected_vs_legacy_external_test.json"),
    )
    args = parser.parse_args()

    rows_by_role = read_external_test(Path(args.external_test))
    selected = select_disjoint_test_rows(rows_by_role, args.samples_per_role)
    train_rows = read_jsonl(Path(args.train_file))
    train_repo_pr = {
        (str(row["source"].get("repo", "")).lower(), str(row["source"].get("pr_number", "")))
        for row in train_rows
    }
    unique_test_prs = {
        (str(row["source"].get("repo", "")).lower(), str(row["source"].get("pr_number", "")))
        for rows in selected.values()
        for row in rows
    }
    if len(unique_test_prs) != args.samples_per_role * len(ROLE_IDS):
        raise RuntimeError("The final raw-test sample must contain unique repo/PRs across all roles.")
    train_test_overlap = len(unique_test_prs & train_repo_pr)
    if train_test_overlap:
        raise RuntimeError(
            f"Raw external test selection overlaps {train_test_overlap} training repo/PR keys."
        )

    # One paired pass: baseline and selected adapters see the same raw-test sample set.
    legacy_tokenizer, legacy_model = load_local_model(args.base_model, args.legacy_adapter_root)
    legacy_summary, legacy_outputs = run_legacy(
        legacy_tokenizer, legacy_model, selected, train_rows, args.max_new_tokens
    )
    del legacy_model, legacy_tokenizer
    torch.cuda.empty_cache()

    selected_tokenizer, selected_model = load_local_model(args.base_model, args.selected_adapter_root)
    selected_summary, selected_outputs = run_selected(
        selected_tokenizer, selected_model, selected, train_rows, args.max_new_tokens
    )
    del selected_model, selected_tokenizer
    torch.cuda.empty_cache()

    report = {
        "description": (
            "Single paired evaluation on raw CRC-PY data/test/test.json after validation-best "
            "checkpoint selection. Exact line-anchor metrics are weak-label proxies, not semantic correctness."
        ),
        "external_test": str(Path(args.external_test)),
        "training_repo_pr_overlap": train_test_overlap,
        "unique_repo_pr_samples": len(unique_test_prs),
        "samples_per_role": args.samples_per_role,
        "legacy_summary": legacy_summary,
        "selected_summary": selected_summary,
        "predictions": legacy_outputs + selected_outputs,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"legacy": legacy_summary, "selected": selected_summary},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Saved single paired raw-test report to {output}")


if __name__ == "__main__":
    main()
