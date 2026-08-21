"""生产 Tool 的精确版本 Registry。"""

from __future__ import annotations

from dataclasses import dataclass

from docreview.tool_runtime.models import ToolDefinition, ToolName, ToolVersion
from docreview.tool_runtime.schema import SchemaNode, compile_schema


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDefinition
    input_schema: SchemaNode
    output_schema: SchemaNode


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[ToolName, ToolVersion], RegisteredTool] = {}
        self._frozen = False

    def register(self, definition: ToolDefinition) -> None:
        if self._frozen:
            raise RuntimeError("tool registry is frozen")
        if not callable(getattr(definition.backend, "execute", None)) or not callable(
            getattr(definition.backend, "recover", None)
        ):
            raise ValueError("tool backend must provide execute and recover")
        input_schema = compile_schema(definition.input_schema)
        output_schema = compile_schema(definition.output_schema)
        if definition.requires_resource:
            resource_schema = (input_schema.properties or {}).get(definition.resource_input_field)
            if (
                resource_schema is None
                or resource_schema.type_name != "string"
                or definition.resource_input_field not in input_schema.required
            ):
                raise ValueError("tool resource input field must be a required string")
        key = (definition.name, definition.version)
        if key in self._definitions:
            raise ValueError("tool name and version are already registered")
        self._definitions[key] = RegisteredTool(definition, input_schema, output_schema)

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def resolve(self, name: ToolName, version: ToolVersion) -> ToolDefinition:
        try:
            return self._definitions[(name, version)].definition
        except KeyError as error:
            raise LookupError(f"tool {name}@{version} is not registered") from error

    def resolve_registered(self, name: ToolName, version: ToolVersion) -> RegisteredTool:
        self.resolve(name, version)
        return self._definitions[(name, version)]


__all__ = ["RegisteredTool", "ToolRegistry"]
