from docreview.storage.postgres import upload_write


def compact(value: str) -> str:
    return " ".join(value.lower().split())


def upload_sql() -> list[str]:
    return [
        compact(value)
        for name, value in vars(upload_write).items()
        if name.endswith("_SQL") and isinstance(value, str)
    ]


def test_successful_upload_transaction_persists_the_new_session_selection() -> None:
    selection_updates = [
        sql
        for sql in upload_sql()
        if "update assistant_sessions" in sql and "selected_resource_id" in sql
    ]

    assert len(selection_updates) == 1
    update = selection_updates[0]
    assert "resource_selected_at" in update
    assert "workspace_id = %s" in update
    assert "returning" in update


def test_upload_selection_sql_cannot_replace_the_session_workspace() -> None:
    selection_updates = [
        sql
        for sql in upload_sql()
        if "update assistant_sessions" in sql and "selected_resource_id" in sql
    ]

    assert len(selection_updates) == 1
    selection_update = selection_updates[0]

    set_clause, where_clause = selection_update.split(" where ", maxsplit=1)
    assert "workspace_id =" not in set_clause
    assert "id = %s" in where_clause
    assert "workspace_id = %s" in where_clause


def test_failed_upload_selection_update_preserves_the_existing_pointer() -> None:
    selection_updates = [
        sql
        for sql in upload_sql()
        if "update assistant_sessions" in sql and "selected_resource_id" in sql
    ]

    assert len(selection_updates) == 1
    update = selection_updates[0]
    assert "coalesce" in update or "case when" in update
    assert "selected_resource_id" in update
    assert "resource_selected_at" in update
