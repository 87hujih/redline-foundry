from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from docreview.tool_runtime import ToolName, ToolRegistry, ToolRiskLevel, ToolVersion
from docreview.tool_runtime.builtin.registration import register_read_only_builtins


class Backend:
    async def execute(self, request: object) -> object:
        raise AssertionError("execution was not expected")

    async def recover(self, request: object) -> object:
        return None


EXPECTED = {
    ("artifact.read", "1.0.0"): {
        "description": "Read a bounded artifact by immutable ID",
        "input": {
            "type": "object",
            "properties": {"artifact_id": {"type": "string", "minLength": 1}},
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        "output": {
            "type": "object",
            "properties": {"artifact": {"type": "object"}},
            "required": ["artifact"],
            "additionalProperties": False,
        },
        "permission": ("artifact.read",),
        "resource_type": "artifact",
        "resource_field": "artifact_id",
        "max_tokens": 1200,
    },
    ("document.get_current_version", "1.0.0"): {
        "description": "Get document version metadata without inlining document content",
        "input": {
            "type": "object",
            "properties": {"resource_id": {"type": "string", "minLength": 1}},
            "required": ["resource_id"],
            "additionalProperties": False,
        },
        "output": {
            "type": "object",
            "properties": {"version": {"type": "object"}},
            "required": ["version"],
            "additionalProperties": False,
        },
        "permission": ("document.read",),
        "resource_type": "document",
        "resource_field": "resource_id",
        "max_tokens": 300,
    },
    ("document.read_nodes", "1.0.0"): {
        "description": "Read bounded canonical document nodes by stable node ID",
        "input": {
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "minLength": 1},
                "version_id": {"type": "string"},
                "node_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                },
            },
            "required": ["resource_id", "node_ids"],
            "additionalProperties": False,
        },
        "output": {
            "type": "object",
            "properties": {"nodes": {"type": "array"}},
            "required": ["nodes"],
            "additionalProperties": False,
        },
        "permission": ("document.read",),
        "resource_type": "document",
        "resource_field": "resource_id",
        "max_tokens": 3000,
    },
    ("document.search_nodes", "1.0.0"): {
        "description": "Search nodes within one authorized document",
        "input": {
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "minLength": 1},
                "version_id": {"type": "string"},
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["resource_id", "query", "limit"],
            "additionalProperties": False,
        },
        "output": {
            "type": "object",
            "properties": {"nodes": {"type": "array"}},
            "required": ["nodes"],
            "additionalProperties": False,
        },
        "permission": ("document.read",),
        "resource_type": "document",
        "resource_field": "resource_id",
        "max_tokens": 3000,
    },
    ("retrieval.search", "2.0.0"): {
        "description": "Retrieve a versioned evidence set",
        "input": {
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "minLength": 1},
                "version_id": {"type": "string"},
                "include_history": {"type": "boolean"},
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["resource_id", "query", "limit"],
            "additionalProperties": False,
        },
        "permission": ("retrieval.search", "document.read"),
        "resource_type": "document",
        "resource_field": "resource_id",
        "max_tokens": 4000,
    },
}


def test_registration_matches_exact_read_only_descriptors_and_schemas() -> None:
    registry = ToolRegistry()
    backend = Backend()

    definitions = register_read_only_builtins(
        registry,
        documents=backend,
        retrieval=backend,
        artifacts=backend,
    )

    assert {(item.name.value, item.version.value) for item in definitions} == set(EXPECTED)
    for identity, expected in EXPECTED.items():
        definition = registry.resolve(ToolName(identity[0]), ToolVersion(identity[1]))
        assert definition.description == expected["description"]
        assert json.loads(definition.input_schema) == expected["input"]
        if "output" in expected:
            assert json.loads(definition.output_schema) == expected["output"]
        assert definition.risk_level is ToolRiskLevel.LOW
        assert definition.required_permissions == expected["permission"]
        assert definition.resource_type == expected["resource_type"]
        assert definition.resource_input_field == expected["resource_field"]
        assert definition.resource_access == "read"
        expected_timeout = 120 if identity == ("retrieval.search", "2.0.0") else 10
        assert definition.timeout == timedelta(seconds=expected_timeout)
        assert definition.max_attempts == 2
        assert definition.retry_backoff == timedelta(milliseconds=100)
        assert definition.max_result_tokens == expected["max_tokens"]
        assert definition.data_classification == "internal"

    for absent in (
        "artifact.write",
        "patch.validate",
        "patch.commit",
        "web.search",
        "workflow.request_approval",
    ):
        with pytest.raises(LookupError):
            registry.resolve(ToolName(absent), ToolVersion("1.0.0"))

    retrieval = registry.resolve(ToolName("retrieval.search"), ToolVersion("2.0.0"))
    canonical_output = json.dumps(
        json.loads(retrieval.output_schema), sort_keys=True, separators=(",", ":")
    ).encode()
    assert len(canonical_output) == 3979
    assert hashlib.sha256(canonical_output).hexdigest() == (
        "ab96f8684449d988000788b4153dc9eb1a04d18b8b4d89f514d0d1a1426f3caa"
    )


@pytest.mark.parametrize("missing", ["documents", "retrieval", "artifacts"])
def test_registration_fails_closed_when_a_required_backend_is_missing(missing: str) -> None:
    backends: dict[str, object | None] = {
        "documents": Backend(),
        "retrieval": Backend(),
        "artifacts": Backend(),
    }
    backends[missing] = None

    with pytest.raises(ValueError, match=missing):
        register_read_only_builtins(ToolRegistry(), **backends)  # type: ignore[arg-type]
