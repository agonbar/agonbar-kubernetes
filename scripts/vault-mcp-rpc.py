#!/usr/bin/env python3
"""Call a vault-mcp tool over plain HTTP JSON-RPC.

Escape hatch for when the `knowledge-vault` MCP *client* connection inside an
agent session is dead ("MCP server not connected") while the server itself is
perfectly healthy. Talks to the same FastMCP endpoint the client would use, so
it cannot cause the git/Syncthing divergence that writing to the repo directly
caused before (see the knowledge-vault-sync note).

The URL + bearer token are read from ~/.claude.json (mcpServers.knowledge-vault),
so there is nothing to keep in sync here.

Usage:
    vault-mcp-rpc.py list
    vault-mcp-rpc.py call read '{"path": "projects/litellm.md"}'
    vault-mcp-rpc.py call append '{"path": "log.md", "content": "..."}'
"""
import json
import sys
import urllib.request

CONFIG = "/home/agonbar/.claude.json"
SERVER = "knowledge-vault"


def endpoint():
    cfg = json.load(open(CONFIG))["mcpServers"][SERVER]
    return cfg["url"], cfg["headers"]["Authorization"]


def rpc(url, auth, method, params=None, session=None, notify=False):
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = 1
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
        # FastMCP streamable-http rejects a request that does not accept both
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.headers.get("mcp-session-id"), resp.read().decode()


def parse(raw):
    """The endpoint answers as SSE; pull the JSON out of the data: lines."""
    for line in raw.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(raw) if raw.strip() else None


def main():
    url, auth = endpoint()
    session, raw = rpc(url, auth, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "vault-mcp-rpc", "version": "1"},
    })
    rpc(url, auth, "notifications/initialized", session=session, notify=True)

    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    if action == "list":
        _, raw = rpc(url, auth, "tools/list", session=session)
        for tool in parse(raw)["result"]["tools"]:
            required = tool["inputSchema"].get("required", [])
            print(f"{tool['name']}({', '.join(required)})")
        return

    name, args = sys.argv[2], json.loads(sys.argv[3])
    _, raw = rpc(url, auth, "tools/call", {"name": name, "arguments": args},
                 session=session)
    out = parse(raw)
    if "error" in out:
        print(json.dumps(out["error"], indent=2))
        sys.exit(1)
    result = out["result"]
    if result.get("isError"):
        print("TOOL ERROR:", json.dumps(result, indent=2)[:2000])
        sys.exit(1)
    for block in result.get("content", []):
        print(block.get("text", ""))


if __name__ == "__main__":
    main()
