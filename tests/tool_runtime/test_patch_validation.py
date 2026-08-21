# 动态 ToolRuntime fixture 刻意使用轻量 request double。
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from docreview.document.ingestion import ingest
from docreview.document.parser import DocumentParser
from docreview.document.validation import ValidationRequest, ValidationSnapshot
from docreview.tool_runtime.builtin.patch import PatchValidationBackend
from docreview.tool_runtime.builtin.registration import register_patch_validation_tool
from docreview.tool_runtime.registry import ToolRegistry


def test_patch_tool_only_calls_pure_validation_and_returns_typed_result():
    async def run() -> None:
        document = (
            await ingest(
                DocumentParser(),
                document_id="resource-1",
                version_id="version-1",
                file_name="source.md",
                content=b"# Intro\n\nBody",
            )
        ).document
        node = document.root.children[0]

        def make_request(_runtime_request, patch):
            return ValidationRequest(
                workspace_id="workspace-1",
                resource_id=document.document_id,
                principal_type="user",
                principal_id="user-1",
                idempotency_key="patch-key",
                patch=patch,
                snapshot=ValidationSnapshot(
                    workspace_id="workspace-1",
                    resource_id=document.document_id,
                    current_version_id=document.version_id,
                    document=document,
                    authorized_node_ids=frozenset({node.node_id}),
                ),
            )

        backend = PatchValidationBackend(make_request)
        request = SimpleNamespace(
            context=SimpleNamespace(request_id="request-1", resource_id="resource-1"),
            tool_input={
                "patch": {
                    "schema_version": "1.0",
                    "resource_id": "resource-1",
                    "base_version_id": "version-1",
                    "operations": [
                        {
                            "op": "replace_node",
                            "node_id": node.node_id,
                            "expected_hash": node.content_hash,
                            "content": "changed",
                        }
                    ],
                    "evidence_refs": [],
                    "reason": "update",
                }
            },
        )
        result = await backend.execute(request)  # type: ignore[arg-type]
        assert result.output["valid"] is True
        assert result.output["canonical_patch_hash"]
        assert "validated_patch" in result.output
        assert "approval" not in result.output

        failed = SimpleNamespace(
            context=request.context,
            tool_input={"patch": {**request.tool_input["patch"], "resource_id": "other"}},
        )
        failed_result = await backend.execute(failed)  # type: ignore[arg-type]
        assert failed_result.output["valid"] is False
        assert failed_result.output["errors"]

    asyncio.run(run())


def test_patch_validation_registration_is_separate_from_commit_and_approval():
    class Backend:
        async def execute(self, request):
            raise AssertionError("not executed")

        async def recover(self, request):
            return None

    registry = ToolRegistry()
    definition = register_patch_validation_tool(registry, backend=Backend())
    assert definition.name.value == "patch.validate"
    assert definition.requires_approval is False
    assert definition.side_effecting is False
