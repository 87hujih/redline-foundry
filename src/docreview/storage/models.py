"""Phase 2 路由使用的只读存储模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict


class CitationWindow(TypedDict, total=False):
    group_id: str
    start_order: int
    end_order: int


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    title: str
    source_type: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceVersion:
    id: str
    resource_id: str
    version_number: int
    content: str
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    resource_id: str
    section_title: str
    snippet: str
    section_id: str | None = None
    section_type: str | None = None
    window: CitationWindow | None = None


@dataclass(frozen=True, slots=True)
class SearchChunk:
    id: str
    resource_id: str
    version_id: str
    chunk_index: int
    section_title: str
    content: str
    section_id: str | None = None
    section_type: str | None = None
    chunk_role: str | None = None
    window_group_id: str | None = None
    order_in_section: int | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SearchSection:
    id: str
    resource_id: str
    version_id: str
    section_key: str
    section_type: str
    section_order: int
    title: str
    canonical_entity_name: str | None
    aliases: list[str]
    summary: str
    content: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    id: str
    workspace_id: str
    status: str
    objective: str
    step_count: int
    completed_step_count: int
    failed_step_count: int
    created_at: datetime
    updated_at: datetime
    resource_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    current_step: str | None = None
    pending_approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublicRun:
    id: str
    status: str
    objective: str
    created_at: datetime
    updated_at: datetime
    resource_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    current_step: str | None = None
    deadline_at: datetime | None = None
    cancel_requested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PublicStep:
    id: str
    step_key: str
    step_type: str
    status: str
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    next_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PublicToolCall:
    id: str
    step_id: str
    tool_name: str
    tool_version: str
    status: str
    error_category: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApprovalView:
    id: str
    run_id: str
    step_id: str
    tool_name: str
    status: str
    created_at: datetime
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApprovalSummary:
    id: str
    workspace_id: str
    run_id: str
    step_id: str
    objective: str
    tool_name: str
    tool_version: str
    reason: str
    status: str
    resources: Any
    payload: Any
    created_at: datetime
    resource_id: str | None = None
    session_id: str | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AssistantSession:
    id: str
    title: str
    web_search_enabled: bool
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    id: str
    role: str
    kind: str
    payload: Any
    sequence_no: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UploadedFile:
    id: str
    resource_id: str | None
    session_id: str | None
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PublicRunDetail:
    run: PublicRun
    steps: list[PublicStep]
    tool_calls: list[PublicToolCall]
    approvals: list[ApprovalView]
    findings: list[Finding]


__all__ = [
    "ApprovalSummary",
    "ApprovalView",
    "AssistantMessage",
    "AssistantSession",
    "Citation",
    "CitationWindow",
    "Finding",
    "PublicRun",
    "PublicRunDetail",
    "PublicStep",
    "PublicToolCall",
    "Resource",
    "ResourceVersion",
    "RunSummary",
    "SearchChunk",
    "SearchSection",
    "UploadedFile",
]
