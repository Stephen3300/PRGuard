from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
            if limit is not None and len(rows) >= limit:
                break
    return rows


def load_examples(split: str = "train", limit: int | None = 500) -> list[dict[str, Any]]:
    if split not in {"train", "validation", "test"}:
        raise ValueError(f"Unknown data split: {split}")
    return read_jsonl(PROCESSED_DIR / f"{split}.jsonl", limit=limit)

