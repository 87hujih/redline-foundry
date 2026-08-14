from __future__ import annotations

import json
from pathlib import Path

import pytest

from docreview.parity.runner import (
    CaptureSafetyError,
    compare_bundle,
    load_bundle,
    render_markdown,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "parity" / "v1"
MANIFEST = FIXTURE_ROOT / "manifest.json"
REQUIRED_CATEGORIES = {
    "approval_continuation",
    "commit_outbox",
    "decision_tool_intent",
    "error_codes",
    "evidence_citation_node_id",
    "json_dto",
    "patch_hash",
    "retry_timeout_crash_recovery",
    "routing_http_status",
    "run_step_attempt_status",
    "sse_sequence",
    "workspace_isolation",
}


def test_v1_bundle_covers_required_contracts_without_live_execution() -> None:
    bundle = load_bundle(MANIFEST)
    result = compare_bundle(bundle)

    assert result.runner_version == "1.0.0"
    assert result.fixture_version == "v1"
    assert result.failed == 0
    assert {item.category for item in result.scenarios} == REQUIRED_CATEGORIES
    assert all(
        item.verification in {"fixed_fixture", "static_contract"} for item in result.scenarios
    )
    assert bundle.go_capture.real_side_effects_executed is False
    assert bundle.python_capture.real_side_effects_executed is False


def test_comparator_reports_a_precise_json_path_for_a_difference(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    go_capture = json.loads((FIXTURE_ROOT / "go.json").read_text(encoding="utf-8"))
    python_capture = json.loads((FIXTURE_ROOT / "python.json").read_text(encoding="utf-8"))
    python_capture["scenarios"][0]["observation"]["probes"][0]["status"] = 418

    (tmp_path / "go.json").write_text(json.dumps(go_capture), encoding="utf-8")
    (tmp_path / "python.json").write_text(json.dumps(python_capture), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = compare_bundle(load_bundle(tmp_path / "manifest.json"))

    assert result.failed == 1
    assert result.scenarios[0].differences[0].path == "$.probes[0].status"
    assert result.scenarios[0].differences[0].go == 200
    assert result.scenarios[0].differences[0].python == 418


@pytest.mark.parametrize("implementation", ["go", "python"])
def test_runner_rejects_any_capture_that_executed_real_side_effects(
    tmp_path: Path, implementation: str
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for name in ("go", "python"):
        capture = json.loads((FIXTURE_ROOT / f"{name}.json").read_text(encoding="utf-8"))
        capture["real_side_effects_executed"] = name == implementation
        (tmp_path / f"{name}.json").write_text(json.dumps(capture), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CaptureSafetyError, match="real side effects"):
        load_bundle(tmp_path / "manifest.json")


def test_runner_rejects_live_capture_mode_and_path_escape(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    go_capture = json.loads((FIXTURE_ROOT / "go.json").read_text(encoding="utf-8"))
    python_capture = json.loads((FIXTURE_ROOT / "python.json").read_text(encoding="utf-8"))
    go_capture["capture_mode"] = "live"
    (tmp_path / "go.json").write_text(json.dumps(go_capture), encoding="utf-8")
    (tmp_path / "python.json").write_text(json.dumps(python_capture), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CaptureSafetyError, match="capture mode"):
        load_bundle(tmp_path / "manifest.json")

    manifest["go_capture"] = "../outside.json"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CaptureSafetyError, match="fixture directory"):
        load_bundle(tmp_path / "manifest.json")


def test_report_lists_all_blocking_release_gates() -> None:
    bundle = load_bundle(MANIFEST)
    report = render_markdown(bundle, compare_bundle(bundle))

    for gate in (
        "database-roundtrip",
        "protected-ingress",
        "production-assembly",
        "canary-capacity",
        "rollback-rehearsal",
    ):
        assert f"`{gate}`" in report
    assert "不得执行生产 canary" in report
