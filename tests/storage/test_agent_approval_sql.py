from docreview.storage.postgres.agent_queries import (
    PUBLIC_APPROVAL_DETAIL_SQL,
    PUBLIC_APPROVAL_LIST_SQL,
)


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_approval_queries_join_run_and_scope_both_sides() -> None:
    listing = normalized(PUBLIC_APPROVAL_LIST_SQL)
    detail = normalized(PUBLIC_APPROVAL_DETAIL_SQL)
    for sql in (listing, detail):
        assert "approval.workspace_id = %s" in sql
        assert "run.workspace_id = approval.workspace_id" in sql
    assert "order by approval.created_at desc, approval.id desc" in listing
    assert "limit %s" in listing


def test_approval_query_keeps_json_payload_as_public_json_only() -> None:
    sql = normalized(PUBLIC_APPROVAL_LIST_SQL + PUBLIC_APPROVAL_DETAIL_SQL)
    assert "resources_json" in sql
    assert "payload_json" in sql
    for forbidden in ("state_json", "input_json", "output_json", "context_manifest"):
        assert forbidden not in sql
