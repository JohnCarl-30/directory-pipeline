"""MCP server exposing the company directory to any MCP client."""

from .server import build_server, main

__all__ = ["build_server", "main"]
