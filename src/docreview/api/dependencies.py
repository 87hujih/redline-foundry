"""HTTP 路由适配器的显式应用依赖。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from docreview.identity.trusted_ingress import IdentityRequest, WorkspaceScope
from docreview.runtime.lifecycle import RuntimeLifecycle
from docreview.storage.models import (
    ApprovalSummary,
    AssistantMessage,
    AssistantSession,
    Citation,
    PublicRunDetail,
    Resource,
    ResourceVersion,
    RunSummary,
    UploadedFile,
)
from docreview.turn.pipeline import Observer, PipelineRequest, PipelineResult

if TYPE_CHECKING:
    from docreview.providers.assembly import ProductionProviderDependencies
    from docreview.storage.postgres.pool import DatabasePool


class IdentityAdapter(Protocol):
    def authenticate(
        self, request: IdentityRequest, requested_workspace_id: str
    ) -> WorkspaceScope: ...


class ResourceReader(Protocol):
    async def list(self, workspace_id: str) -> list[Resource]: ...
    async def get_by_id(self, workspace_id: str, resource_id: str) -> Resource | None: ...
    async def delete(self, workspace_id: str, resource_id: str) -> bool: ...
    async def get_current_version(
        self, workspace_id: str, resource_id: str
    ) -> ResourceVersion | None: ...


class ResourceSearchReader(Protocol):
    async def search_by_resource(
        self, workspace_id: str, resource_id: str, query: str, limit: int
    ) -> list[Citation]: ...


class RunQueryReader(Protocol):
    async def list_runs(
        self, workspace_id: str, status: str, resource_id: str, limit: int
    ) -> list[RunSummary]: ...
    async def get_run(self, workspace_id: str, run_id: str) -> PublicRunDetail: ...


class ApprovalQueryReader(Protocol):
    async def list_approvals(
        self, workspace_id: str, status: str, limit: int
    ) -> list[ApprovalSummary]: ...
    async def get_approval(self, workspace_id: str, approval_id: str) -> ApprovalSummary: ...


class AssistantReader(Protocol):
    async def list_sessions(self, workspace_id: str) -> list[AssistantSession]: ...
    async def get_conversation(
        self, workspace_id: str, session_id: str
    ) -> tuple[AssistantSession, list[AssistantMessage]] | None: ...


class AssistantResourceSelection(Protocol):
    async def get_resource_selection(self, workspace_id: str, session_id: str) -> str | None: ...

    async def set_resource_selection(
        self, workspace_id: str, session_id: str, resource_id: str
    ) -> str: ...


class AssistantSessionWriter(Protocol):
    async def delete_session(self, workspace_id: str, session_id: str) -> bool: ...


class TurnPipeline(Protocol):
    async def execute(
        self, request: PipelineRequest, observer: Observer | None
    ) -> PipelineResult: ...


class ApprovalDecider(Protocol):
    async def decide_approval(self, command: Any) -> Any: ...


class UploadedFileReader(Protocol):
    async def get_by_id(self, workspace_id: str, file_id: str) -> UploadedFile | None: ...


class FileStore(Protocol):
    async def stat(self, storage_key: str) -> int | None: ...
    async def open(self, storage_key: str) -> Any: ...


class AssistantUploader(Protocol):
    async def upload_conversation(
        self,
        workspace_id: str,
        file_name: str,
        content: bytes,
        *,
        principal_type: str,
        principal_id: str,
    ) -> dict[str, object]: ...

    async def upload_session(
        self,
        workspace_id: str,
        session_id: str,
        file_name: str,
        content: bytes,
        *,
        principal_type: str,
        principal_id: str,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class CompatibilityScope:
    """由服务端持有的 scope，供未认证调用方的基础路由使用。"""

    workspace_id: str

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("必须提供兼容工作区范围")


@dataclass(frozen=True, slots=True)
class AppDependencies:
    compatibility_scope: CompatibilityScope | None = None
    identity_adapter: IdentityAdapter | None = None
    resources: ResourceReader | None = None
    resource_search: ResourceSearchReader | None = None
    run_queries: RunQueryReader | None = None
    approval_queries: ApprovalQueryReader | None = None
    assistant: AssistantReader | None = None
    assistant_resource_selection: AssistantResourceSelection | None = None
    assistant_writer: AssistantSessionWriter | None = None
    uploaded_files: UploadedFileReader | None = None
    file_store: FileStore | None = None
    upload_policy_extensions: list[str] | None = None
    upload_max_bytes: int | None = None
    assistant_uploader: AssistantUploader | None = None
    turn_pipeline: TurnPipeline | None = None
    approval_decider: ApprovalDecider | None = None
    runtime_lifecycle: RuntimeLifecycle | None = None
    providers: ProductionProviderDependencies | None = None
    database_pool: DatabasePool | None = None
    # 持久化 Runtime 组件保持显式，避免将不完整 Graph 误认为可生产装配。
    runtime_engine: object | None = None
    runtime_executor: object | None = None
    runtime_boundary: object | None = None
    projection_worker: object | None = None
    checkpointer: object | None = None
    canonical_committer: object | None = None


__all__ = ["AppDependencies", "AssistantUploader", "CompatibilityScope"]
