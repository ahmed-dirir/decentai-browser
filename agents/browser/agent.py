"""The Browser agent (see manifest.yaml).

A real browser, the chat's model as the intelligence, and the person
for the two things a browser agent must never decide alone: logins and
irreversible actions. The person watches it in the chat and takes it
over when a site wants a human.
"""

from standard import AgentBase

from .tools import BrowseTool


class BrowserAgent(AgentBase):
    def tools(self):
        return [BrowseTool(self)]
