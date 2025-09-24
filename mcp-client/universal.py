#!/usr/bin/env python3
"""
Multi-Server MCP Client (HTTP JSON-RPC or SSE) — self-contained, no mcpeval deps.

- Parses server specs: path[:arg1,arg2]^ENV=val
- Connects to HTTP MCP endpoints (e.g., http://localhost:8000/mcp)
- Lists tools via "tools/list"
- Interactive: call <server-index> <tool-name> <json-args>
"""

import asyncio
import sys
import logging
import argparse
import json
import re
from typing import List, Tuple, Dict, Any

import httpx

# ---------- Headers & SSE helpers ----------
# Match your working curl command's Accept header
HEADERS_COMBINED = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json"
}


def _parse_sse_events(text: str) -> List[dict]:
    """Extract JSON objects carried in SSE 'data:' lines."""
    events: List[dict] = []
    buf: List[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            buf.append(line[len("data:"):].strip())
        elif not line.strip():  # blank line = event boundary
            if buf:
                payload = "\n".join(buf).strip()
                try:
                    events.append(json.loads(payload))
                except Exception:
                    pass
                buf = []
    if buf:
        payload = "\n".join(buf).strip()
        try:
            events.append(json.loads(payload))
        except Exception:
            pass
    return events

async def _post_mcp_request(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    """
    Send MCP request with combined Accept header like the working curl command.
    Handle both JSON and SSE responses.
    """
    try:
        r = await client.post(url, json=payload, headers=HEADERS_COMBINED)
        r.raise_for_status()
        
        ctype = r.headers.get("content-type", "").lower()
        
        # Handle SSE response
        if "text/event-stream" in ctype:
            events = _parse_sse_events(r.text)
            for ev in reversed(events):
                if "result" in ev or "error" in ev:
                    return ev
            return {"error": {"code": -32603, "message": "No result in SSE stream"}}
        
        # Handle JSON response
        elif "application/json" in ctype:
            return r.json()
        
        # Try to parse as JSON anyway
        else:
            try:
                return r.json()
            except:
                return {"error": {"code": -32603, "message": f"Unexpected content-type: {ctype}"}}
                
    except httpx.HTTPStatusError as e:
        return {"error": {"code": e.response.status_code, "message": f"HTTP {e.response.status_code}: {e.response.text}"}}
    except httpx.RequestError as e:
        return {"error": {"code": -32603, "message": f"Request error: {str(e)}"}}
    except Exception as e:
        return {"error": {"code": -32603, "message": f"Unexpected error: {str(e)}"}}

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mcp-multi-http-client")

# ---------- CLI usage ----------
def print_usage() -> None:
    print("Multi-Server MCP Client (HTTP JSON-RPC)")
    print("=" * 44)
    print("\nUsage:")
    print("  uv run client.py --servers <server1> [server2] [server3] ...")
    print("  uv run client.py --servers <server1:args> [server2:args] [server3:args] ...")
    print("\nServer Format:")
    print("  server_path                    - Server with no arguments or env vars")
    print("  server_path:arg1,arg2          - Server with arguments (comma-separated)")
    print("  server_path^ENV_VAR=value      - Server with environment variables")
    print("  server_path:arg1^ENV_VAR=value - Server with both args and env vars")
    print("\nExamples:")
    print("  uv run client.py --servers http://localhost:8000/mcp")
    print("  uv run client.py --servers 127.0.0.1:9000/mcp")
    print("\nNotes:")
    print("  * This client talks HTTP JSON-RPC and handles SSE responses.")
    print("  * Non-HTTP specs are displayed but not connected.\n")

# ---------- URL parsing helpers ----------
_URL_HOSTPORT_RE = re.compile(
    r"^(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9.-]+):\d+(?:/.*)?$"
)

def is_probably_url(s: str) -> bool:
    return "://" in s or bool(_URL_HOSTPORT_RE.match(s))

def normalize_url(s: str) -> str:
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if is_probably_url(s):
        return "http://" + s
    return s

def parse_servers_argument(args_namespace: argparse.Namespace) -> Tuple[List[str], List[List[str]], List[Dict[str, str]]]:
    """
    Accept formats like:
      127.0.0.1:9000/mcp
      http://127.0.0.1:9000/mcp
      server_path[:arg1,arg2]^ENV=VAL
      http://127.0.0.1:9000/mcp^ENV=VAL
    """
    raw = args_namespace.servers or []
    server_paths: List[str] = []
    server_args_list: List[List[str]] = []
    server_env_list: List[Dict[str, str]] = []

    for entry in raw:
        parts = entry.split("^")
        head = parts[0]
        envs: Dict[str, str] = {}
        if len(parts) > 1:
            for envchunk in parts[1:]:
                if "=" not in envchunk:
                    raise ValueError(f"Bad env spec in '{entry}': '{envchunk}' (expected KEY=VAL)")
                k, v = envchunk.split("=", 1)
                envs[k] = v

        if is_probably_url(head):
            path = normalize_url(head)
            args = []
        else:
            if ":" in head:
                path, argstr = head.split(":", 1)
                args = [a for a in argstr.split(",") if a]
            else:
                path, args = head, []

        server_paths.append(path)
        server_args_list.append(args)
        server_env_list.append(envs)

    return server_paths, server_args_list, server_env_list

# ---------- Simple HTTP MCP Client ----------
class SimpleHTTPMCPClient:
    """
    Minimal JSON-RPC over HTTP MCP client with SSE support.

    Methods used:
      - tools/list
      - tools/call
    """
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._sessions: Dict[str, httpx.AsyncClient] = {}

    async def connect_to_multiple_servers(
        self,
        server_paths: List[str],
        server_args_list: List[List[str]],
        server_env_list: List[Dict[str, str]]
    ) -> Dict[str, Dict[str, Any]]:
        mapping: Dict[str, Dict[str, Any]] = {}

        async def fetch_server(url: str, idx: int, args: List[str], env: Dict[str, str]) -> None:
            if not (url.startswith("http://") or url.startswith("https://")):
                logger.info(f"Skipping non-HTTP server spec: {url}")
                return

            client = httpx.AsyncClient(timeout=self.timeout)
            self._sessions[url] = client

            payload = {"jsonrpc": "2.0", "id": idx, "method": "tools/list", "params": {}}
            try:
                data = await _post_mcp_request(client, url, payload)
                if "error" in data:
                    raise RuntimeError(data["error"])
                tools = data.get("result", {}).get("tools", [])
                mapping[url] = {"tools": tools, "args": args, "env": env}
                logger.info(f"Successfully listed {len(tools)} tools from {url}")
            except Exception as e:
                logger.error(f"Failed to list tools from {url}: {e}")
                mapping[url] = {"tools": [], "args": args, "env": env, "error": str(e)}

        await asyncio.gather(*[
            fetch_server(server_paths[i], i + 1, server_args_list[i], server_env_list[i])
            for i in range(len(server_paths))
        ])
        return mapping

    async def call_tool(self, url: str, name: str, arguments: Dict[str, Any]) -> Any:
        if url not in self._sessions:
            raise ValueError(f"Server not connected: {url}")
        client = self._sessions[url]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments}
        }
        data = await _post_mcp_request(client, url, payload)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("result")

    async def cleanup(self) -> None:
        for c in self._sessions.values():
            await c.aclose()
        self._sessions.clear()

# ---------- Main ----------
async def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Server MCP Client (HTTP JSON-RPC)")
    parser.add_argument('--servers', nargs='+', help='Server specs: url[:args]^ENV=VAL ...')
    parser.add_argument('--help-usage', action='store_true', help='Show detailed usage information')
    args = parser.parse_args()

    if args.help_usage or len(sys.argv) == 1:
        print_usage()
        return

    if not args.servers:
        print("❌ Error: --servers argument is required\n")
        print_usage()
        sys.exit(1)

    try:
        server_paths, server_args_list, server_env_list = parse_servers_argument(args)
    except ValueError as e:
        print(f"❌ Error parsing servers: {e}\n")
        print_usage()
        sys.exit(1)

    server_configs = []
    for i, server_path in enumerate(server_paths):
        server_args = server_args_list[i] if i < len(server_args_list) else []
        server_env = server_env_list[i] if i < len(server_env_list) else {}
        server_configs.append((server_path, server_args, server_env))

    print("🚀 Multi-Server MCP Client (HTTP JSON-RPC)")
    print("=" * 50)
    print(f"📡 You provided {len(server_paths)} servers:")
    for i, (server_path, args_l, env_d) in enumerate(server_configs, 1):
        disp = []
        if args_l: disp.append(f"args={args_l}")
        if env_d:
            env_disp = {k: (v[:20] + "..." if len(v) > 20 else v) for k, v in env_d.items()}
            disp.append(f"env={env_disp}")
        details = f" ({', '.join(disp)})" if disp else " (no args, no env)"
        print(f"  {i}. {server_path}{details}")
    print()

    client = SimpleHTTPMCPClient()

    try:
        print("⏳ Connecting (HTTP endpoints only)...")
        mapping = await client.connect_to_multiple_servers(server_paths, server_args_list, server_env_list)
        print("✅ Connected (or skipped non-HTTP).\n")

        urls = list(mapping.keys())
        total_tools = sum(len(mapping[u].get("tools", [])) for u in urls)
        for idx, url in enumerate(urls, 1):
            tools = mapping[url].get("tools", [])
            err = mapping[url].get("error")
            if err:
                print(f"[{idx}] {url} - ❌ tools/list failed: {err}")
            else:
                print(f"[{idx}] {url} - {len(tools)} tools")
                for t in tools:
                    name = t.get("name")
                    desc = t.get("description", "") or ""
                    print(f"     - {name}: {desc[:80]}{'...' if len(desc) > 80 else ''}")
        print(f"\n🔧 Total tools available (HTTP): {total_tools}\n")

        print("💬 Interactive commands:")
        print("  help                          -> show help")
        print("  list                          -> list servers and tools")
        print("  call <idx> <tool> <json-args> -> call a tool (e.g., call 1 get_quote {\"ticker\":\"AAPL\"})")
        print("  quit                          -> exit\n")

        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 bye")
                break

            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                break
            if line.lower() in ("help", "?"):
                print("Commands:\n  list\n  call <server-index> <tool-name> <json-args>\n  quit\n")
                continue
            if line.lower() == "list":
                for idx, url in enumerate(urls, 1):
                    tools = mapping[url].get("tools", [])
                    print(f"[{idx}] {url} - {len(tools)} tools")
                    for t in tools:
                        print("    -", t.get("name"))
                continue

            if line.startswith("call "):
                parts = line.split(" ", 3)
                if len(parts) < 4:
                    print("Usage: call <server-index> <tool-name> <json-args>")
                    continue
                _, idx_str, tool_name, args_json = parts
                try:
                    idx_int = int(idx_str)
                    if idx_int < 1 or idx_int > len(urls):
                        print("Invalid server index")
                        continue
                    url = urls[idx_int - 1]
                    try:
                        tool_args = json.loads(args_json)
                        if not isinstance(tool_args, dict):
                            print('json-args must be a JSON object, e.g. {"ticker":"AAPL"}')
                            continue
                    except json.JSONDecodeError as je:
                        print(f"Bad JSON: {je}")
                        continue

                    result = await client.call_tool(url, tool_name, tool_args)
                    out = json.dumps(result, indent=2)
                    print(out if len(out) <= 4000 else out[:4000] + " ... [truncated]")
                except Exception as e:
                    print(f"❌ call failed: {e}")
                continue

            print("Unknown command. Type 'help'.")

    finally:
        await client.cleanup()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print_usage()
        sys.exit(0)
    asyncio.run(main())