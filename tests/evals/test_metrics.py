from evals.metrics import evaluate_case, summarize, summarize_by_tag
from evals.schema import EvalCase, Prediction


def test_evaluate_case_reports_quality_trajectory_scope_and_safety() -> None:
    case = EvalCase(
        "case-1",
        "q",
        ("node-1", "node-2"),
        ("关键事实",),
        ("RetrieveEvidence", "RenderOutcome"),
        ("retrieval.search",),
        ("web.search",),
        "workspace-1",
        "version-1",
        False,
        "high",
        ("中文",),
    )
    prediction = Prediction(
        case_id="case-1",
        retrieved_node_ids=("other", "node-1"),
        citations=("node-1",),
        answer="answer",
        claims=("关键事实",),
        steps=("UnderstandGoal", "RetrieveEvidence", "RenderOutcome"),
        tool_calls=("retrieval.search",),
        workspace_id="workspace-1",
        version_id="version-1",
        latency_ms=100,
        cost=0.01,
    )

    result = evaluate_case(case, prediction)

    assert result["recall_at_k"] == 0.5
    assert result["mrr"] == 0.5
    assert 0 < result["ndcg_at_k"] < 1
    assert result["citation_precision"] == 1.0
    assert result["citation_recall"] == 0.5
    assert result["trajectory_correct"] is True
    assert result["scope_correct"] is True
    assert result["safety_pass"] is True
    assert result["passed"] is False


def test_forbidden_tool_and_wrong_scope_fail_case() -> None:
    case = EvalCase(
        case_id="case-1",
        question="q",
        should_abstain=True,
        forbidden_tools=("patch.commit",),
        expected_workspace_id="workspace-1",
    )
    prediction = Prediction(
        case_id="case-1",
        answer="cannot answer",
        abstained=True,
        tool_calls=("patch.commit",),
        workspace_id="workspace-2",
    )

    result = evaluate_case(case, prediction)

    assert result["forbidden_tools_absent"] is False
    assert result["scope_correct"] is False
    assert result["safety_pass"] is False
    assert result["passed"] is False


def test_summarize_includes_latency_cost_judges_and_tags() -> None:
    result = {
        "passed": True,
        "safety_pass": True,
        "risk_level": "normal",
        "recall_at_k": 1,
        "mrr": 1,
        "ndcg_at_k": 1,
        "citation_precision": 1,
        "citation_recall": 1,
        "claim_recall": 1,
        "latency_ms": 120,
        "cost": 0.01,
        "judge_scores": {"faithfulness": 0.9},
        "tags": ["中文"],
    }
    summary = summarize([result])

    assert summary["pass_rate"] == 1.0
    assert summary["p95_latency_ms"] == 120
    assert summary["total_cost"] == 0.01
    assert summary["judge_scores"] == {"faithfulness": 0.9}
    assert summarize_by_tag([result])["中文"]["cases"] == 1
