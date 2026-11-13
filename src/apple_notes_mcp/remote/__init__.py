"""Remote mode: the Apple Notes MCP server over Streamable HTTP + OAuth (issue #167).

A separate entry point (``apple-notes-mcp-remote``) so the stdio server
(``apple-notes-mcp``) is untouched. The remote server copies the stdio
server's tools through an allowlist (``tools.py``), guards ``/mcp`` with an
OAuth proxy to GitHub limited to one identity (``auth.py``), and logs every
request without arguments, contents or tokens (``request_log.py``).
"""
