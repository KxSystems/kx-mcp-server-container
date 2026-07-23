"""The simplest possible add-in: a pure tool with no backend and no config.

The module-level standalone ``@tool`` decorator (FastMCP 3.x) attaches discovery metadata and
returns the function unchanged, so native discovery finds and registers it. The name is **bare**
(``echo``) — the container supplies the ``acme`` qualifier at ``mount(namespace="acme")``, so the
composed tool is ``acme_echo``.
"""

from fastmcp.tools import tool


@tool
def echo(text: str) -> str:
    """Echo the input text back."""
    return text
