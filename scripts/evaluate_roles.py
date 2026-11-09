from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prguard.data import load_examples  # noqa: E402
from prguard.guardrails import validate_review_response  # noqa: E402
from prguard.local_model import ROLE_IDS, generate_review, load_local_model  # noqa: E402
from prguard.review_team import make_system_prompt, make_user_prompt  # noqa: E402
from prguard.retrieval import retrieve_guidance, retrieve_similar_comments  # noqa: E402
from prguard.roles import role_by_id  # noqa: E402


def load_test_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Held-out test split is missing: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def select_distinct_prs(rows: list[dict[str, Any]], role_id: str, limit: int) -> list[dict[str, Any]]:
    candidates = [row for row in rows if row.get("role_id") == role_id]
    candidates.sort(
        key=lambda row: hashlib.sha256(
            f"{row.get('source', {}).get('repo', '')}:{row.get('source', {}).get('pr_number', '')}".encode()
        ).hexdigest()
    )
    selected: list[dict[str, Any]] = []
    seen_prs: set[tuple[str, str]] = set()
    for row in candidates:
        source = row.get("source", {})
        pr_key = (str(source.get("repo", "")), str(source.get("pr_number", "")))
        if pr_key in seen_prs:
            continue
        seen_prs.add(pr_key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a small held-out review set for human inspection")
    parser.add_argument("--base-model", default=str(ROOT / "models" / "base" / "Qwen3-1.7B"))
    parser.add_argument("--adapter-root", default=str(ROOT / "outputs" / "adapters" / "by-role"))
    parser.add_argument("--test-file", default=str(ROOT / "data" / "processed" / "test.jsonl"))
    parser.add_argument("--samples-per-role", type=int, default=5)
    parser.add_argument("--max-new-tokens", type=int, default=120)
    parser.add_argument(
        "--output",
        default=str(ROOT / "outputs" / "evaluations" / "role_adapters_holdout.json"),
    )
    args = parser.parse_args()

    rows = load_test_rows(Path(args.test_file))
    for role_id in ROLE_IDS:
        if not (Path(args.adapter_root) / role_id / "adapter_config.json").is_file():
            raise FileNotFoundError(f"Missing role Adapter: {Path(args.adapter_root) / role_id}")

    tokenizer, model = load_local_model(args.base_model, args.adapter_root)
    train_rows = load_examples("train", limit=2500)
    outputs: list[dict[str, Any]] = []
    role_summary: dict[str, Any] = {}
    for role_id in ROLE_IDS:
        selected = select_distinct_prs(rows, role_id, args.samples_per_role)
        if not selected:
            raise RuntimeError(f"No held-out samples available for {role_id}")
        nonempty = 0
        for row in selected:
            messages = row["messages"]
            source = row.get("source", {})
            input_text = messages[1]["content"]
            marker = "Diff hunk:\n"
            diff = input_text.split(marker, 1)[1] if marker in input_text else input_text
            issue = f"Repository: {source.get('repo', '')}\nFile: {source.get('file_path', '')}"
            role = role_by_id(role_id)
            query = f"{issue}\n{diff}"
            guidance = retrieve_guidance(role, query)
            examples = retrieve_similar_comments(role, query, top_k=2, rows=train_rows)
            system_prompt = make_system_prompt(role)
            user_prompt = make_user_prompt(role, issue, diff, guidance, examples)
            prediction = generate_review(
                tokenizer,
                model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                adapter_name=role_id,
                max_new_tokens=args.max_new_tokens,
                deterministic=True,
            )
            nonempty += bool(prediction.strip())
            validation = validate_review_response(prediction, diff)
            outputs.append(
                {
                    "role_id": role_id,
                    "repo": source.get("repo", ""),
                    "pr_number": source.get("pr_number", ""),
                    "file_path": source.get("file_path", ""),
                    "line_number": source.get("line_number"),
                    "comment_id": source.get("comment_id", ""),
                    "held_out_diff_prompt": diff,
                    "human_reference_comment": messages[2]["content"],
                    "model_prediction": prediction,
                    "validation": validation,
                }
            )
        role_summary[role_id] = {
            "samples": len(selected),
            "nonempty_predictions": nonempty,
            "evidence_gate_passed": sum(
                item["validation"]["status"] in {"evidence_quote_match", "abstained"}
                for item in outputs[-len(selected) :]
            ),
            "human_review_required": True,
        }

    report = {
        "description": "Held-out PR examples selected by role, at most one per repo/PR. Includes output from the retrieval and evidence-validation harness; this is not an automated defect recall score.",
        "test_split_rows_by_role": dict(Counter(row.get("role_id", "unknown") for row in rows)),
        "role_summary": role_summary,
        "predictions": outputs,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(outputs)} held-out predictions to {output_path}")
    print(json.dumps(role_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
