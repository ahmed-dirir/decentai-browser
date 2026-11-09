"""What an agent is written with: an agent made of tools, each function a method of one, named by its id."""

from __future__ import annotations

import keyword
import logging
from typing import Callable


class ResourceDenied(Exception):
    """The runtime refused or failed one of the program's calls: its words, and its code (agents.md §9)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class ToolBase:
    """A group of an agent's functions. A function's id is the tool's id, an underscore, and its method's name."""

    id = ""

    def __init__(self, agent: "AgentBase"):
        self.agent = agent
        self.logger = logging.getLogger(type(self).__name__)

    async def close(self) -> None:
        """Whatever the tool holds, let go."""


class AgentBase:
    """An agent: its tools, and its functions found among their methods."""

    def __init__(self):
        self.logger = logging.getLogger(type(self).__name__)
        self._tools = {tool.id: tool for tool in self.tools()}

    def tools(self) -> list[ToolBase]:
        raise NotImplementedError

    def tool(self, tool_id: str) -> ToolBase | None:
        return self._tools.get(tool_id)

    def function(self, function_id: str) -> Callable | None:
        """The method a function id names: ``notes_save`` is ``save`` of the tool ``notes``."""
        found = []
        for tool_id, tool in self._tools.items():
            if function_id.startswith(f"{tool_id}_"):
                name = function_id[len(tool_id) + 1:]
                name += "_" if keyword.iskeyword(name) else ""
                method = getattr(type(tool), name, None)
                if callable(method) and method is not getattr(ToolBase, name, None):
                    found.append(getattr(tool, name))
        return found[0] if len(found) == 1 else None

    async def close(self) -> None:
        for tool in self._tools.values():
            await tool.close()
