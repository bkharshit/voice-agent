"""Local read-only MCP server for integration verification."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-tools", host="127.0.0.1", port=55434)


@mcp.tool()
def echo(message: str) -> str:
    """Return the supplied message."""
    return message


@mcp.tool()
def not_allowed() -> str:
    """This tool must not be exposed to the agent."""
    raise RuntimeError("Disallowed tool executed")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
