import asyncio
import json
from pathlib import Path

from evals.judge import judge_predictions
from evals.schema import EvalCase, Prediction


class FakeJudge:
    async def score(self, case: EvalCase, prediction: Prediction) -> dict[str, float]:
        assert case.case_id == prediction.case_id
        return {"faithfulness": 0.9}


def test_judge_enriches_predictions_without_replacing_existing_scores(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    output = tmp_path / "judged.jsonl"
    dataset.write_text(
        '{"case_id":"case-1","question":"unknown","should_abstain":true}\n',
        encoding="utf-8",
    )
    predictions.write_text(
        '{"case_id":"case-1","answer":"unknown","abstained":true,'
        '"judge_scores":{"relevance":1}}\n',
        encoding="utf-8",
    )

    asyncio.run(judge_predictions(dataset, predictions, output, FakeJudge()))

    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["judge_scores"] == {"relevance": 1.0, "faithfulness": 0.9}
