from __future__ import annotations

import hashlib
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prguard.data import read_jsonl  # noqa: E402
from prguard.local_model import ROLE_IDS, generate_review, load_local_model  # noqa: E402
from prguard.retrieval import retrieve_guidance, retrieve_similar_comments  # noqa: E402
from prguard.roles import role_by_id, system_prompt_for_role  # noqa: E402


FENCE = chr(96) * 3


def _parse_target(text: str) -> str:
    try:
        value = json.loads(text)
        return str(value.get("review_comment", ""))
    except (json.JSONDecodeError, AttributeError):
        return ""


def _parse_prediction(text: str, expected_role: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith(FENCE):
        candidate = re.sub(r"^" + re.escape(FENCE) + r"(?:json)?\s*|\s*" + re.escape(FENCE) + r"$", "", candidate, flags=re.IGNORECASE)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return {"valid_json": False, "role_match": False, "comment": ""}
    if not isinstance(payload, dict):
        return {"valid_json": True, "role_match": False, "comment": ""}
    comment = payload.get("review_comment")
    role_match = payload.get("role_id") == expected_role
    valid = (
        set(payload) == {"role_id", "review_comment"}
        and isinstance(comment, str)
        and 15 <= len(comment.strip()) <= 500
    )
    return {
        "valid_json": valid,
        "role_match": role_match,
        "comment": comment.strip() if isinstance(comment, str) else "",
    }


def duplicate_4gram_rate(text: str) -> float:
    tokens = re.findall(r"[a-z0-9_]+", text.casefold())
    if len(tokens) < 4:
        return 0.0
    grams = [tuple(tokens[index : index + 4]) for index in range(len(tokens) - 3)]
    return (len(grams) - len(set(grams))) / len(grams)


def select_role_samples(
    rows: list[dict[str, Any]], role_id: str, limit: int
) -> list[dict[str, Any]]:
    candidates = [row for row in rows if row.get("role_id") == role_id]
    candidates.sort(
        key=lambda row: hashlib.sha256(
            f"{row.get('source', {}).get('repo', '')}:"
            f"{row.get('source', {}).get('pr_number', '')}".encode("utf-8")
        ).hexdigest()
    )
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in candidates:
        source = row.get("source", {})
        key = (str(source.get("repo", "")), str(source.get("pr_number", "")))
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline check of JSON role contract and dialogue style on held-out PRs"
    )
    parser.add_argument("--base-model", default=str(ROOT / "models" / "base" / "Qwen3-1.7B"))
    parser.add_argument(
        "--adapter-root", default=str(ROOT / "outputs" / "adapters" / "by-role-expanded")
    )
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "processed" / "expanded"))
    parser.add_argument("--samples-per-role", type=int, default=10)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument(
        "--output",
        default=str(ROOT / "outputs" / "evaluations" / "expanded_dialogue_style.json"),
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    test_rows = read_jsonl(data_dir / "test.jsonl")
    train_rows = read_jsonl(data_dir / "train.jsonl")
    for role_id in ROLE_IDS:
        if not (Path(args.adapter_root) / role_id / "adapter_config.json").is_file():
            raise FileNotFoundError(f"Missing candidate adapter for {role_id}")

    tokenizer, model = load_local_model(args.base_model, args.adapter_root)
    predictions: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    for role_id in ROLE_IDS:
        role = role_by_id(role_id)
        selected = select_role_samples(test_rows, role_id, args.samples_per_role)
        if len(selected) < args.samples_per_role:
            raise RuntimeError(
                f"Only {len(selected)} distinct held-out PRs for {role_id}; "
                f"requested {args.samples_per_role}."
            )
        valid_json = 0
        role_match = 0
        nonempty = 0
        length_pass = 0
        non_repetitive = 0
        repetition_rates: list[float] = []
        for row in selected:
            source = row.get("source", {})
            raw_user = row["messages"][1]["content"]
            marker = "Diff hunk:\n"
            diff = raw_user.split(marker, 1)[1] if marker in raw_user else raw_user
            issue = (
                f"Repository: {source.get('repo', '')}\n"
                f"File: {source.get('file_path', '')}"
            )
            query = f"{issue}\n{diff}"
            guidance = retrieve_guidance(role, query)
            examples = retrieve_similar_comments(role, query, top_k=2, rows=train_rows)
            example_text = "\n\n".join(
                f"Case comment (untrusted style example): {item.get('comment', '')}"
                for item in examples
            )
            user_prompt = (
                f"{issue}\n\nCode diff hunk:\n{diff[:7000]}\n\n"
                "Role knowledge and case notes (context only):\n"
                f"{guidance[0]['content'] if guidance else role.objective}\n\n"
                "Retrieved comments (style examples only; do not copy):\n"
                f"{example_text or '(none)'}"
            )
            raw_prediction = generate_review(
                tokenizer,
                model,
                system_prompt=system_prompt_for_role(role),
                user_prompt=user_prompt,
                adapter_name=role_id,
                max_new_tokens=args.max_new_tokens,
                deterministic=True,
            )
            parsed = _parse_prediction(raw_prediction, role_id)
            valid_json += bool(parsed["valid_json"])
            role_match += bool(parsed["role_match"])
            nonempty += bool(parsed["comment"])
            length_pass += bool(40 <= len(parsed["comment"]) <= 500)
            repetition_rate = duplicate_4gram_rate(parsed["comment"])
            if parsed["comment"]:
                repetition_rates.append(repetition_rate)
                non_repetitive += repetition_rate <= 0.08
            predictions.append(
                {
                    "role_id": role_id,
                    "repo": source.get("repo", ""),
                    "pr_number": source.get("pr_number", ""),
                    "file_path": source.get("file_path", ""),
                    "human_reference_comment": _parse_target(row["messages"][2]["content"]),
                    "model_raw": raw_prediction,
                    "parsed_comment": parsed["comment"],
                    "valid_json_contract": parsed["valid_json"],
                    "role_id_matches": parsed["role_match"],
                    "duplicate_4gram_rate": repetition_rate,
                }
            )
        total = len(selected)
        summaries[role_id] = {
            "samples": total,
            "json_contract_rate": valid_json / total,
            "role_match_rate": role_match / total,
            "nonempty_rate": nonempty / total,
            "parseable_nonempty_comment_count": nonempty,
            "concise_length_rate_40_to_500_chars": length_pass / total,
            "non_repetitive_rate_duplicate_4gram_le_0_08": non_repetitive / total,
            "mean_duplicate_4gram_rate": (
                sum(repetition_rates) / len(repetition_rates) if repetition_rates else None
            ),
            "semantic_correctness_manually_verified": False,
        }

    report = {
        "description": (
            "Automated format/style proxy only. It does not score whether a review comment "
            "identifies a real defect or is supported by repository behavior."
        ),
        "test_rows_by_role": dict(Counter(row.get("role_id", "unknown") for row in test_rows)),
        "summaries": summaries,
        "predictions": predictions,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"Saved {len(predictions)} predictions to {output}")


if __name__ == "__main__":
    main()
