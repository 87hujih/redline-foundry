"""Approval 后端与持久化 continuation 契约。"""

from docreview.approval.backend import (
    ApprovalBackend,
    ApprovalConflictError,
    ApprovalValidationError,
    InMemoryApprovalRepository,
    build_continuation,
    continuation_step_id,
)
from docreview.approval.models import (
    Approval,
    ApprovalBinding,
    ApprovalCreateCommand,
    ApprovalDecisionCommand,
    ApprovalStatus,
    MembershipFact,
    OutboxFact,
    PatchFact,
    Principal,
    ResourceFact,
    RunFact,
    StepFact,
)

__all__ = [
    "Approval",
    "ApprovalBackend",
    "ApprovalBinding",
    "ApprovalConflictError",
    "ApprovalCreateCommand",
    "ApprovalDecisionCommand",
    "ApprovalStatus",
    "ApprovalValidationError",
    "InMemoryApprovalRepository",
    "MembershipFact",
    "OutboxFact",
    "PatchFact",
    "Principal",
    "ResourceFact",
    "RunFact",
    "StepFact",
    "build_continuation",
    "continuation_step_id",
]
