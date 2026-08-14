from docreview.storage.postgres.uploaded_files import GET_UPLOADED_FILE_SQL


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_uploaded_file_read_is_workspace_scoped_through_owned_resource_or_session() -> None:
    sql = normalized(GET_UPLOADED_FILE_SQL)
    assert "uploaded.workspace_id = %s" in sql
    assert "uploaded.id = %s" in sql
    assert "resources as resource" in sql
    assert "assistant_sessions as session" in sql
    assert "resource.workspace_id = %s" in sql
    assert "session.workspace_id = %s" in sql
