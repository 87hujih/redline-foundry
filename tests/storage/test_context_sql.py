from docreview.storage.postgres.context import (
    LOAD_CONTEXT_FACTS_SQL,
    LOAD_CONTEXT_MESSAGES_SQL,
    LOAD_CONTEXT_OBSERVATIONS_SQL,
    LOAD_WINDOW_CONTEXT_CHILDREN_SQL,
)


def test_context_source_sql_is_durable_and_bounded() -> None:
    assert "run.runtime_mode = 'durable'" in LOAD_CONTEXT_FACTS_SQL
    assert "run.workspace_id IS NOT NULL" in LOAD_CONTEXT_FACTS_SQL
    assert "run.resource_id IS NOT NULL" in LOAD_CONTEXT_FACTS_SQL
    assert "step.id = %s" in LOAD_CONTEXT_FACTS_SQL
    assert "LIMIT 32" in LOAD_CONTEXT_OBSERVATIONS_SQL
    assert "WHERE run_id = %s" in LOAD_CONTEXT_OBSERVATIONS_SQL
    assert "LIMIT 16" in LOAD_CONTEXT_MESSAGES_SQL
    assert "JOIN assistant_sessions AS session" in LOAD_CONTEXT_MESSAGES_SQL
    assert "SELECT message.id, message.role, message.payload" in LOAD_CONTEXT_MESSAGES_SQL
    assert "message.created_at, message.sequence_no" in LOAD_CONTEXT_MESSAGES_SQL
    assert "message.session_id = %s" in LOAD_CONTEXT_MESSAGES_SQL
    assert "session.workspace_id = %s" in LOAD_CONTEXT_MESSAGES_SQL


def test_window_context_node_fallback_casts_uuid_before_text_coalesce() -> None:
    normalized = " ".join(LOAD_WINDOW_CONTEXT_CHILDREN_SQL.lower().split())

    assert "coalesce(chunk.canonical_node_id, chunk.id::text)" in normalized
