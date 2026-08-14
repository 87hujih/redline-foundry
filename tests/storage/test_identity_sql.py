from docreview.storage.postgres.identity import (
    ACTIVE_MEMBERSHIP_SQL,
    RESOURCE_OWNERSHIP_SQL,
)


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_membership_query_is_active_and_exactly_workspace_scoped() -> None:
    sql = normalized(ACTIVE_MEMBERSHIP_SQL)

    assert "membership.workspace_id = %s" in sql
    assert "membership.user_id = %s" in sql
    assert "membership.status = 'active'" in sql
    assert "account.status = 'active'" in sql
    assert "workspace.status = 'active'" in sql


def test_resource_ownership_queries_bind_id_and_workspace() -> None:
    assert set(RESOURCE_OWNERSHIP_SQL) == {"document", "artifact", "task"}
    for sql in RESOURCE_OWNERSHIP_SQL.values():
        query = normalized(sql)
        assert "id = %s" in query
        assert "workspace_id = %s" in query
