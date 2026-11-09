from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from prepare_sft import (  # noqa: E402
    load_source_rows,
    make_examples,
    split_group,
)
from prguard.diff_evidence import annotate_unified_diff, evidence_for_source_line  # noqa: E402
from prguard.roles import role_by_id, system_prompt_for_role_with_evidence  # noqa: E402


MAX_DIFF_CHARS = 1200
MAX_REVIEW_COMMENT_CHARS = 500
ROLE_IDS = (
    "functional_reviewer",
    "refactoring_reviewer",
    "documentation_reviewer",
    "requirements_reviewer",
    "test_reviewer",
)


def evidence_annotated_context(
    code: str, file_path: str, target_evidence_id: str
) -> str | None:
    annotated = annotate_unified_diff(code)
    marker = f"[{target_evidence_id}]"
    lines = annotated.splitlines()
    target_index = next(
        (index for index, line in enumerate(lines) if marker in line),
        None,
    )
    if target_index is None:
        return None
    hunk_start = max(
        (index for index, line in enumerate(lines[: target_index + 1]) if line.startswith("@@")),
        default=0,
    )
    start = max(hunk_start, target_index - 6)
    end = min(len(lines), target_index + 7)
    excerpt_lines = lines[start:end]
    clipped: list[str] = []
    for index, line in enumerate(excerpt_lines):
        limit = 360 if marker in line else 180
        clipped.append(line if len(line) <= limit else line[:limit])
    excerpt = "\n".join(clipped)
    if marker not in excerpt:
        return None
    return excerpt


def make_evidence_example(row: dict[str, Any]) -> dict[str, Any] | None:
    base = make_examples(row, min_comment_chars=24)
    if not base:
        return None
    source_example = base[0]
    source = source_example["source"]
    if len(source.get("review_comment", "")) > MAX_REVIEW_COMMENT_CHARS:
        return None
    file_path = str(source.get("file_path", ""))
    code = str(row.get("code") or "")
    evidence = evidence_for_source_line(code, file_path, source.get("line_number"))
    if evidence is None:
        return None

    context = evidence_annotated_context(code, file_path, evidence.evidence_id)
    if context is None:
        return None
    role_id = source_example["role_id"]
    role = role_by_id(role_id)
    user_content = (
        f"Repository: {source.get('repo', '')}\n"
        f"File: {file_path}\n"
        "Review this diff hunk. Changed lines have exact evidence IDs in square brackets.\n"
        f"Diff hunk with evidence IDs:\n{context}"
    )
    assistant_content = json.dumps(
        {
            "role_id": role_id,
            "review_comment": source["review_comment"],
            "evidence_id": evidence.evidence_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "role_id": role_id,
        "category": row.get("category", ""),
        "source": {
            **source,
            "evidence_id": evidence.evidence_id,
            "evidence_line": evidence.to_dict(),
        },
        "messages": [
            {"role": "system", "content": system_prompt_for_role_with_evidence(role)},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build role-specific dialogue targets with line-number-derived evidence IDs"
    )
    parser.add_argument("--max-per-role", type=int, default=0)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "data" / "processed" / "evidence_selected"
    )
    args = parser.parse_args()

    source_rows = load_source_rows()
    by_role: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    skipped = Counter()
    seen: set[tuple[str, str, str]] = set()
    for row in source_rows:
        basic = make_examples(row, min_comment_chars=24)
        if not basic:
            skipped["comment_or_category_filter"] += 1
            continue
        evidence_example = make_evidence_example(row)
        if evidence_example is None:
            comment_length = len(basic[0]["source"].get("review_comment", ""))
            reason = (
                "comment_above_style_limit"
                if comment_length > MAX_REVIEW_COMMENT_CHARS
                else "line_number_not_mappable_to_unique_changed_line_or_context_too_long"
            )
            skipped[reason] += 1
            continue
        key = (
            evidence_example["role_id"],
            evidence_example["messages"][1]["content"],
            evidence_example["messages"][2]["content"],
        )
        if key in seen:
            skipped["duplicate_after_evidence_annotation"] += 1
            continue
        seen.add(key)
        by_role[evidence_example["role_id"]].append((row, evidence_example))

    pre_cap = {role_id: len(items) for role_id, items in by_role.items()}
    cap_dropped: Counter[str] = Counter()
    splits: dict[str, list[dict[str, Any]]] = {
        "train": [],
        "validation": [],
        "test": [],
    }
    retained = Counter()
    for role_id, items in by_role.items():
        items.sort(
            key=lambda item: hashlib.sha256(
                f"{item[1]['source']['repo']}:{item[1]['source']['pr_number']}:"
                f"{item[1]['source']['comment_id']}".encode("utf-8")
            ).hexdigest()
        )
        if args.max_per_role > 0:
            cap_dropped[role_id] = max(0, len(items) - args.max_per_role)
            items = items[: args.max_per_role]
        for source_row, example in items:
            split = split_group(source_row)
            splits[split].append(example)
            retained[role_id] += 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for split, rows in splits.items():
        path = args.output_dir / f"{split}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{split}: {len(rows)} -> {path}")

    report = {
        "source": "CRC-PY data/train chunks",
        "source_rows": len(source_rows),
        "split_policy": "deterministic hash grouped by repo + PR (80/10/10 buckets)",
        "evidence_policy": (
            "Source line_number must map uniquely to an added line; deleted-line fallback allowed. "
            "Evidence text is copied from the parsed hunk, never generated."
        ),
        "role_rows_with_evidence_before_cap": pre_cap,
        "role_rows_after_cap": dict(retained),
        "cap_dropped_by_role": dict(cap_dropped),
        "split_counts": {split: len(rows) for split, rows in splits.items()},
        "skipped": dict(skipped),
    }
    (args.output_dir / "selection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("evidence-mapped role rows:", pre_cap)
    print("skipped:", dict(skipped))


if __name__ == "__main__":
    main()
