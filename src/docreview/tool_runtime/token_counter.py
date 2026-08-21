"""带版本的保守 token 估算器。"""

from __future__ import annotations

import unicodedata


class ModelTokenEstimator:
    def __init__(self, profile: str = "docreview-estimator-v1") -> None:
        if not profile.strip() or profile != profile.strip():
            raise ValueError("令牌 估算器 配置档 为必填项")
        self.profile = profile

    def count(self, text: str) -> int:
        tokens = 0
        run_bytes = 0

        def flush() -> None:
            nonlocal tokens, run_bytes
            if run_bytes:
                tokens += (run_bytes + 3) // 4
                run_bytes = 0

        for character in text:
            category = unicodedata.category(character)
            if character.isspace():
                flush()
            elif _is_cjk(character) or category.startswith(("P", "S")):
                flush()
                tokens += 1
            else:
                run_bytes += len(character.encode("utf-8"))
        flush()
        return tokens


class JSONTokenCounter:
    def __init__(self, estimator: ModelTokenEstimator | None = None) -> None:
        self.estimator = estimator or ModelTokenEstimator()

    def count_json(self, content: bytes) -> int:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return -1
        return self.estimator.count(text)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


__all__ = ["JSONTokenCounter", "ModelTokenEstimator"]
