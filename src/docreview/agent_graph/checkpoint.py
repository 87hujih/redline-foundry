"""Bounded project-storage adapter for LangGraph checkpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.types import Interrupt

from docreview.agent_graph.models import StrictModel

_TYPE_KEY = "__docreview_checkpoint_type__"


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    run_id: str
    namespace: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    checkpoint_json: bytes
    metadata_json: bytes


@dataclass(frozen=True, slots=True)
class StoredWrite:
    run_id: str
    namespace: str
    checkpoint_id: str
    task_id: str
    task_path: str
    index: int
    channel: str
    value_json: bytes


@dataclass(frozen=True, slots=True)
class StoredStepResult:
    run_id: str
    step_id: str
    result_json: bytes


class CheckpointRepository(Protocol):
    def save_checkpoint(self, value: StoredCheckpoint) -> None: ...

    def load_checkpoint(
        self, run_id: str, namespace: str, checkpoint_id: str | None
    ) -> StoredCheckpoint | None: ...

    def list_checkpoints(
        self,
        run_id: str | None,
        namespace: str | None,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> Sequence[StoredCheckpoint]: ...

    def save_writes(self, values: Sequence[StoredWrite]) -> None: ...

    def load_writes(
        self, run_id: str, namespace: str, checkpoint_id: str
    ) -> Sequence[StoredWrite]: ...

    def delete_thread(self, run_id: str) -> None: ...

    def save_step_result(self, value: StoredStepResult) -> None: ...

    def load_step_result(self, run_id: str, step_id: str) -> StoredStepResult | None: ...


class InMemoryCheckpointRepository:
    """Offline repository implementing the same adapter contract as project storage."""

    def __init__(self) -> None:
        self.checkpoints: dict[tuple[str, str, str], StoredCheckpoint] = {}
        self.writes: dict[tuple[str, str, str, str, int], StoredWrite] = {}
        self.step_results: dict[tuple[str, str], StoredStepResult] = {}

    def save_checkpoint(self, value: StoredCheckpoint) -> None:
        key = (value.run_id, value.namespace, value.checkpoint_id)
        existing = self.checkpoints.get(key)
        if existing is not None and existing != value:
            raise RuntimeError("checkpoint idempotency conflict")
        self.checkpoints[key] = value

    def load_checkpoint(
        self, run_id: str, namespace: str, checkpoint_id: str | None
    ) -> StoredCheckpoint | None:
        if checkpoint_id is not None:
            return self.checkpoints.get((run_id, namespace, checkpoint_id))
        matches = [
            value
            for (thread, scope, _), value in self.checkpoints.items()
            if thread == run_id and scope == namespace
        ]
        return max(matches, key=lambda item: item.checkpoint_id) if matches else None

    def list_checkpoints(
        self,
        run_id: str | None,
        namespace: str | None,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> Sequence[StoredCheckpoint]:
        matches = [
            value
            for value in self.checkpoints.values()
            if (run_id is None or value.run_id == run_id)
            and (namespace is None or value.namespace == namespace)
            and (before_checkpoint_id is None or value.checkpoint_id < before_checkpoint_id)
        ]
        matches.sort(key=lambda item: item.checkpoint_id, reverse=True)
        return matches if limit is None else matches[: max(limit, 0)]

    def save_writes(self, values: Sequence[StoredWrite]) -> None:
        for value in values:
            key = (
                value.run_id,
                value.namespace,
                value.checkpoint_id,
                value.task_id,
                value.index,
            )
            existing = self.writes.get(key)
            if existing is not None:
                # LangGraph may retry the same task write while restoring an
                # interrupted super-step. The first durable write wins.
                continue
            self.writes[key] = value

    def load_writes(self, run_id: str, namespace: str, checkpoint_id: str) -> Sequence[StoredWrite]:
        values = [
            value
            for value in self.writes.values()
            if value.run_id == run_id
            and value.namespace == namespace
            and value.checkpoint_id == checkpoint_id
        ]
        return sorted(values, key=lambda item: (item.task_id, item.index))

    def delete_thread(self, run_id: str) -> None:
        self.checkpoints = {
            key: value for key, value in self.checkpoints.items() if value.run_id != run_id
        }
        self.writes = {key: value for key, value in self.writes.items() if value.run_id != run_id}
        self.step_results = {
            key: value for key, value in self.step_results.items() if value.run_id != run_id
        }

    def save_step_result(self, value: StoredStepResult) -> None:
        key = (value.run_id, value.step_id)
        existing = self.step_results.get(key)
        if existing is not None and existing != value:
            raise RuntimeError("graph Step result idempotency conflict")
        self.step_results[key] = value

    def load_step_result(self, run_id: str, step_id: str) -> StoredStepResult | None:
        return self.step_results.get((run_id, step_id))


class ProjectCheckpointer(BaseCheckpointSaver[int]):
    """LangGraph adapter; checkpoints are reconstructable, bounded project records."""

    def __init__(
        self,
        repository: CheckpointRepository,
        *,
        max_checkpoint_bytes: int = 512 * 1024,
        max_write_bytes: int = 256 * 1024,
    ) -> None:
        super().__init__()
        if max_checkpoint_bytes <= 0 or max_write_bytes <= 0:
            raise ValueError("checkpoint byte limits must be positive")
        self.repository = repository
        self.max_checkpoint_bytes = max_checkpoint_bytes
        self.max_write_bytes = max_write_bytes

    @staticmethod
    def _scope(
        config: RunnableConfig, *, require_checkpoint: bool = False
    ) -> tuple[str, str, str | None]:
        configurable = config.get("configurable", {})
        run_id = str(configurable.get("run_id", "")).strip()
        thread_id = str(configurable.get("thread_id", "")).strip()
        namespace = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)
        if not run_id or thread_id != run_id:
            raise ValueError("checkpoint thread_id must equal the durable run_id")
        if require_checkpoint and not checkpoint_id:
            raise ValueError("checkpoint_id is required")
        return run_id, namespace, checkpoint_id

    @staticmethod
    def _json(value: object, field: str, maximum: int) -> bytes:
        def normalize(item: object) -> object:
            if isinstance(item, Interrupt):
                return {
                    _TYPE_KEY: "langgraph_interrupt",
                    "id": item.id,
                    "value": normalize(item.value),
                }
            if isinstance(item, StrictModel):
                return normalize(item.model_dump(mode="json"))
            if item is None or isinstance(item, str | int | float | bool):
                return item
            if isinstance(item, list | tuple):
                return [normalize(child) for child in cast(Sequence[object], item)]
            if isinstance(item, dict):
                typed = cast(dict[object, object], item)
                if any(not isinstance(key, str) for key in typed):
                    raise TypeError("checkpoint object keys must be strings")
                return {cast(str, key): normalize(child) for key, child in typed.items()}
            raise TypeError(f"unsupported checkpoint value {type(item).__name__}")

        try:
            encoded = json.dumps(
                normalize(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field} must contain only bounded JSON values") from error
        if len(encoded) > maximum:
            raise ValueError(f"{field} exceeds byte limit")
        return encoded

    @staticmethod
    def _load(raw: bytes) -> Any:
        def restore(value: dict[str, Any]) -> object:
            if value.get(_TYPE_KEY) == "langgraph_interrupt" and set(value) == {
                _TYPE_KEY,
                "id",
                "value",
            }:
                return Interrupt(value=value["value"], id=str(value["id"]))
            return value

        return json.loads(raw, object_hook=restore)

    @staticmethod
    def _config(run_id: str, namespace: str, checkpoint_id: str) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": run_id,
                "run_id": run_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint_id,
            }
        }

    def _tuple(self, value: StoredCheckpoint) -> CheckpointTuple:
        checkpoint = cast(Checkpoint, self._load(value.checkpoint_json))
        metadata = cast(CheckpointMetadata, self._load(value.metadata_json))
        pending = [
            (item.task_id, item.channel, self._load(item.value_json))
            for item in self.repository.load_writes(
                value.run_id, value.namespace, value.checkpoint_id
            )
        ]
        parent = (
            self._config(value.run_id, value.namespace, value.parent_checkpoint_id)
            if value.parent_checkpoint_id
            else None
        )
        return CheckpointTuple(
            config=self._config(value.run_id, value.namespace, value.checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent,
            pending_writes=pending,
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        run_id, namespace, checkpoint_id = self._scope(config)
        value = self.repository.load_checkpoint(run_id, namespace, checkpoint_id)
        return self._tuple(value) if value else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            run_id = namespace = None
        else:
            run_id, namespace, _ = self._scope(config)
        before_id = self._scope(before)[2] if before is not None else None
        for value in self.repository.list_checkpoints(run_id, namespace, before_id, limit):
            item = self._tuple(value)
            if filter and not all(
                item.metadata.get(key) == expected for key, expected in filter.items()
            ):
                continue
            yield item

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions
        run_id, namespace, parent_id = self._scope(config)
        checkpoint_id = str(checkpoint.get("id", "")).strip()
        if not checkpoint_id:
            raise ValueError("checkpoint id is required")
        checkpoint_json = self._json(checkpoint, "checkpoint", self.max_checkpoint_bytes)
        metadata_json = self._json(metadata, "checkpoint metadata", self.max_write_bytes)
        self.repository.save_checkpoint(
            StoredCheckpoint(
                run_id,
                namespace,
                checkpoint_id,
                parent_id,
                checkpoint_json,
                metadata_json,
            )
        )
        return self._config(run_id, namespace, checkpoint_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        run_id, namespace, checkpoint_id = self._scope(config, require_checkpoint=True)
        assert checkpoint_id is not None
        values = [
            StoredWrite(
                run_id,
                namespace,
                checkpoint_id,
                task_id,
                task_path,
                WRITES_IDX_MAP.get(channel, index),
                channel,
                self._json(value, "checkpoint write", self.max_write_bytes),
            )
            for index, (channel, value) in enumerate(writes)
        ]
        self.repository.save_writes(values)

    def delete_thread(self, thread_id: str) -> None:
        self.repository.delete_thread(thread_id)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        values = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for value in values:
            yield value

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)

    def put_step_result(self, run_id: str, step_id: str, result: object) -> None:
        self.repository.save_step_result(
            StoredStepResult(
                run_id,
                step_id,
                self._json(result, "graph Step result", self.max_checkpoint_bytes),
            )
        )

    def get_step_result(self, run_id: str, step_id: str) -> object | None:
        value = self.repository.load_step_result(run_id, step_id)
        return self._load(value.result_json) if value is not None else None

    async def aput_step_result(self, run_id: str, step_id: str, result: object) -> None:
        await asyncio.to_thread(self.put_step_result, run_id, step_id, result)

    async def aget_step_result(self, run_id: str, step_id: str) -> object | None:
        return await asyncio.to_thread(self.get_step_result, run_id, step_id)


__all__ = [
    "CheckpointRepository",
    "InMemoryCheckpointRepository",
    "ProjectCheckpointer",
    "StoredCheckpoint",
    "StoredStepResult",
    "StoredWrite",
]
