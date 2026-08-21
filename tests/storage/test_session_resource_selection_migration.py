from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_NAME = "025_assistant_session_resource_selection.sql"


def compact(value: str) -> str:
    return " ".join(value.lower().split())


def migration_path() -> Path:
    matches = [
        path
        for path in ROOT.rglob(MIGRATION_NAME)
        if not ({".runtime", ".venv", ".git"} & set(path.parts))
    ]
    assert len(matches) == 1, f"expected exactly one append-only {MIGRATION_NAME}, found {matches}"
    return matches[0]


def test_selection_migration_is_append_only_and_workspace_constrained() -> None:
    sql = compact(migration_path().read_text(encoding="utf-8"))

    assert "alter table assistant_sessions" in sql
    assert "add column selected_resource_id uuid" in sql
    assert (
        "add column resource_selected_at timestamp with time zone" in sql
        or "add column resource_selected_at timestamptz" in sql
    )
    assert "alter table resources" in sql
    assert "unique (workspace_id, id)" in sql
    assert "foreign key (workspace_id, selected_resource_id)" in sql
    assert "references resources (workspace_id, id)" in sql
    assert "on update restrict" in sql
    assert "on delete restrict" in sql
    assert "check" in sql
    assert "selected_resource_id is null" in sql
    assert "resource_selected_at is null" in sql


def test_selection_migration_does_not_rewrite_or_backfill_historical_rows() -> None:
    sql = compact(migration_path().read_text(encoding="utf-8"))

    assert "update assistant_sessions" not in sql
    assert "insert into assistant_sessions" not in sql
    assert "delete from assistant_sessions" not in sql
    assert "drop column" not in sql
    assert "drop table" not in sql
