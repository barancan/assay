"""Requirements text -> individually addressable requirements.

Traceability starts here. Every intent the builder produces points back at one of the
ids minted below, so the coverage matrix has one bucket per requirement instead of the
single "auto" bucket everything used to collapse into.

The input is whatever a human typed: a markdown document with headings and numbered
items, a bullet list, or the wizard textarea where each line is one sentence. All three
have to yield the same shape.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*)$")
# A list marker, a numbered item, or an explicit "R3." style requirement label.
_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)]|[Rr]\d+\s*[.):])\s+")
_EXPLICIT = re.compile(r"^\s*[Rr](\d+)\s*[.):]\s+")
# A line ending in one of these is a finished thought; the next line starts a new item.
_TERMINATORS = (".", "!", "?", ":", ";")


def split_requirements(text: str) -> list[dict]:
    """Split `text` into requirements, each with a stable id.

    Returns [{"id": "R1", "text": ..., "section": ...}, ...]. Ids are R1..Rn in document
    order, unless every item carries its own unique "R<n>" label -- then those labels are
    kept, so a document that numbers R1/R2/R5 stays citable as written.
    """
    items: list[dict] = []
    section: str | None = None
    cur: dict | None = None

    def close() -> None:
        nonlocal cur
        if cur is not None:
            body = " ".join(line.strip() for line in cur["lines"]).strip()
            if body:
                cur["text"] = body
                items.append(cur)
            cur = None

    for raw in (text or "").splitlines():
        if not raw.strip():
            close()
            continue
        heading = _HEADING.match(raw)
        if heading:
            close()
            section = heading.group(1).strip().rstrip("#").strip() or None
            continue
        marker = _MARKER.match(raw)
        if marker:
            close()
            explicit = _EXPLICIT.match(raw)
            cur = {
                "label": int(explicit.group(1)) if explicit else None,
                "section": section,
                "lines": [raw[marker.end():]],
            }
            continue
        # A wrapped line: indented, or continuing a sentence that has not ended yet.
        if cur is not None and (
            raw[:1].isspace() or not cur["lines"][-1].rstrip().endswith(_TERMINATORS)
        ):
            cur["lines"].append(raw)
            continue
        close()
        cur = {"label": None, "section": section, "lines": [raw]}
    close()

    if not items:
        body = " ".join((text or "").split())
        return [{"id": "R1", "text": body, "section": None}] if body else []

    labels = [it["label"] for it in items]
    use_labels = all(n is not None for n in labels) and len(set(labels)) == len(labels)
    return [
        {
            "id": f"R{it['label']}" if use_labels else f"R{i}",
            "text": it["text"],
            "section": it["section"],
        }
        for i, it in enumerate(items, start=1)
    ]


def format_for_prompt(requirements: list[dict]) -> str:
    """Render requirements as an id-prefixed list the model must cite back."""
    lines = []
    for r in requirements:
        prefix = f"[{r['section']}] " if r.get("section") else ""
        lines.append(f"{r['id']}: {prefix}{r['text']}")
    return "\n".join(lines)
