"""Public API error envelope."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class APIError(Exception):
    status_code: int
    message: str

    def __str__(self) -> str:
        return self.message
