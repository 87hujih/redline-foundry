"""当前生产只读 builtin Tool 的精确注册。"""

# 紧凑 schema 字面量保持稳定 fixture 结构。
# ruff: noqa: E501

from __future__ import annotations

from datetime import timedelta

from docreview.tool_runtime.models import (
    ToolDefinition,
    ToolName,
    ToolRiskLevel,
    ToolVersion,
)
from docreview.tool_runtime.registry import ToolRegistry

CURRENT_VERSION_INPUT_SCHEMA = """{"type":"object","properties":{"resource_id":{"type":"string","minLength":1}},"required":["resource_id"],"additionalProperties":false}"""
CURRENT_VERSION_OUTPUT_SCHEMA = """{"type":"object","properties":{"version":{"type":"object"}},"required":["version"],"additionalProperties":false}"""
READ_NODES_INPUT_SCHEMA = """{"type":"object","properties":{"resource_id":{"type":"string","minLength":1},"version_id":{"type":"string"},"node_ids":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":50}},"required":["resource_id","node_ids"],"additionalProperties":false}"""
NODES_OUTPUT_SCHEMA = """{"type":"object","properties":{"nodes":{"type":"array"}},"required":["nodes"],"additionalProperties":false}"""
SEARCH_NODES_INPUT_SCHEMA = """{"type":"object","properties":{"resource_id":{"type":"string","minLength":1},"version_id":{"type":"string"},"query":{"type":"string","minLength":1,"maxLength":500},"limit":{"type":"integer","minimum":1,"maximum":50}},"required":["resource_id","query","limit"],"additionalProperties":false}"""
RETRIEVAL_INPUT_SCHEMA = """{"type":"object","properties":{"resource_id":{"type":"string","minLength":1},"version_id":{"type":"string"},"include_history":{"type":"boolean"},"query":{"type":"string","minLength":1,"maxLength":500},"limit":{"type":"integer","minimum":1,"maximum":50}},"required":["resource_id","query","limit"],"additionalProperties":false}"""
ARTIFACT_READ_INPUT_SCHEMA = """{"type":"object","properties":{"artifact_id":{"type":"string","minLength":1}},"required":["artifact_id"],"additionalProperties":false}"""
ARTIFACT_OUTPUT_SCHEMA = """{"type":"object","properties":{"artifact":{"type":"object"}},"required":["artifact"],"additionalProperties":false}"""

EVIDENCE_SET_OUTPUT_SCHEMA = """{
  "type":"object",
  "properties":{
    "evidence_set":{
      "type":"object",
      "properties":{
        "schema_version":{"type":"string","const":"1.0"},
        "set_id":{"type":"string","minLength":1},
        "workspace_id":{"type":"string","minLength":1},
        "resource_id":{"type":"string","minLength":1},
        "version_id":{"type":"string","minLength":1},
        "query":{"type":"string","minLength":1},
        "query_hash":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},
        "profile_version":{"type":"string","minLength":1},
        "created_at":{"type":"string","minLength":1},
        "evidence":{
          "type":"array","maxItems":50,
          "items":{
            "type":"object",
            "properties":{
              "evidence_id":{"type":"string","minLength":1},
              "resource_id":{"type":"string","minLength":1},
              "version_id":{"type":"string","minLength":1},
              "node_id":{"type":"string","minLength":1},
              "source_type":{"type":"string","minLength":1},
              "content":{"type":"string","minLength":1},
              "content_hash":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},
              "lexical_score":{"type":"number","minimum":0,"maximum":1},
              "vector_score":{"type":"number","minimum":0,"maximum":1},
              "fused_score":{"type":"number","minimum":0,"maximum":1},
              "trust_level":{"type":"string","const":"untrusted"},
              "created_at":{"type":"string","minLength":1},
              "chunk_id":{"type":"string","minLength":1},
              "chunk_profile":{"type":"string","minLength":1},
              "window_group_id":{"type":"string","minLength":1},
              "order_in_section":{"type":"integer","minimum":1},
              "provenance":{
                "type":"object",
                "properties":{
                  "retrieval":{"type":"array","minItems":1,"maxItems":2,"items":{"type":"object","properties":{"channel":{"type":"string","enum":["lexical","semantic"]},"rank":{"type":"integer","minimum":1},"score":{"type":"number","minimum":0,"maximum":1},"index_version":{"type":"string","minLength":1}},"required":["channel","rank","score","index_version"],"additionalProperties":false}},
                  "filtering":{"type":"array","minItems":1,"items":{"type":"object","properties":{"stage":{"type":"string","minLength":1},"decision":{"type":"string","enum":["included","excluded"]},"reason":{"type":"string","minLength":1}},"required":["stage","decision","reason"],"additionalProperties":false}},
                  "fusion":{"type":"object","properties":{"algorithm":{"type":"string","enum":["weighted_sum","reciprocal_rank_fusion"]},"profile_version":{"type":"string","minLength":1},"pre_rerank_rank":{"type":"integer","minimum":1},"threshold":{"type":"number","minimum":0,"maximum":1}},"required":["algorithm","profile_version","pre_rerank_rank","threshold"],"additionalProperties":false},
                  "rerank":{"type":"object","properties":{"enabled":{"type":"boolean"},"applied":{"type":"boolean"},"profile_version":{"type":"string","minLength":1},"model":{"type":"string"},"before_rank":{"type":"integer","minimum":1},"after_rank":{"type":"integer","minimum":1},"score":{"type":"number","minimum":0,"maximum":1},"degraded_reason":{"type":"string"}},"required":["enabled","applied","profile_version","before_rank","after_rank","score"],"additionalProperties":false}
                },
                "required":["retrieval","filtering","fusion","rerank"],"additionalProperties":false
              }
            },
            "required":["evidence_id","resource_id","version_id","node_id","source_type","content","content_hash","lexical_score","vector_score","fused_score","trust_level","created_at","provenance"],
            "additionalProperties":false
          }
        },
        "process":{"type":"array","minItems":1,"items":{"type":"object","properties":{"stage":{"type":"string","enum":["recall","filter","fusion","rerank","degradation"]},"status":{"type":"string","enum":["succeeded","degraded","skipped"]},"channel":{"type":"string","enum":["lexical","semantic"]},"input_count":{"type":"integer","minimum":0},"output_count":{"type":"integer","minimum":0},"reason":{"type":"string"}},"required":["stage","status","input_count","output_count"],"additionalProperties":false}}
      },
      "required":["schema_version","set_id","workspace_id","resource_id","version_id","query","query_hash","profile_version","created_at","evidence","process"],
      "additionalProperties":false
    }
  },
  "required":["evidence_set"],
  "additionalProperties":false
}"""


def register_read_only_builtins(
    registry: ToolRegistry,
    *,
    documents: object,
    retrieval: object,
    artifacts: object,
) -> tuple[ToolDefinition, ...]:
    for name, backend in (
        ("documents", documents),
        ("retrieval", retrieval),
        ("artifacts", artifacts),
    ):
        if backend is None:
            raise ValueError(f"{name}后端 为必填项")
    definitions = (
        _definition(
            "artifact.read",
            "1.0.0",
            "Read a bounded artifact by immutable ID",
            ARTIFACT_READ_INPUT_SCHEMA,
            ARTIFACT_OUTPUT_SCHEMA,
            artifacts,
            permissions=("artifact.read",),
            resource_type="artifact",
            resource_field="artifact_id",
            max_tokens=1200,
        ),
        _definition(
            "document.get_current_version",
            "1.0.0",
            "Get document version metadata without inlining document content",
            CURRENT_VERSION_INPUT_SCHEMA,
            CURRENT_VERSION_OUTPUT_SCHEMA,
            documents,
            permissions=("document.read",),
            resource_type="document",
            resource_field="resource_id",
            max_tokens=300,
        ),
        _definition(
            "document.read_nodes",
            "1.0.0",
            "Read bounded canonical document nodes by stable node ID",
            READ_NODES_INPUT_SCHEMA,
            NODES_OUTPUT_SCHEMA,
            documents,
            permissions=("document.read",),
            resource_type="document",
            resource_field="resource_id",
            max_tokens=3000,
        ),
        _definition(
            "document.search_nodes",
            "1.0.0",
            "Search nodes within one authorized document",
            SEARCH_NODES_INPUT_SCHEMA,
            NODES_OUTPUT_SCHEMA,
            documents,
            permissions=("document.read",),
            resource_type="document",
            resource_field="resource_id",
            max_tokens=3000,
        ),
        _definition(
            "retrieval.search",
            "2.0.0",
            "Retrieve a versioned evidence set",
            RETRIEVAL_INPUT_SCHEMA,
            EVIDENCE_SET_OUTPUT_SCHEMA,
            retrieval,
            permissions=("retrieval.search", "document.read"),
            resource_type="document",
            resource_field="resource_id",
            max_tokens=4000,
            timeout=timedelta(seconds=120),
        ),
    )
    for definition in definitions:
        registry.register(definition)
    return definitions


def register_patch_validation_tool(registry: ToolRegistry, *, backend: object) -> ToolDefinition:
    """仅注册纯 ``patch.validate`` Tool。

    本注册刻意不包含 Commit 和 approval Tool，因此校验失败没有下游副作用路径。
    """
    if backend is None:
        raise ValueError("补丁校验后端为必填项")
    definition = _definition(
        "patch.validate",
        "1.0.0",
        "Validate a canonical PatchSet against an immutable snapshot",
        '{"type":"object","properties":{"resource_id":{"type":"string","minLength":1},"patch":{"type":"object"}},"required":["resource_id","patch"],"additionalProperties":false}',
        '{"type":"object","properties":{"valid":{"type":"boolean"},"errors":{"type":"array"},"validated_patch":{"type":"object"},"canonical_patch_hash":{"type":"string"},"target_resource_id":{"type":"string"},"target_version_id":{"type":"string"},"affected_node_ids":{"type":"array"},"evidence_references":{"type":"array"},"required_approval":{"type":"object"},"summary":{"type":"string"}},"required":["valid","errors"],"additionalProperties":false}',
        backend,
        permissions=("document.write",),
        resource_type="document",
        resource_field="resource_id",
        max_tokens=2_000,
    )
    registry.register(definition)
    return definition


def register_web_search_tool(registry: ToolRegistry, *, backend: object) -> ToolDefinition:
    """注册由 provider 支持且结果不可信的 Web Search 能力。"""
    if backend is None:
        raise ValueError("web 搜索 后端 为必填项")
    definition = _definition(
        "web.search",
        "1.0.0",
        "Search the configured external web provider",
        '{"type":"object","properties":{"resource_id":{"type":"string","minLength":1},"query":{"type":"string","minLength":1,"maxLength":2000},"limit":{"type":"integer","minimum":1,"maximum":20}},"required":["resource_id","query"],"additionalProperties":false}',
        '{"type":"object","properties":{"query":{"type":"string"},"provider":{"type":"string"},"items":{"type":"array"}},"required":["query","provider","items"],"additionalProperties":false}',
        backend,
        permissions=("web.search",),
        resource_type="document",
        resource_field="resource_id",
        max_tokens=2_000,
    )
    registry.register(definition)
    return definition


def register_patch_commit_tool(registry: ToolRegistry, *, backend: object) -> ToolDefinition:
    """注册需要 approval 的规范 Patch Commit 写入 Tool。"""
    if backend is None:
        raise ValueError("patch 提交 后端 为必填项")
    definition = ToolDefinition(
        name=ToolName("document.commit_patch"),
        version=ToolVersion("1.0.0"),
        description="Commit one validated canonical PatchSet atomically",
        input_schema='{"type":"object","properties":{"resource_id":{"type":"string","minLength":1},"patch":{"type":"object"}},"required":["resource_id","patch"],"additionalProperties":false}',
        output_schema='{"type":"object","properties":{"resource_id":{"type":"string"},"version_id":{"type":"string"},"outbox_id":{"type":"string"},"created":{"type":"boolean"}},"required":["resource_id","version_id","outbox_id","created"],"additionalProperties":false}',
        risk_level=ToolRiskLevel.HIGH,
        timeout=timedelta(seconds=30),
        requires_resource=True,
        requires_approval=True,
        max_inline_output_bytes=64 * 1024,
        backend=backend,
        resource_input_field="resource_id",
        max_attempts=1,
        retry_backoff=timedelta(0),
        side_effecting=True,
        max_summary_bytes=16 * 1024,
        required_permissions=("document.write",),
        resource_type="document",
        resource_access="write",
        max_result_tokens=500,
        data_classification="internal",
    )
    registry.register(definition)
    return definition


def _definition(
    name: str,
    version: str,
    description: str,
    input_schema: str,
    output_schema: str,
    backend: object,
    *,
    permissions: tuple[str, ...],
    resource_type: str,
    resource_field: str,
    max_tokens: int,
    timeout: timedelta = timedelta(seconds=10),
) -> ToolDefinition:
    return ToolDefinition(
        name=ToolName(name),
        version=ToolVersion(version),
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        risk_level=ToolRiskLevel.LOW,
        timeout=timeout,
        requires_resource=True,
        requires_approval=False,
        max_inline_output_bytes=4 * 1_024 * 1_024,
        backend=backend,
        resource_input_field=resource_field,
        max_attempts=2,
        retry_backoff=timedelta(milliseconds=100),
        side_effecting=False,
        max_summary_bytes=64 * 1_024,
        required_permissions=permissions,
        resource_type=resource_type,
        resource_access="read",
        max_result_tokens=max_tokens,
        data_classification="internal",
    )


__all__ = [
    "ARTIFACT_OUTPUT_SCHEMA",
    "ARTIFACT_READ_INPUT_SCHEMA",
    "CURRENT_VERSION_INPUT_SCHEMA",
    "CURRENT_VERSION_OUTPUT_SCHEMA",
    "EVIDENCE_SET_OUTPUT_SCHEMA",
    "NODES_OUTPUT_SCHEMA",
    "READ_NODES_INPUT_SCHEMA",
    "RETRIEVAL_INPUT_SCHEMA",
    "SEARCH_NODES_INPUT_SCHEMA",
    "register_patch_commit_tool",
    "register_patch_validation_tool",
    "register_read_only_builtins",
    "register_web_search_tool",
]
