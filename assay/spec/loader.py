"""Load and validate a pipeline spec from YAML."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import yaml
from .models import Spec


def load_spec(path: str | Path) -> Spec:
    raw = yaml.safe_load(Path(path).read_text())
    return Spec.model_validate(raw)


def spec_hash(spec: Spec) -> str:
    payload = json.dumps(spec.model_dump(by_alias=True), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
