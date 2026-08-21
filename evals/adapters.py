from __future__ import annotations

from typing import Protocol

from .schema import EvalCase, Prediction


class PredictionAdapter(Protocol):
    """离线、API 或 staging 评测生成器需要实现的边界。"""

    async def predict(self, case: EvalCase) -> Prediction: ...


class JudgeAdapter(Protocol):
    """在确定性 PR 门禁之外可选使用的 LLM 或规则评审器。"""

    async def score(self, case: EvalCase, prediction: Prediction) -> dict[str, float]: ...
