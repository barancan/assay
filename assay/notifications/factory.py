from __future__ import annotations
import os
from .base import Notifier, NoOpNotifier


def get_notifier() -> Notifier:
    if os.environ.get("ASSAY_LINEAR_API_KEY"):
        from .linear import LinearNotifier
        return LinearNotifier()
    return NoOpNotifier()
