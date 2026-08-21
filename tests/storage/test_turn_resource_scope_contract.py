from docreview.storage.postgres import turn


def compact(value: str) -> str:
    return " ".join(value.lower().split())


def turn_sql() -> str:
    return compact(
        " ".join(
            value
            for name, value in vars(turn).items()
            if name.endswith("_SQL") and isinstance(value, str)
        )
    )


def test_new_turn_acceptance_locks_session_and_resource_in_the_trusted_workspace() -> None:
    sql = turn_sql()

    assert "from assistant_sessions where id = %s and workspace_id = %s for update" in sql
    assert "from resources" in sql
    assert "where id = %s and workspace_id = %s" in sql
    assert "for key share" in sql


def test_turn_and_run_keep_the_explicit_single_resource_snapshot() -> None:
    create = compact(turn.CREATE_TURN_SQL)
    facts = compact(turn.ACCEPT_FACTS_SQL)

    assert "organization_id, workspace_id, resource_id, session_id" in create
    assert "insert into agent_runs" in facts
    assert "organization_id, workspace_id, resource_id, session_id" in facts
    assert "jsonb_build_object('turn_id', %s::text, 'resource_id', %s::text" in facts
    assert "selected_resource_id" not in create
    assert "selected_resource_id" not in facts


def test_new_session_flows_from_insert_returning_without_same_statement_update() -> None:
    facts = compact(turn.ACCEPT_FACTS_SQL)

    assert "updated_existing_session as" in facts
    assert "from inserted_message, locked_session" in facts
    assert (
        "select id from created_session union all select id from updated_existing_session" in facts
    )
