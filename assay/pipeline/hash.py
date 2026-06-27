from __future__ import annotations
import hashlib
import json


def content_hash(config: dict, sources: dict, rubrics: dict) -> str:
    """SHA-256 over the canonical JSON of config + sources + rubrics."""
    payload = json.dumps(
        {"config": config, "sources": sources, "rubrics": rubrics},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
