from docreview.storage.postgres.agent_queries import (
    PUBLIC_APPROVAL_VIEWS_SQL,
    PUBLIC_RUN_DETAIL_SQL,
    PUBLIC_RUN_FINDINGS_SQL,
    PUBLIC_RUN_LIST_SQL,
    PUBLIC_STEPS_SQL,
    PUBLIC_TOOL_CALLS_SQL,
)


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_run_list_filters_and_order_are_workspace_scoped() -> None:
    sql = normalized(PUBLIC_RUN_LIST_SQL)

    assert "run.workspace_id = %s" in sql
    assert "run.status = %s" in sql
    assert "run.resource_id = nullif(%s, '')::uuid" in sql
    assert "approval.workspace_id = %s" in sql
    assert "order by run.updated_at desc, run.id desc" in sql
    assert "limit %s" in sql


def test_all_run_detail_queries_have_workspace_and_run_predicates() -> None:
    for sql in (
        PUBLIC_RUN_DETAIL_SQL,
        PUBLIC_STEPS_SQL,
        PUBLIC_TOOL_CALLS_SQL,
        PUBLIC_APPROVAL_VIEWS_SQL,
        PUBLIC_RUN_FINDINGS_SQL,
    ):
        query = normalized(sql)
        assert "workspace_id = %s" in query or "workspace_id = scoped.workspace_id" in query
        assert "run.id = %s" in query or "id = %s" in query


def test_public_run_queries_do_not_select_sensitive_runtime_payloads() -> None:
    sql = " ".join(
        (
            PUBLIC_RUN_DETAIL_SQL,
            PUBLIC_STEPS_SQL,
            PUBLIC_TOOL_CALLS_SQL,
            PUBLIC_APPROVAL_VIEWS_SQL,
        )
    ).lower()
    for forbidden in (
        "state_json",
        "input_json",
        "output_json",
        "context_manifests",
        "agent_attempts",
        "trace_id",
        "items_json",
    ):
        assert forbidden not in sql


def test_public_findings_have_stable_ordering() -> None:
    sql = normalized(PUBLIC_RUN_FINDINGS_SQL)

    assert "order by finding_group, fact_created_at, fact_id" in sql
