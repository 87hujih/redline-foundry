import asyncio
import json
from pathlib import Path

from evals.generate import generate
from evals.schema import EvalCase, Prediction


class FakeAdapter:
    async def predict(self, case: EvalCase) -> Prediction:
        return Prediction(case_id=case.case_id, answer="generated", abstained=True)


def test_generate_writes_one_prediction_per_case(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "predictions.jsonl"
    dataset.write_text(
        '{"case_id":"case-1","question":"unknown","should_abstain":true}\n',
        encoding="utf-8",
    )

    asyncio.run(generate(dataset, output, FakeAdapter()))

    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["case_id"] == "case-1"
    assert row["abstained"] is True
