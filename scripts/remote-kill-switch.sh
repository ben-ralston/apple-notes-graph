#!/bin/bash
# Kill switch for the remote Apple Notes connector (issue #167): stop the public
# tunnel and the remote MCP server. Safe to run any time; every step tolerates
# "not running". Touches ONLY this project's LaunchAgents and process - never a
# blanket `pkill cloudflared` (other tunnels may run on the same Mac).
#
#   ./run remote-kill        (or run this script directly)
#
# Tailscale Funnel port: NOTES_GRAPH_FUNNEL_PORT (default 443); server port:
# NOTES_GRAPH_REMOTE_PORT (default 9893).
set -u

uid="$(id -u)"
funnel_port="${NOTES_GRAPH_FUNNEL_PORT:-443}"
server_port="${NOTES_GRAPH_REMOTE_PORT:-9893}"

# 1. Tunnel first, so nothing public reaches the server while it stops.
launchctl bootout "gui/$uid/com.notes-graph.mcp-remote-tunnel" 2>/dev/null \
  && echo "stopped tunnel LaunchAgent" || echo "tunnel LaunchAgent not loaded"
if command -v tailscale >/dev/null 2>&1; then
  # Only if the Funnel actually proxies to this server: `off` drops the whole
  # :$funnel_port handler, which may belong to something else.
  if tailscale funnel status 2>/dev/null | grep -Eq "127\.0\.0\.1:${server_port}([^0-9]|$)"; then
    tailscale funnel --https="$funnel_port" off 2>/dev/null \
      && echo "turned off Tailscale Funnel on :$funnel_port" || echo "could not turn off Tailscale Funnel on :$funnel_port"
  else
    echo "no Tailscale Funnel to 127.0.0.1:${server_port}"
  fi
fi

# 2. Server: unload the LaunchAgent (stops KeepAlive restarts), then any stray
#    foreground `apple-notes-mcp-remote` (e.g. ./run remote-start).
launchctl bootout "gui/$uid/com.notes-graph.mcp-remote" 2>/dev/null \
  && echo "stopped server LaunchAgent" || echo "server LaunchAgent not loaded"
pkill -f "bin/apple-notes-mcp-remote" 2>/dev/null \
  && echo "stopped apple-notes-mcp-remote process" || echo "no apple-notes-mcp-remote process"

exit 0
