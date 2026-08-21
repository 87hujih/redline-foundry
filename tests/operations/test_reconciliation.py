from __future__ import annotations

from typing import Any, cast

import pytest

from docreview.operations.reconciliation import (
    HISTORICAL_RECONCILIATION_SQL,
    RECONCILIATION_SQL,
    HistoricalReconciliationRepository,
    ReconciliationRepository,
)

WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


class Cursor:
    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.params: list[tuple[object, ...]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _query: str, params: tuple[object, ...]) -> None:
        self.params.append(params)

    async def fetchone(self) -> tuple[object, ...]:
        return (self.counts.pop(0),)


class Connection:
    def __init__(self, counts: list[int]) -> None:
        self.cursor_value = Cursor(counts)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def cursor(self) -> Cursor:
        return self.cursor_value


class Pool:
    def __init__(self, counts: list[int]) -> None:
        self.connection_value = Connection(counts)

    def connection(self) -> Connection:
        return self.connection_value


@pytest.mark.anyio
async def test_reconciliation_is_read_only_workspace_scoped_and_complete() -> None:
    pool = Pool([0] * len(RECONCILIATION_SQL))
    report = await ReconciliationRepository(cast(Any, pool)).reconcile(WORKSPACE_ID)

    assert report.eligible is True
    assert set(report.mismatch_counts) == set(RECONCILIATION_SQL)
    assert pool.connection_value.cursor_value.params == [
        (WORKSPACE_ID,) for _ in RECONCILIATION_SQL
    ]
    normalized = " ".join(RECONCILIATION_SQL.values()).upper()
    assert all(word not in normalized for word in ("DELETE ", "UPDATE ", "TRUNCATE", "DROP "))


@pytest.mark.anyio
async def test_reconciliation_mismatch_and_invalid_scope_fail_gate() -> None:
    report = await ReconciliationRepository(
        cast(Any, Pool([1] + [0] * (len(RECONCILIATION_SQL) - 1)))
    ).reconcile(WORKSPACE_ID)
    assert report.eligible is False
    with pytest.raises(ValueError, match="UUID"):
        await ReconciliationRepository(cast(Any, Pool([]))).reconcile("all-workspaces")


@pytest.mark.anyio
async def test_historical_reconciliation_is_global_read_only_and_blocks_on_null_scope() -> None:
    clean = await HistoricalReconciliationRepository(
        cast(Any, Pool([0] * len(HISTORICAL_RECONCILIATION_SQL)))
    ).reconcile()
    assert clean.eligible

    report = await HistoricalReconciliationRepository(
        cast(Any, Pool([1] + [0] * (len(HISTORICAL_RECONCILIATION_SQL) - 1)))
    ).reconcile()
    assert not report.eligible
    normalized = " ".join(HISTORICAL_RECONCILIATION_SQL.values()).upper()
    assert all(word not in normalized for word in ("DELETE ", "UPDATE ", "TRUNCATE", "DROP "))
