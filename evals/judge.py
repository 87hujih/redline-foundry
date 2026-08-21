from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

from .adapters import JudgeAdapter
from .run import read_jsonl
from .schema import EvalCase, Prediction
from .validation import validate_dataset, validate_predictions


def _load_judge(reference: str) -> JudgeAdapter:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("judge must use module:attribute syntax")
    module = importlib.import_module(module_name)
    value: Any = getattr(module, attribute)
    judge = value() if isinstance(value, type) else value
    if not callable(getattr(judge, "score", None)):
        raise TypeError("judge must provide async score(case, prediction)")
    return cast(JudgeAdapter, judge)


async def judge_predictions(
    dataset: Path,
    predictions_path: Path,
    output: Path,
    judge: JudgeAdapter,
) -> None:
    cases = [EvalCase.from_dict(row) for row in read_jsonl(dataset)]
    predictions = [Prediction.from_dict(row) for row in read_jsonl(predictions_path)]
    validate_dataset(cases)
    validate_predictions(cases, predictions)
    cases_by_id = {case.case_id: case for case in cases}
    judged: list[Prediction] = []
    for prediction in predictions:
        scores = await judge.score(cases_by_id[prediction.case_id], prediction)
        if any(not 0 <= value <= 1 for value in scores.values()):
            raise ValueError("judge scores must be between 0 and 1")
        judged.append(replace(prediction, judge_scores={**prediction.judge_scores, **scores}))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(
            json.dumps(asdict(prediction), ensure_ascii=True, separators=(",", ":")) + "\n"
            for prediction in judged
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="为评测预测结果添加评审分数")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--judge", required=True, help="Python 模块:属性")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(
        judge_predictions(
            args.dataset,
            args.predictions,
            args.output,
            _load_judge(args.judge),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
