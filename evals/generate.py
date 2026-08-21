from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from .adapters import PredictionAdapter
from .run import read_jsonl
from .schema import EvalCase
from .validation import validate_dataset


def _load_adapter(reference: str) -> PredictionAdapter:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("adapter must use module:attribute syntax")
    module = importlib.import_module(module_name)
    value: Any = getattr(module, attribute)
    adapter = value() if isinstance(value, type) else value
    if not callable(getattr(adapter, "predict", None)):
        raise TypeError("adapter must provide async predict(case)")
    return cast(PredictionAdapter, adapter)


async def generate(dataset: Path, output: Path, adapter: PredictionAdapter) -> None:
    cases = [EvalCase.from_dict(row) for row in read_jsonl(dataset)]
    validate_dataset(cases)
    predictions = [await adapter.predict(case) for case in cases]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(
            json.dumps(asdict(prediction), ensure_ascii=True, separators=(",", ":")) + "\n"
            for prediction in predictions
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DocReview 评测预测结果")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--adapter", required=True, help="Python 模块:属性")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(generate(args.dataset, args.output, _load_adapter(args.adapter)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
