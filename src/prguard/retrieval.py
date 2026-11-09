from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .data import PROJECT_ROOT, load_examples
from .roles import ReviewRole


TOKEN_RE = re.compile(r"[a-z0-9_./-]+|[\u4e00-\u9fff]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    terms: list[str] = []
    for part in TOKEN_RE.findall(text.lower()):
        if part and "\u4e00" <= part[0] <= "\u9fff":
            if len(part) <= 2:
                terms.append(part)
            else:
                terms.extend(part[i : i + 2] for i in range(len(part) - 1))
        else:
            terms.append(part)
    return terms


def _markdown_chunks(text: str) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    title = "角色通用知识"
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if lines:
                content = "\n".join(lines).strip()
                if content:
                    chunks.append((title, content))
            title = line.lstrip("# ").strip() or "角色通用知识"
            lines = [line]
        else:
            lines.append(line)
    if lines:
        content = "\n".join(lines).strip()
        if content:
            chunks.append((title, content))
    return chunks


def retrieve_guidance(role: ReviewRole, query: str, top_k: int = 3) -> list[dict[str, str]]:
    path = PROJECT_ROOT / "data" / "knowledge" / f"{role.role_id}.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    chunks = [
        (title, content)
        for title, content in _markdown_chunks(text)
        if not any(marker in title.casefold() for marker in ("案例", "case", "经历案例"))
    ]
    if not chunks:
        return []

    query_terms = set(tokenize(query))
    scored = [
        (len(query_terms & set(tokenize(content))), index, title, content)
        for index, (title, content) in enumerate(chunks)
    ]
    matched = [item for item in scored if item[0] > 0]
    if matched:
        selected = sorted(matched, key=lambda item: (-item[0], item[1]))[:top_k]
    else:
        preferred = [
            item for item in scored
            if any(term in item[2].casefold() for term in ("专业知识", "职责范围", "角色人设"))
        ]
        selected = preferred[:top_k] or scored[:top_k]

    relative_path = str(path.relative_to(PROJECT_ROOT))
    return [
        {
            "source": f"{relative_path}#{title}",
            "content": content[:3000],
        }
        for _, _, title, content in selected
    ]


def retrieve_similar_comments(
    role: ReviewRole,
    query: str,
    *,
    top_k: int = 3,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    examples = rows if rows is not None else load_examples("train", limit=5000)
    query_terms = set(tokenize(query))
    candidates = [row for row in examples if row.get("role_id") == role.role_id]
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, row in enumerate(candidates):
        user_text = row.get("messages", [{}, {}, {}])[1].get("content", "")
        comment = row.get("source", {}).get("review_comment") or row.get("messages", [{}, {}, {}])[2].get("content", "")
        overlap = len(query_terms & set(tokenize(user_text)))
        if overlap:
            scored.append((overlap, index, {"comment": comment, **row.get("source", {})}))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:top_k]]

