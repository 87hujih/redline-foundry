"""规范文档提交的 Serializable PostgreSQL writer。

此适配器保持窄边界：校验与 Patch 应用在进入事务前完成；事务内再通过行锁
复核当前版本，并原子写入规范 bundle、commit 事实和
``document.version.committed`` outbox 事件。
"""

# 重建持久化 AST 值时校验动态规范 JSON。
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnnecessaryCast=false

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, cast
from uuid import uuid4

from docreview.document.commit import CommitResult, CommitSnapshot, StoredCommit
from docreview.document.model import (
    Document,
    Node,
    NodeType,
    PageMapping,
    SourceLocation,
    canonical_json_bytes,
    flatten,
)
from docreview.document.patch import PatchSet, canonical_patch_bytes
from docreview.knowledge.chunking import (
    REVIEW_STRUCTURE_PROFILE,
    ChunkProjection,
    ChunkTokenizer,
    build_projection,
)
from docreview.runtime.codec import canonical_json

GET_IDEMPOTENCY_SQL = """
SELECT patch_hash, resource_id::text, new_version_id::text, outbox_event_id::text
FROM document_patch_commits
WHERE workspace_id = %s AND idempotency_key = %s
"""

ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"

LOCK_RESOURCE_SQL = """
SELECT resource.id::text, version.id::text, version.version_number
FROM resources AS resource
JOIN resource_versions AS version ON version.resource_id = resource.id
WHERE resource.id = %s AND resource.workspace_id = %s
ORDER BY version.version_number DESC
LIMIT 1
FOR UPDATE OF resource, version
"""

LOCK_NODE_HASH_SQL = """
SELECT content_hash FROM document_nodes
WHERE version_id = %s AND resource_id = %s AND workspace_id = %s AND node_id = %s
FOR UPDATE
"""

INSERT_VERSION_SQL = """
INSERT INTO resource_versions
    (id, resource_id, version_number, content, source, canonical_schema_version,
     renderer_profile, embedding_profile)
VALUES (%s, %s, %s, %s, 'agent_canonical_patch', %s, %s, %s)
RETURNING id::text
"""

INSERT_DOCUMENT_SQL = """
INSERT INTO canonical_documents
    (workspace_id, resource_id, version_id, document_id, root_node_id,
     schema_version, source_format, content_hash, ast_json, metadata_json,
     renderer_profile, chunk_profile, embedding_profile, projection_status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
        %s, %s, %s, 'pending')
"""

INSERT_NODE_SQL = """
INSERT INTO document_nodes
    (workspace_id, resource_id, version_id, node_id, parent_node_id, sibling_order,
     node_type, attributes_json, content, source_location_json, page_mapping_json,
     metadata_json, content_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb,
        %s::jsonb, %s)
"""

INSERT_SOURCE_MAPPING_SQL = """
INSERT INTO document_node_source_mappings
    (workspace_id, resource_id, version_id, node_id, mapping_order, source_json,
     page_number, start_offset, end_offset)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
"""

INSERT_SECTION_SQL = """
INSERT INTO resource_sections
    (resource_id, version_id, section_key, section_type, section_order, title,
     aliases_json, summary, content, page_start, page_end, metadata_json,
     canonical_node_id)
VALUES (%s, %s, %s, %s, %s, %s, '[]'::jsonb, %s, %s, %s, %s,
        %s::jsonb, %s)
RETURNING id::text
"""

INSERT_CHUNK_SQL = """
INSERT INTO resource_chunks
    (resource_id, version_id, chunk_index, section_title, content, embedding,
     section_id, section_type, chunk_role, window_group_id, order_in_section,
     page_start, page_end, metadata_json, canonical_node_id, content_hash,
     chunk_profile, embedding_profile, embedding_status)
VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
        %s, %s, %s, %s, 'pending')
"""

INSERT_COMMIT_SQL = """
INSERT INTO document_patch_commits
    (workspace_id, resource_id, idempotency_key, patch_hash, patch_schema_version,
     patch_json, base_version_id, new_version_id, outbox_event_id, actor_id)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
"""

INSERT_OUTBOX_SQL = """
INSERT INTO outbox_events
    (aggregate_type, aggregate_id, event_type, idempotency_key, payload_json)
VALUES ('resource', %s, 'document.version.committed', %s, %s::jsonb)
RETURNING id::text
"""

GET_CANONICAL_SQL = """
SELECT version.id::text, canonical.version_id::text,
       canonical.ast_json, canonical.content_hash
FROM resources AS resource
JOIN LATERAL (
    SELECT id FROM resource_versions
    WHERE resource_id = resource.id
    ORDER BY version_number DESC LIMIT 1
) AS version ON true
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.id = %s AND resource.workspace_id = %s
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...] = ()) -> Any: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    def transaction(self, *args: object, **kwargs: object) -> Any: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


class PostgresCommitStore:
    def __init__(
        self,
        pool: AsyncPool,
        *,
        renderer_profile: str = "canonical-v1",
        chunk_profile: str = REVIEW_STRUCTURE_PROFILE.profile_id,
        embedding_profile: str = "embedding-v1",
        tokenizer: ChunkTokenizer | None = None,
        require_exact_tokenizer: bool = False,
    ) -> None:
        if not all(value.strip() for value in (renderer_profile, chunk_profile, embedding_profile)):
            raise ValueError("规范 投影 配置档 为必填项")
        if chunk_profile != REVIEW_STRUCTURE_PROFILE.profile_id:
            raise ValueError("规范 写入器 需要 该 结构化 切块 配置档")
        self._pool = pool
        self._renderer_profile = renderer_profile
        self._chunk_profile = chunk_profile
        self._embedding_profile = embedding_profile
        self._tokenizer = tokenizer
        self._require_exact_tokenizer = require_exact_tokenizer

    async def get_commit(self, workspace_id: str, idempotency_key: str) -> StoredCommit | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_IDEMPOTENCY_SQL, (workspace_id, idempotency_key))
            row = await cursor.fetchone()
        if row is None:
            return None
        return StoredCommit(
            patch_hash=str(row[0]),
            result=CommitResult(str(row[1]), str(row[2]), str(row[3]), False),
        )

    async def load_snapshot(self, workspace_id: str, resource_id: str) -> CommitSnapshot:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_CANONICAL_SQL, (resource_id, workspace_id))
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("规范 文档 快照 未找到")
        document = _document(row[2])
        return CommitSnapshot(
            document=document,
            current_version_id=str(row[0]),
            # 授权与证据是类型化编排边界提供的请求范围
            # 事实，绝不从持有 Workspace 可见文档快照推断。
            authorized_node_ids=frozenset(),
            evidence_refs=frozenset(),
        )

    async def allocate_version_id(self, workspace_id: str, resource_id: str, digest: str) -> str:
        del workspace_id, resource_id, digest
        return str(uuid4())

    async def commit_atomic(
        self,
        *,
        workspace_id: str,
        resource_id: str,
        base_version_id: str,
        idempotency_key: str,
        patch_hash: str,
        patch: PatchSet,
        expected_hashes: dict[str, str],
        document: Document,
        actor_id: str,
    ) -> CommitResult:
        new_version_id = document.version_id
        if not new_version_id.strip():
            raise ValueError("文档 版本 id 为必填项")
        patch_json = canonical_patch_bytes(patch).decode()
        outbox_key = f"document-patch-commit:{workspace_id}:{idempotency_key}"
        projection = build_projection(
            document,
            tokenizer=self._tokenizer,
            embedding_profile=self._embedding_profile,
            require_exact_tokenizer=self._require_exact_tokenizer,
        )
        async with self._pool.connection() as connection:  # noqa: SIM117
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    # 显式使用 SERIALIZABLE，避免部署默认值削弱规范版本/提交
                    # 冲突检测。
                    await cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    await cursor.execute(
                        ADVISORY_LOCK_SQL,
                        (_advisory_lock_key(workspace_id, idempotency_key),),
                    )
                    await cursor.execute(GET_IDEMPOTENCY_SQL, (workspace_id, idempotency_key))
                    existing = await cursor.fetchone()
                    if existing is not None:
                        if str(existing[0]) != patch_hash:
                            raise RuntimeError("文档 提交 幂等 冲突")
                        return CommitResult(
                            str(existing[1]), str(existing[2]), str(existing[3]), False
                        )
                    await cursor.execute(LOCK_RESOURCE_SQL, (resource_id, workspace_id))
                    resource = await cursor.fetchone()
                    if resource is None or str(resource[1]) != base_version_id:
                        raise RuntimeError("文档 base 版本 冲突")
                    for node_id, expected_hash in sorted(expected_hashes.items()):
                        await cursor.execute(
                            LOCK_NODE_HASH_SQL,
                            (base_version_id, resource_id, workspace_id, node_id),
                        )
                        node_hash = await cursor.fetchone()
                        if node_hash is None or str(node_hash[0]) != expected_hash:
                            raise RuntimeError("document node hash conflict")
                    version_number = int(cast(int, resource[2])) + 1
                    content = _content(document)
                    await cursor.execute(
                        INSERT_VERSION_SQL,
                        (
                            new_version_id,
                            resource_id,
                            version_number,
                            content,
                            document.schema_version,
                            self._renderer_profile,
                            self._embedding_profile,
                        ),
                    )
                    await cursor.execute(
                        INSERT_DOCUMENT_SQL,
                        (
                            workspace_id,
                            resource_id,
                            new_version_id,
                            document.document_id,
                            document.root.node_id,
                            document.schema_version,
                            document.source_format,
                            document.content_hash,
                            canonical_json_bytes(document).decode(),
                            canonical_json(document.metadata),
                            self._renderer_profile,
                            self._chunk_profile,
                            self._embedding_profile,
                        ),
                    )
                    parents: dict[str, str | None] = {document.root.node_id: None}
                    for parent in flatten(document.root):
                        for child in parent.children:
                            parents[child.node_id] = parent.node_id
                        for index, node in enumerate(parent.children):
                            await cursor.execute(
                                INSERT_NODE_SQL,
                                _node_params(
                                    workspace_id,
                                    resource_id,
                                    new_version_id,
                                    node,
                                    parents[node.node_id],
                                    index,
                                ),
                            )
                    await cursor.execute(
                        INSERT_NODE_SQL,
                        _node_params(
                            workspace_id, resource_id, new_version_id, document.root, None, 0
                        ),
                    )
                    for node in flatten(document.root):
                        mappings = node.page_mapping or [None]
                        for index, page in enumerate(mappings):
                            await cursor.execute(
                                INSERT_SOURCE_MAPPING_SQL,
                                (
                                    workspace_id,
                                    resource_id,
                                    new_version_id,
                                    node.node_id,
                                    index,
                                    canonical_json_bytes(node.source_location).decode(),
                                    None if page is None else page.page,
                                    node.source_location.start_offset
                                    if page is None
                                    else page.start_offset,
                                    node.source_location.end_offset
                                    if page is None
                                    else page.end_offset,
                                ),
                            )
                    section_ids = await _insert_sections(
                        cursor, resource_id, new_version_id, projection
                    )
                    await _insert_chunks(
                        cursor,
                        resource_id,
                        new_version_id,
                        projection,
                        section_ids,
                        self._chunk_profile,
                        self._embedding_profile,
                    )
                    await cursor.execute(
                        INSERT_OUTBOX_SQL,
                        (
                            resource_id,
                            outbox_key,
                            canonical_json(
                                {
                                    "workspace_id": workspace_id,
                                    "resource_id": resource_id,
                                    "base_version_id": base_version_id,
                                    "version_id": new_version_id,
                                    "content_hash": document.content_hash,
                                    "embedding_profile": self._embedding_profile,
                                    "projection_status": "pending",
                                }
                            ),
                        ),
                    )
                    outbox = await cursor.fetchone()
                    if outbox is None:
                        raise RuntimeError("文档 提交 发件箱 写入 未返回数据行")
                    outbox_id = str(outbox[0])
                    await cursor.execute(
                        INSERT_COMMIT_SQL,
                        (
                            workspace_id,
                            resource_id,
                            idempotency_key,
                            patch_hash,
                            patch.schema_version,
                            patch_json,
                            base_version_id,
                            new_version_id,
                            outbox_id,
                            actor_id,
                        ),
                    )
        return CommitResult(resource_id, new_version_id, outbox_id, True)


def _content(document: Document) -> str:
    return "\n\n".join(
        node.content.strip() for node in flatten(document.root) if node.content.strip()
    )


async def insert_canonical_projection(
    cursor: AsyncCursor,
    *,
    workspace_id: str,
    resource_id: str,
    version_id: str,
    document: Document,
    projection: ChunkProjection,
    renderer_profile: str,
    chunk_profile: str,
    embedding_profile: str,
) -> None:
    """Write one already-versioned canonical bundle inside the caller transaction."""

    await cursor.execute(
        INSERT_DOCUMENT_SQL,
        (
            workspace_id,
            resource_id,
            version_id,
            document.document_id,
            document.root.node_id,
            document.schema_version,
            document.source_format,
            document.content_hash,
            canonical_json_bytes(document).decode(),
            canonical_json(document.metadata),
            renderer_profile,
            chunk_profile,
            embedding_profile,
        ),
    )
    parents: dict[str, str | None] = {document.root.node_id: None}
    for parent in flatten(document.root):
        for child in parent.children:
            parents[child.node_id] = parent.node_id
        for index, node in enumerate(parent.children):
            await cursor.execute(
                INSERT_NODE_SQL,
                _node_params(
                    workspace_id,
                    resource_id,
                    version_id,
                    node,
                    parents[node.node_id],
                    index,
                ),
            )
    await cursor.execute(
        INSERT_NODE_SQL,
        _node_params(workspace_id, resource_id, version_id, document.root, None, 0),
    )
    for node in flatten(document.root):
        mappings = node.page_mapping or [None]
        for index, page in enumerate(mappings):
            await cursor.execute(
                INSERT_SOURCE_MAPPING_SQL,
                (
                    workspace_id,
                    resource_id,
                    version_id,
                    node.node_id,
                    index,
                    canonical_json_bytes(node.source_location).decode(),
                    None if page is None else page.page,
                    node.source_location.start_offset if page is None else page.start_offset,
                    node.source_location.end_offset if page is None else page.end_offset,
                ),
            )
    section_ids = await _insert_sections(cursor, resource_id, version_id, projection)
    await _insert_chunks(
        cursor,
        resource_id,
        version_id,
        projection,
        section_ids,
        chunk_profile,
        embedding_profile,
    )


def _node_params(
    workspace_id: str, resource_id: str, version_id: str, node: Node, parent: str | None, order: int
) -> tuple[object, ...]:
    return (
        workspace_id,
        resource_id,
        version_id,
        node.node_id,
        parent,
        order,
        node.type.value,
        canonical_json(node.attributes),
        node.content,
        canonical_json_bytes(node.source_location).decode(),
        canonical_json_bytes(node.page_mapping).decode(),
        canonical_json(node.metadata),
        node.content_hash,
    )


def _advisory_lock_key(workspace_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{workspace_id}\0{idempotency_key}".encode()).hexdigest()


async def _insert_sections(
    cursor: AsyncCursor, resource_id: str, version_id: str, projection: ChunkProjection
) -> dict[str, str]:
    ids: dict[str, str] = {}
    for section in projection.sections:
        await cursor.execute(
            INSERT_SECTION_SQL,
            (
                resource_id,
                version_id,
                section.section_key,
                section.section_type,
                section.section_order,
                section.title,
                section.summary,
                section.content,
                section.page_start,
                section.page_end,
                canonical_json(section.metadata),
                section.canonical_node_id,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("规范 章节 写入 未返回数据行")
        ids[section.section_key] = str(row[0])
    return ids


async def _insert_chunks(
    cursor: AsyncCursor,
    resource_id: str,
    version_id: str,
    projection: ChunkProjection,
    section_ids: dict[str, str],
    chunk_profile: str,
    embedding_profile: str,
) -> None:
    for chunk in projection.chunks:
        section_id = section_ids.get(chunk.section_key)
        if section_id is None:
            raise RuntimeError("规范 切块 章节 缺失")
        page_start = chunk.page_start if chunk.page_start is not None else 0
        page_end = chunk.page_end if chunk.page_end is not None else page_start
        await cursor.execute(
            INSERT_CHUNK_SQL,
            (
                resource_id,
                version_id,
                chunk.chunk_index,
                chunk.section_title,
                chunk.content,
                section_id,
                chunk.section_type,
                chunk.chunk_role,
                chunk.window_group_id,
                chunk.order_in_section,
                page_start,
                page_end,
                canonical_json(chunk.metadata),
                chunk.node_id,
                chunk.content_hash,
                chunk_profile,
                embedding_profile,
            ),
        )


def _document(value: object) -> Document:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("规范 ast_json 必须是对象")
    raw = cast(dict[str, object], value)
    return Document(
        document_id=str(raw["document_id"]),
        version_id=str(raw["version_id"]),
        root=_node(cast(dict[str, object], raw["root"])),
        source_format=str(raw["source_format"]),
        metadata=cast(dict[str, object], raw.get("metadata", {})),
        content_hash=str(raw.get("content_hash", "")),
        schema_version=str(raw.get("schema_version", "1.0")),
    )


def _node(value: dict[str, object]) -> Node:
    source = cast(dict[str, object], value["source_location"])
    pages = cast(list[object], value.get("page_mapping", []))
    return Node(
        node_id=str(value["node_id"]),
        type=NodeType(str(value["type"])),
        attributes=cast(dict[str, object], value.get("attributes", {})),
        content=str(value.get("content", "")),
        children=[
            _node(cast(dict[str, object], child))
            for child in cast(list[object], value.get("children", []))
        ],
        source_location=SourceLocation(
            str(source["file_name"]),
            _integer(source["start_offset"]),
            _integer(source["end_offset"]),
            _integer(source.get("start_line", 0)),
            _integer(source.get("end_line", 0)),
        ),
        page_mapping=[
            PageMapping(
                _integer(cast(dict[str, object], page)["page"]),
                _integer(cast(dict[str, object], page)["start_offset"]),
                _integer(cast(dict[str, object], page)["end_offset"]),
            )
            for page in pages
        ],
        metadata=cast(dict[str, object], value.get("metadata", {})),
        content_hash=str(value.get("content_hash", "")),
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("规范 AST 整数 字段 无效")
    return value


__all__ = ["PostgresCommitStore", "insert_canonical_projection"]
