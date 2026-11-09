from __future__ import annotations

import argparse
import hashlib
import json
import html
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "crc-py-dataset" / "data"
OUT_DIR = ROOT / "data" / "processed"
sys.path.insert(0, str(ROOT / "src"))
from prguard.roles import role_by_id, system_prompt_for_role  # noqa: E402

COMMENT_ROLE_BY_CATEGORY = {
    "functional": "functional_reviewer",
    "refactoring": "refactoring_reviewer",
    "documentation": "documentation_reviewer",
    "discussion": "requirements_reviewer",
}

FENCE = chr(96) * 3
SUGGESTION_RE = re.compile(re.escape(FENCE) + r"suggestion\s*[\s\S]*?" + re.escape(FENCE), re.IGNORECASE)
DETAILS_RE = re.compile(r"<details\b[^>]*>[\s\S]*?</details\s*>", re.IGNORECASE)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
ARTIFACT_RE = re.compile(
    r"prompt to fix with ai|this is a comment left during a code review",
    re.IGNORECASE,
)

def normalize_category(value: Any) -> str:
    return re.sub(r"\s+", "_", str(value or "").strip().lower())


def normalize_review_comment(value: Any) -> str:
    """Keep reviewer prose while removing suggestion blocks and repeated markup noise."""
    raw = html.unescape(str(value or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw or ARTIFACT_RE.search(raw):
        return ""
    text = DETAILS_RE.sub(" ", raw)
    text = SUGGESTION_RE.sub(" ", text)
    text = IMAGE_RE.sub(" ", text)
    text = LINK_RE.sub(r"\1", text)
    text = HTML_TAG_RE.sub(" ", text)

    paragraphs: list[str] = []
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", text):
        lines = []
        for line in block.splitlines():
            line = re.sub(r"^\s*>\s?", "", line)
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        paragraph = " ".join(lines).strip()
        normalized_key = paragraph.casefold()
        if paragraph and normalized_key not in seen:
            seen.add(normalized_key)
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs).strip()


def load_source_rows() -> list[dict[str, Any]]:
    chunk_files = sorted((RAW_DIR / "train").glob("chunk_*.json"))
    if not chunk_files:
        chunk_files = sorted(RAW_DIR.glob("chunk_*.json"))
    if not chunk_files:
        raise FileNotFoundError(
            f"No CRC-PY train chunks found under {RAW_DIR}. Run scripts/download_data.ps1 first."
        )
    rows: list[dict[str, Any]] = []
    for path in chunk_files:
        text = path.read_text(encoding="utf-8").lstrip()
        # The public repository's current train chunk files begin with an
        # object but retain a closing array bracket. Repair only that wrapper.
        if text.startswith("{") and text.rstrip().endswith("]"):
            chunk = json.loads("[" + text)
        else:
            chunk = json.loads(text)
        if not isinstance(chunk, list):
            raise ValueError(f"Expected a JSON list in {path}")
        rows.extend(chunk)
    return rows


def group_hash(row: dict[str, Any]) -> int:
    key = f"{row.get('repo', '')}:{row.get('pr_number', '')}"
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100


def make_examples(
    row: dict[str, Any],
    min_comment_chars: int,
    *,
    structured_dialogues: bool = False,
) -> list[dict[str, Any]]:
    category = normalize_category(row.get("category"))
    code = str(row.get("code") or "").strip()
    raw_comment = str(row.get("comment") or "").strip()
    comment = normalize_review_comment(raw_comment)
    if not code or len(comment) < min_comment_chars:
        return []

    repo = str(row.get("repo", ""))
    file_path = str(row.get("file_path", ""))
    source = {
        "repo": repo,
        "pr_number": str(row.get("pr_number", "")),
        "comment_id": str(row.get("comment_id", "")),
        "file_path": file_path,
        "line_number": row.get("line_number"),
        "raw_review_comment": raw_comment[:1200],
        "review_comment": comment[:1200],
    }
    examples: list[dict[str, Any]] = []

    normalized_path = file_path.lower().replace("\\", "/")
    basename = normalized_path.rsplit("/", 1)[-1]
    is_test_file = (
        "/test/" in normalized_path
        or "/tests/" in normalized_path
        or "/spec/" in normalized_path
        or normalized_path.startswith(("test/", "tests/"))
        or ".spec." in normalized_path
        or "_test." in normalized_path
        or basename.startswith("test_")
        or normalized_path.endswith("_test.py")
    )
    role_id = "test_reviewer" if is_test_file else COMMENT_ROLE_BY_CATEGORY.get(category)
    if not role_id:
        # CRC-PY has too few false-positive items in its training split to
        # train a dedicated false-positive persona responsibly.
        return []
    user_content = (
        f"Repository: {repo}\nFile: {file_path}\n"
        f"Review this code change.\nDiff hunk:\n{code[:2500]}"
    )
    assistant_content = comment[:1200]
    system_content = (
        system_prompt_for_role(role_by_id(role_id))
        if structured_dialogues
        else (
            f"You review code changes as {role_id}. "
            "Write one concise, respectful, actionable peer-review comment. "
            "Do not invent facts or claim tests ran."
        )
    )
    if structured_dialogues:
        assistant_content = json.dumps(
            {"role_id": role_id, "review_comment": assistant_content},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    examples.append(
        {
            "role_id": role_id,
            "category": category,
            "source": source,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ],
        }
    )
    return examples


def split_group(row: dict[str, Any]) -> str:
    bucket = group_hash(row)
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "validation"
    return "test"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-role", type=int, default=500)
    parser.add_argument("--min-comment-chars", type=int, default=24)
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR)
    parser.add_argument(
        "--structured-dialogues",
        action="store_true",
        help="Wrap the verbatim human comment in the shared role/review_comment JSON contract.",
    )
    args = parser.parse_args()

    source_rows = load_source_rows()
    dedup: set[tuple[str, str, str]] = set()
    by_role: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    skipped = Counter()

    for row in source_rows:
        code = str(row.get("code") or "").strip()
        comment = str(row.get("comment") or "").strip()
        if not code:
            skipped["empty_code"] += 1
            continue
        if len(comment) < args.min_comment_chars:
            skipped["comment_below_minimum"] += 1
            continue
        if len(normalize_review_comment(comment)) < args.min_comment_chars:
            skipped["comment_removed_by_sanitization"] += 1
            continue
        examples = make_examples(
            row,
            args.min_comment_chars,
            structured_dialogues=args.structured_dialogues,
        )
        if not examples:
            skipped["unknown_role_or_category"] += 1
            continue
        for sample in examples:
            key = (
                sample["role_id"],
                sample["messages"][1]["content"],
                sample["messages"][2]["content"],
            )
            if key in dedup:
                skipped["duplicate"] += 1
                continue
            dedup.add(key)
            by_role[sample["role_id"]].append((row, sample))

    split_rows: dict[str, list[dict[str, Any]]] = {
        "train": [], "validation": [], "test": []
    }
    role_counts = Counter()
    pre_cap_counts = {role_id: len(pairs) for role_id, pairs in by_role.items()}
    cap_dropped = Counter()
    for role_id, pairs in by_role.items():
        pairs.sort(
            key=lambda pair: hashlib.sha256(
                f"{pair[1]['source']['repo']}:{pair[1]['source']['pr_number']}:{pair[1]['source']['comment_id']}".encode("utf-8")
            ).hexdigest()
        )
        if args.max_per_role > 0:
            cap_dropped[role_id] = max(0, len(pairs) - args.max_per_role)
            pairs = pairs[: args.max_per_role]
        for source, sample in pairs:
            split_rows[split_group(source)].append(sample)
            role_counts[role_id] += 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for split, rows in split_rows.items():
        output = args.output_dir / f"{split}.jsonl"
        with output.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{split}: {len(rows)} -> {output}")

    report = {
        "source_rows": len(source_rows),
        "skipped": dict(skipped),
        "rows_after_dedup_before_cap": sum(pre_cap_counts.values()),
        "duplicate_rows_removed": skipped.get("duplicate", 0),
        "role_rows_before_cap": pre_cap_counts,
        "cap_dropped_by_role": dict(cap_dropped),
        "role_rows_after_cap": dict(role_counts),
        "split_counts": {split: len(rows) for split, rows in split_rows.items()},
        "split_policy": "deterministic hash grouped by repo + PR (80/10/10 buckets)",
        "structured_dialogues": args.structured_dialogues,
    }
    (args.output_dir / "selection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("source rows:", len(source_rows))
    print("role rows:", dict(role_counts))
    print("skipped:", dict(skipped))
    print("pre-cap role rows:", pre_cap_counts)
    print("cap dropped:", dict(cap_dropped))
    print("split policy: deterministic hash grouped by repo + PR")


if __name__ == "__main__":
    main()

