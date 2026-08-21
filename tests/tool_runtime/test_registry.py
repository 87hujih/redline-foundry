from __future__ import annotations

from datetime import timedelta

import pytest

from docreview.tool_runtime import (
    ToolDefinition,
    ToolName,
    ToolRegistry,
    ToolRiskLevel,
    ToolVersion,
)


class Backend:
    async def execute(self, request: object) -> object:
        raise AssertionError("backend execution was not expected")

    async def recover(self, request: object) -> object:
        raise AssertionError("backend recovery was not expected")


def definition(
    *,
    name: str = "document.read_nodes",
    version: str = "1.0.0",
    input_schema: str = '{"type":"object","additionalProperties":false}',
    requires_resource: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        name=ToolName(name),
        version=ToolVersion(version),
        description="Read immutable document nodes",
        input_schema=input_schema,
        output_schema='{"type":"object","additionalProperties":false}',
        risk_level=ToolRiskLevel.LOW,
        timeout=timedelta(seconds=1),
        requires_resource=requires_resource,
        requires_approval=False,
        max_inline_output_bytes=1_024,
        backend=Backend(),
    )


def test_registry_resolves_only_the_explicit_name_and_version() -> None:
    registry = ToolRegistry()
    expected = definition()

    registry.register(expected)

    assert registry.resolve(ToolName("document.read_nodes"), ToolVersion("1.0.0")) == expected
    with pytest.raises(LookupError, match="not registered"):
        registry.resolve(ToolName("document.read_nodes"), ToolVersion("2.0.0"))


def test_registry_rejects_duplicate_registration_and_changes_after_freeze() -> None:
    registry = ToolRegistry()
    registry.register(definition())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition())

    registry.freeze()
    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(definition(name="retrieval.search", version="2.0.0"))


@pytest.mark.parametrize("value", ["", " document.read_nodes", "Document.Read"])
def test_tool_name_rejects_empty_or_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="tool name"):
        ToolName(value)


@pytest.mark.parametrize("value", ["", "latest", " 1.0.0"])
def test_tool_version_rejects_empty_or_implicit_values(value: str) -> None:
    with pytest.raises(ValueError, match="tool version"):
        ToolVersion(value)


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ('{"type":"object","type":"object"}', "duplicate JSON key"),
        (
            '{"type":"object","oneOf":[],"additionalProperties":false}',
            "unsupported JSON Schema keyword",
        ),
        ('{"type":"object"}', "additionalProperties must be false"),
    ],
)
def test_registry_rejects_schema_constraints_it_cannot_enforce(schema: str, message: str) -> None:
    registry = ToolRegistry()

    with pytest.raises(ValueError, match=message):
        registry.register(definition(input_schema=schema))


def test_registry_rejects_resource_tool_without_a_required_string_selector() -> None:
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="resource input field"):
        registry.register(definition(requires_resource=True))
