from docreview.storage.postgres.assistant import (
    GET_CONVERSATION_SESSION_SQL,
    LIST_SESSION_MESSAGES_SQL,
    LIST_SESSIONS_SQL,
)


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_session_reads_are_exact_workspace_scoped() -> None:
    assert "workspace_id = %s" in normalized(LIST_SESSIONS_SQL)
    assert "workspace_id = %s" in normalized(GET_CONVERSATION_SESSION_SQL)
    assert "workspace_id = %s" in normalized(LIST_SESSION_MESSAGES_SQL)
    assert "order by last_message_at desc, id desc" in normalized(LIST_SESSIONS_SQL)
    assert "order by message.sequence_no asc, message.id asc" in normalized(
        LIST_SESSION_MESSAGES_SQL
    )
