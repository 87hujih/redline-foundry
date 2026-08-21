from docreview.storage.postgres.resources import (
    CLEAR_RESOURCE_SELECTIONS_SQL,
    DELETE_RESOURCE_SQL,
    GET_CURRENT_VERSION_SQL,
    GET_RESOURCE_SQL,
    LIST_RESOURCES_SQL,
)


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_every_resource_query_is_workspace_scoped_and_stably_ordered() -> None:
    listing = normalized(LIST_RESOURCES_SQL)
    detail = normalized(GET_RESOURCE_SQL)
    version = normalized(GET_CURRENT_VERSION_SQL)

    assert "where workspace_id = %s" in listing
    assert "order by created_at desc" in listing
    assert "where id = %s and workspace_id = %s" in detail
    assert "resource.id = version.resource_id" in version
    assert "resource.workspace_id = %s" in version
    assert "version.version_number desc" in version


def test_delete_clears_session_selection_and_is_workspace_scoped() -> None:
    clear_selection = normalized(CLEAR_RESOURCE_SELECTIONS_SQL)
    deletion = normalized(DELETE_RESOURCE_SQL)

    assert "selected_resource_id = null" in clear_selection
    assert "resource_selected_at = null" in clear_selection
    assert "where workspace_id = %s and selected_resource_id = %s" in clear_selection
    assert "where id = %s and workspace_id = %s" in deletion
