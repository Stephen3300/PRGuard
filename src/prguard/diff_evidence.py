from __future__ import annotations

import re
from dataclasses import asdict, dataclass


HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")


@dataclass(frozen=True)
class EvidenceLine:
    evidence_id: str
    file_path: str
    line_number: int
    side: str
    text: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


def parse_unified_diff(diff: str, default_file_path: str = "") -> list[EvidenceLine]:
    """Return only added/deleted lines, each with a stable ID within this diff."""
    candidates: list[EvidenceLine] = []
    current_path = default_file_path
    old_line: int | None = None
    new_line: int | None = None

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_path = parts[3][2:]
            old_line = None
            new_line = None
            continue
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if line.startswith("--- a/") or line.startswith(("index ", "new file mode ", "deleted file mode ")):
            continue

        hunk = HUNK_RE.match(line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            continue
        if old_line is None or new_line is None:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            candidates.append(
                EvidenceLine(
                    evidence_id=f"E{len(candidates) + 1:04d}",
                    file_path=current_path,
                    line_number=new_line,
                    side="added",
                    text=line[1:],
                )
            )
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            candidates.append(
                EvidenceLine(
                    evidence_id=f"E{len(candidates) + 1:04d}",
                    file_path=current_path,
                    line_number=old_line,
                    side="deleted",
                    text=line[1:],
                )
            )
            old_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1

    return candidates


def annotate_unified_diff(diff: str, *, min_evidence_chars: int = 8) -> str:
    """Mark selectable changed lines with the parser's stable IDs.

    The index still advances across every changed line so visible IDs remain
    identical to ``parse_unified_diff``; short/blank lines receive no marker.
    """
    output: list[str] = []
    old_line: int | None = None
    new_line: int | None = None
    evidence_index = 0
    for line in diff.splitlines():
        hunk = HUNK_RE.match(line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            output.append(line)
            continue
        if line.startswith("diff --git "):
            old_line = None
            new_line = None
            output.append(line)
            continue
        if line.startswith(("+++ b/", "--- a/", "index ", "new file mode ", "deleted file mode ")):
            output.append(line)
            continue
        if old_line is None or new_line is None or line.startswith("\\"):
            output.append(line)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            evidence_index += 1
            if len(line[1:].strip()) >= min_evidence_chars:
                output.append(f"+[E{evidence_index:04d}] {line[1:]}")
            else:
                output.append(line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            evidence_index += 1
            if len(line[1:].strip()) >= min_evidence_chars:
                output.append(f"-[E{evidence_index:04d}] {line[1:]}")
            else:
                output.append(line)
            old_line += 1
        else:
            output.append(line)
            if line.startswith(" "):
                old_line += 1
                new_line += 1
    return "\n".join(output)


def evidence_for_source_line(
    diff: str,
    file_path: str,
    line_number: int | str | None,
) -> EvidenceLine | None:
    """Map CRC-PY's review line to one unique changed line, preferring additions."""
    if line_number is None:
        return None
    try:
        wanted = int(line_number)
    except (TypeError, ValueError):
        return None

    candidates = parse_unified_diff(diff, file_path)
    added = [
        line for line in candidates
        if line.side == "added" and line.line_number == wanted
        and (not line.file_path or line.file_path == file_path)
    ]
    if len(added) == 1:
        return added[0] if len(added[0].text.strip()) >= 8 else None
    if added:
        return None
    deleted = [
        line for line in candidates
        if line.side == "deleted" and line.line_number == wanted
        and (not line.file_path or line.file_path == file_path)
    ]
    if len(deleted) == 1 and len(deleted[0].text.strip()) >= 8:
        return deleted[0]
    return None


def format_evidence_candidates(
    lines: list[EvidenceLine],
    *,
    prioritize_id: str | None = None,
    max_lines: int = 64,
) -> str:
    selected = lines
    if len(lines) > max_lines:
        prioritized = [line for line in lines if line.evidence_id == prioritize_id]
        remainder = [line for line in lines if line.evidence_id != prioritize_id]
        selected = (prioritized + remainder)[:max_lines]
    return "\n".join(
        f"{line.evidence_id} | {line.file_path}:{line.line_number} "
        f"({line.side}) | {line.text[:240]}"
        for line in selected
    )
