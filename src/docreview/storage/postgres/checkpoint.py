"""基于现有 artifact 表的 PostgreSQL LangGraph Checkpoint projection。

Checkpoint 是可重建的编排 projection，不是业务事实。因此适配器在
``agent_artifacts`` 中保存有界且显式标记的 JSON，而 Run/Step/Attempt/Tool/
Approval/Commit/Outbox 仍是权威来源。本模块不引入 schema 变化或独立事实源。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Protocol

from docreview.agent_graph.checkpoint import (
    AsyncCheckpointRepository,
    StoredCheckpoint,
    StoredStepResult,
    StoredWrite,
)
from docreview.runtime.codec import canonical_json, require_object

_CHECKPOINT = "langgraph_checkpoint"
_WRITE = "langgraph_write"
_STEP_RESULT = "langgraph_step_result"
_KINDS = (_CHECKPOINT, _WRITE, _STEP_RESULT)
_MAX_LIST = 1_000

INSERT_ARTIFACT_SQL = """
WITH target_run AS (
    SELECT id, workspace_id
    FROM agent_runs
    WHERE id = %s
)
INSERT INTO agent_artifacts (
    workspace_id, run_id, step_id, idempotency_key, data_classification,
    content_json, content_hash, token_count, provenance_json
)
SELECT workspace_id, id, NULL, %s, 'internal', %s::jsonb, %s, 0, '[]'::jsonb
FROM target_run
ON CONFLICT (workspace_id, idempotency_key) DO UPDATE
SET idempotency_key = agent_artifacts.idempotency_key
WHERE agent_artifacts.run_id = EXCLUDED.run_id
  AND agent_artifacts.content_hash = EXCLUDED.content_hash
  AND agent_artifacts.content_json = EXCLUDED.content_json
RETURNING id::text, content_json
"""

INSERT_WRITE_SQL = """
WITH target_run AS (
    SELECT id, workspace_id
    FROM agent_runs
    WHERE id = %s
)
INSERT INTO agent_artifacts (
    workspace_id, run_id, step_id, idempotency_key, data_classification,
    content_json, content_hash, token_count, provenance_json
)
SELECT workspace_id, id, NULL, %s, 'internal', %s::jsonb, %s, 0, '[]'::jsonb
FROM target_run
ON CONFLICT (workspace_id, idempotency_key) DO UPDATE
SET idempotency_key = agent_artifacts.idempotency_key
WHERE agent_artifacts.run_id = EXCLUDED.run_id
  AND agent_artifacts.content_hash = EXCLUDED.content_hash
  AND agent_artifacts.content_json = EXCLUDED.content_json
RETURNING id::text
"""

LOAD_ARTIFACT_SQL = """
SELECT artifact.content_json
FROM agent_artifacts AS artifact
JOIN agent_runs AS run
  ON run.id = artifact.run_id AND run.workspace_id = artifact.workspace_id
WHERE run.id = %s
  AND artifact.content_json->>'kind' = %s
  AND artifact.content_json->>'namespace' = %s
  AND (%s::text IS NULL OR artifact.content_json->>'checkpoint_id' = %s)
ORDER BY artifact.content_json->>'checkpoint_id' DESC, artifact.id DESC
LIMIT 1
"""

LIST_ARTIFACTS_SQL = """
SELECT artifact.content_json
FROM agent_artifacts AS artifact
JOIN agent_runs AS run
  ON run.id = artifact.run_id AND run.workspace_id = artifact.workspace_id
WHERE run.id = %s
  AND artifact.content_json->>'kind' = %s
  AND (%s::text IS NULL OR artifact.content_json->>'namespace' = %s)
  AND (%s::text IS NULL OR artifact.content_json->>'checkpoint_id' < %s)
ORDER BY artifact.content_json->>'checkpoint_id' DESC, artifact.id DESC
LIMIT %s
"""

LOAD_WRITES_SQL = """
SELECT artifact.content_json
FROM agent_artifacts AS artifact
JOIN agent_runs AS run
  ON run.id = artifact.run_id AND run.workspace_id = artifact.workspace_id
WHERE run.id = %s
  AND artifact.content_json->>'kind' = %s
  AND artifact.content_json->>'namespace' = %s
  AND artifact.content_json->>'checkpoint_id' = %s
ORDER BY artifact.content_json->>'task_id',
         (artifact.content_json->>'index')::integer,
         artifact.id
"""

LOAD_BY_KEY_SQL = """
SELECT artifact.content_json
FROM agent_artifacts AS artifact
JOIN agent_runs AS run
  ON run.id = artifact.run_id AND run.workspace_id = artifact.workspace_id
WHERE run.id = %s AND artifact.idempotency_key = %s
"""

RUN_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1 FROM agent_runs AS run
    WHERE run.id = %s AND run.workspace_id IS NOT NULL
)
"""

DELETE_THREAD_SQL = """
DELETE FROM agent_artifacts AS artifact
USING agent_runs AS run
WHERE run.id = %s
  AND run.id = artifact.run_id
  AND run.workspace_id = artifact.workspace_id
  AND artifact.content_json->>'kind' = ANY(%s)
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: Sequence[object] = ()) -> Any: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def fetchall(self) -> list[tuple[object, ...]]: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    def transaction(self) -> Any: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


class PostgresCheckpointRepository(AsyncCheckpointRepository):
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def save_checkpoint(self, value: StoredCheckpoint) -> None:
        payload = {
            "kind": _CHECKPOINT,
            "namespace": value.namespace,
            "checkpoint_id": value.checkpoint_id,
            "parent_checkpoint_id": value.parent_checkpoint_id,
            "checkpoint": _json_value(value.checkpoint_json, "checkpoint"),
            "metadata": _json_value(value.metadata_json, "checkpoint metadata"),
        }
        await self._put_strict(
            value.run_id,
            _key(_CHECKPOINT, value.run_id, value.namespace, value.checkpoint_id),
            payload,
            "checkpoint idempotency conflict or run not found",
        )

    async def load_checkpoint(
        self, run_id: str, namespace: str, checkpoint_id: str | None
    ) -> StoredCheckpoint | None:
        _scope(run_id, namespace, checkpoint_id)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                LOAD_ARTIFACT_SQL,
                (run_id, _CHECKPOINT, namespace, checkpoint_id, checkpoint_id),
            )
            row = await cursor.fetchone()
        return _checkpoint_for_run(run_id, _row_payload(row)) if row is not None else None

    async def list_checkpoints(
        self,
        run_id: str | None,
        namespace: str | None,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> Sequence[StoredCheckpoint]:
        if run_id is None:
            raise ValueError("生产环境 检查点 列表 需要 持久化 run_id")
        _scope(run_id, namespace or "", before_checkpoint_id)
        bounded = _MAX_LIST if limit is None else limit
        if bounded < 0:
            raise ValueError("检查点 列表 限制 不能为负数")
        bounded = min(bounded, _MAX_LIST)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                LIST_ARTIFACTS_SQL,
                (
                    run_id,
                    _CHECKPOINT,
                    namespace,
                    namespace,
                    before_checkpoint_id,
                    before_checkpoint_id,
                    bounded,
                ),
            )
            rows = await cursor.fetchall()
        return tuple(_checkpoint_for_run(run_id, _row_payload(row)) for row in rows)

    async def save_writes(self, values: Sequence[StoredWrite]) -> None:
        if not values:
            return
        run_id = values[0].run_id
        if any(value.run_id != run_id for value in values):
            raise ValueError("检查点 写入 必须属于 到 一个 持久化 运行")
        async with self._pool.connection() as connection:  # noqa: SIM117
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await _require_run(cursor, run_id)
                    for value in values:
                        _scope(value.run_id, value.namespace, value.checkpoint_id)
                        payload = {
                            "kind": _WRITE,
                            "namespace": value.namespace,
                            "checkpoint_id": value.checkpoint_id,
                            "task_id": _required(value.task_id, "检查点 task_id"),
                            "task_path": value.task_path,
                            "index": value.index,
                            "channel": _required(value.channel, "检查点通道"),
                            "value": _json_value(value.value_json, "checkpoint write"),
                        }
                        encoded = canonical_json(payload)
                        await cursor.execute(
                            INSERT_WRITE_SQL,
                            (
                                run_id,
                                _key(
                                    _WRITE,
                                    run_id,
                                    value.namespace,
                                    value.checkpoint_id,
                                    value.task_id,
                                    str(value.index),
                                ),
                                encoded,
                                _hash(encoded),
                            ),
                        )
                        if await cursor.fetchone() is None:
                            raise RuntimeError("checkpoint write idempotency conflict")

    async def load_writes(
        self, run_id: str, namespace: str, checkpoint_id: str
    ) -> Sequence[StoredWrite]:
        _scope(run_id, namespace, checkpoint_id)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                LOAD_WRITES_SQL,
                (run_id, _WRITE, namespace, checkpoint_id),
            )
            rows = await cursor.fetchall()
        return tuple(_write_for_run(run_id, _row_payload(row)) for row in rows)

    async def delete_thread(self, run_id: str) -> None:
        _required(run_id, "检查点 run_id")
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(DELETE_THREAD_SQL, (run_id, list(_KINDS)))

    async def save_step_result(self, value: StoredStepResult) -> None:
        payload = {
            "kind": _STEP_RESULT,
            "step_id": _required(value.step_id, "检查点 step_id"),
            "result": _json_value(value.result_json, "graph Step result"),
        }
        await self._put_strict(
            value.run_id,
            _key(_STEP_RESULT, value.run_id, value.step_id),
            payload,
            "图步骤结果幂等冲突或找不到运行记录",
        )

    async def load_step_result(self, run_id: str, step_id: str) -> StoredStepResult | None:
        _required(run_id, "检查点 run_id")
        _required(step_id, "检查点 step_id")
        key = _key(_STEP_RESULT, run_id, step_id)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LOAD_BY_KEY_SQL, (run_id, key))
            row = await cursor.fetchone()
        if row is None:
            return None
        payload = _row_payload(row)
        if payload.get("kind") != _STEP_RESULT or payload.get("step_id") != step_id:
            raise RuntimeError("已存储的 图 步骤 结果 绑定 无效")
        return StoredStepResult(
            run_id,
            step_id,
            canonical_json(payload.get("result")).encode(),
        )

    async def _put_strict(
        self, run_id: str, idempotency_key: str, payload: dict[str, object], error: str
    ) -> None:
        _required(run_id, "检查点 run_id")
        encoded = canonical_json(payload)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                INSERT_ARTIFACT_SQL,
                (run_id, idempotency_key, encoded, _hash(encoded)),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(error)
        stored = _row_payload(row, index=1)
        if canonical_json(stored) != encoded:
            raise RuntimeError(error)


async def _require_run(cursor: AsyncCursor, run_id: str) -> None:
    _required(run_id, "检查点 run_id")
    await cursor.execute(RUN_EXISTS_SQL, (run_id,))
    row = await cursor.fetchone()
    if row is None or len(row) != 1 or row[0] is not True:
        raise LookupError("检查点 持久化 运行 未找到")


def _checkpoint_for_run(run_id: str, payload: dict[str, object]) -> StoredCheckpoint:
    if payload.get("kind") != _CHECKPOINT:
        raise RuntimeError("已存储的 检查点 类型 无效")
    namespace = _string(payload.get("namespace"), "存储的检查点命名空间", allow_empty=True)
    checkpoint_id = _string(payload.get("checkpoint_id"), "存储的检查点 ID")
    parent = payload.get("parent_checkpoint_id")
    if parent is not None:
        parent = _string(parent, "存储的父检查点 ID")
    checkpoint = require_object(payload.get("checkpoint"), "存储的检查点")
    metadata = require_object(payload.get("metadata"), "存储的检查点元数据")
    return StoredCheckpoint(
        run_id,
        namespace,
        checkpoint_id,
        parent,
        canonical_json(checkpoint).encode(),
        canonical_json(metadata).encode(),
    )


def _write_for_run(run_id: str, payload: dict[str, object]) -> StoredWrite:
    if payload.get("kind") != _WRITE:
        raise RuntimeError("已存储的 检查点 写入 类型 无效")
    index = payload.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise RuntimeError("已存储的 检查点 写入 索引 无效")
    return StoredWrite(
        run_id,
        _string(payload.get("namespace"), "存储的检查点命名空间", allow_empty=True),
        _string(payload.get("checkpoint_id"), "存储的检查点 ID"),
        _string(payload.get("task_id"), "存储的检查点任务 ID"),
        _string(payload.get("task_path"), "存储的检查点任务路径", allow_empty=True),
        index,
        _string(payload.get("channel"), "存储的检查点通道"),
        canonical_json(payload.get("value")).encode(),
    )


def _row_payload(row: tuple[object, ...], *, index: int = 0) -> dict[str, object]:
    if len(row) <= index:
        raise RuntimeError("检查点 数据库 数据行 无效")
    value = row[index]
    if isinstance(value, str):
        value = json.loads(value)
    return require_object(value, "检查点数据库载荷")


def _json_value(raw: bytes, field: str) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{field}必须是 有效 JSON") from error


def _scope(run_id: str, namespace: str, checkpoint_id: str | None) -> None:
    _required(run_id, "检查点 run_id")
    _string(namespace, "检查点命名空间", allow_empty=True)
    if checkpoint_id is not None:
        _required(checkpoint_id, "检查点 ID")


def _required(value: object, field: str) -> str:
    return _string(value, field)


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or (not allow_empty and not value):
        raise ValueError(f"{field}无效")
    if len(value) > 500:
        raise ValueError(f"{field}超出范围")
    return value


def _key(kind: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode()).hexdigest()
    return f"langgraph:{kind}:{digest}"


def _hash(encoded: str) -> str:
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


__all__ = [
    "DELETE_THREAD_SQL",
    "INSERT_ARTIFACT_SQL",
    "LIST_ARTIFACTS_SQL",
    "LOAD_ARTIFACT_SQL",
    "PostgresCheckpointRepository",
]
