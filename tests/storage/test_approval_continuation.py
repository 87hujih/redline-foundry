from __future__ import annotations

from docreview.storage.postgres.runtime_repository import prepare_approval_continuation


def test_graph_resume_approval_continuation_is_bound_to_patch_and_commit_key() -> None:
    raw = {
        "approval_id": "approval-1",
        "status": "pending",
        "graph_request": {
            "request_id": "request-1",
            "operation": "await_approval",
            "payload": {
                "approval_id": "approval-1",
                "target_idempotency_key": "commit-key",
            },
        },
        "checkpoint_thread_id": "run-1",
        "checkpoint_step_id": "step-await",
        "graph_state": {
            "budget": {"steps_remaining": 3},
            "approval_ref": {"approval_id": "approval-1", "fact_id": "fact-1", "status": "pending"},
            "patch_ref": {
                "valid": True,
                "target_idempotency_key": "commit-key",
                "resource_id": "resource-1",
            },
        },
    }

    value = prepare_approval_continuation(raw, "approval-1", "commit-key", "approved")

    assert value["graph_resume"]["checkpoint_step_id"] == "step-await"
    response = value["graph_resume"]["response"]
    assert response["request_id"] == "request-1"
    assert response["data"]["approval"] == {
        "approval_id": "approval-1",
        "fact_id": "fact-1",
        "status": "approved",
    }
    assert value["approval_id"] == "approval-1"
    assert value["patch"]["target_idempotency_key"] == "commit-key"


def test_graph_resume_rejects_mismatched_approval_or_patch() -> None:
    raw = {
        "approval_id": "approval-1",
        "status": "pending",
        "graph_request": {
            "request_id": "request-1",
            "operation": "await_approval",
            "payload": {"approval_id": "wrong"},
        },
        "checkpoint_thread_id": "run-1",
        "checkpoint_step_id": "step-await",
        "graph_state": {},
    }

    try:
        prepare_approval_continuation(raw, "approval-1", "commit-key", "approved")
    except ValueError as error:
        assert "approval continuation" in str(error)
    else:
        raise AssertionError("mismatched graph resume was accepted")
