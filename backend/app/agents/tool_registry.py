from typing import Any


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, name: str, tool: Any) -> None:
        self._tools[name] = tool

    def resolve(self, enabled_names: list[str]) -> list[Any]:
        return [self._tools[name] for name in enabled_names if name in self._tools]

    def missing(self, enabled_names: list[str]) -> list[str]:
        return [name for name in enabled_names if name not in self._tools]


tool_registry = ToolRegistry()
