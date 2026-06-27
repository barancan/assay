"""Notifier protocol and the no-op implementation."""
from __future__ import annotations
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    def notify(self, event: str, payload: dict) -> None: ...


class NoOpNotifier:
    def notify(self, event: str, payload: dict) -> None:
        logger.debug("assay notify [%s] payload=%r", event, payload)
