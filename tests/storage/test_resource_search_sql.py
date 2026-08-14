from docreview.storage.postgres.resources import (
    LIST_CHUNKS_BY_VERSION_SQL,
    LIST_SECTIONS_BY_VERSION_SQL,
    SEARCH_CHUNKS_BY_VERSION_SQL,
    SEARCH_CHUNKS_LEXICAL_BY_VERSION_SQL,
)


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_every_search_query_rechecks_workspace_and_version_scope() -> None:
    for sql in (
        SEARCH_CHUNKS_BY_VERSION_SQL,
        SEARCH_CHUNKS_LEXICAL_BY_VERSION_SQL,
        LIST_SECTIONS_BY_VERSION_SQL,
        LIST_CHUNKS_BY_VERSION_SQL,
    ):
        query = normalized(sql)
        assert "join resource_versions as version" in query
        assert "join resources as resource" in query
        assert "resource.workspace_id = %s" in query
        assert "version.id = %s" in query


def test_search_queries_bound_candidate_order_and_limit() -> None:
    assert "order by chunk.embedding <=> %s::vector" in normalized(SEARCH_CHUNKS_BY_VERSION_SQL)
    assert "limit %s" in normalized(SEARCH_CHUNKS_BY_VERSION_SQL)
    lexical = normalized(SEARCH_CHUNKS_LEXICAL_BY_VERSION_SQL)
    assert "similarity(" in lexical
    assert "similarity(" in lexical
    assert "chunk.chunk_index" in lexical
    assert "limit %s" in lexical
