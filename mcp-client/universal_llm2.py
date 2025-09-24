#!/usr/bin/env python3
"""
Multi-Server MCP Client — OpenAI Responses API (no manual tool loop)

- Pass HTTP MCP endpoints via --servers (e.g., http://127.0.0.1:9000/mcp)
- We give them to the OpenAI Responses API as MCP tools:
      tools=[{"type":"mcp","server_label":"server-1","server_url":"http://...","require_approval":"never"}]
- The model calls MCP tools directly (Responses API handles discovery + execution).
- Includes a small HTTP JSON-RPC lister/caller for debugging.

Requirements:
  pip install openai httpx

Docs:
  - Responses API + MCP tools: see OpenAI blog & cookbook (supports "type": "mcp").
"""

import asyncio
import argparse
import json
import os
import re
import sys
import logging
from typing import Any, Dict, List, Optional

import httpx
from openai import AsyncOpenAI

# ------------ Logging ------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("mcp-responses-client")

# ------------ Simple HTTP/SSE helpers (debug lister) ------------
HEADERS_COMBINED = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

def _parse_sse_events(text: str) -> List[dict]:
    events, buf = [], []
    for line in text.splitlines():
        if line.startswith("data:"):
            buf.append(line[len("data:"):].strip())
        elif not line.strip():
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
    try:
        r = await client.post(url, json=payload, headers=HEADERS_COMBINED)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            events = _parse_sse_events(r.text)
            for ev in reversed(events):
                if "result" in ev or "error" in ev:
                    return ev
            return {"error": {"code": -32603, "message": "No result in SSE stream"}}
        # JSON
        return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": {"code": e.response.status_code, "message": f"HTTP {e.response.status_code}: {e.response.text}"}}
    except httpx.RequestError as e:
        return {"error": {"code": -32603, "message": f"Request error: {str(e)}"}}
    except Exception as e:
        return {"error": {"code": -32603, "message": f"Unexpected error: {str(e)}"}}

class SimpleHTTPMCPClient:
    """Tiny helper to list/call MCP tools over HTTP JSON-RPC (debug)."""
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._sessions: Dict[str, httpx.AsyncClient] = {}

    async def connect_and_list(self, server_urls: List[str]) -> Dict[str, Dict[str, Any]]:
        mapping: Dict[str, Dict[str, Any]] = {}

        async def fetch(url: str, idx: int) -> None:
            if not (url.startswith("http://") or url.startswith("https://")):
                mapping[url] = {"tools": [], "error": "not http(s)"}
                return
            c = httpx.AsyncClient(timeout=self.timeout)
            self._sessions[url] = c
            payload = {"jsonrpc": "2.0", "id": idx, "method": "tools/list", "params": {}}
            try:
                data = await _post_mcp_request(c, url, payload)
                if "error" in data:
                    raise RuntimeError(data["error"])
                mapping[url] = {"tools": data.get("result", {}).get("tools", [])}
            except Exception as e:
                mapping[url] = {"tools": [], "error": str(e)}

        await asyncio.gather(*[fetch(u, i + 1) for i, u in enumerate(server_urls)])
        return mapping

    async def call_tool(self, url: str, name: str, arguments: Dict[str, Any]) -> Any:
        if url not in self._sessions:
            self._sessions[url] = httpx.AsyncClient(timeout=self.timeout)
        c = self._sessions[url]
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        data = await _post_mcp_request(c, url, payload)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("result")

    async def cleanup(self) -> None:
        for c in self._sessions.values():
            await c.aclose()
        self._sessions.clear()

# ------------ URL handling ------------
_URL_HOSTPORT_RE = re.compile(r"^(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9.-]+):\d+(?:/.*)?$")

def _is_probably_url(s: str) -> bool:
    return "://" in s or bool(_URL_HOSTPORT_RE.match(s))

def _normalize_url(s: str) -> str:
    if s.startswith(("http://", "https://")):
        return s
    if _is_probably_url(s):
        return "http://" + s
    return s

# ------------ Responses API wrapper (MCP tools) ------------
class ResponsesMCPClient:
    """
    Minimal wrapper around OpenAI Responses API with MCP tools.
    The model performs MCP tool discovery/calls internally — no function-calling loop.
    """
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1",
        base_url: Optional[str] = None,
        require_approval: str = "never",
    ):
        # Warn if base_url likely not supporting Responses+MCP
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
        self.model = model
        self.require_approval = require_approval
        # simple conversation memory
        self.history: List[Dict[str, str]] = []

    @staticmethod
    def _mcp_tool(url: str, label: str, require_approval: str, allowed_tools: Optional[List[str]] = None) -> Dict[str, Any]:
        tool: Dict[str, Any] = {
            "type": "mcp",
            "server_label": label,
            "server_url": url,
            "require_approval": require_approval,  # "never" | "auto" | "always"
        }
        if allowed_tools:
            tool["allowed_tools"] = allowed_tools
        return tool

    def _build_tools(self, servers: List[str], allowed: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        for i, s in enumerate(servers, 1):
            surl = s if s.startswith(("http://", "https://")) else "http://" + s
            label = f"server-{i}"
            tools.append(self._mcp_tool(surl, label, self.require_approval, (allowed or {}).get(s, None)))
        return tools

    async def ask(self, prompt: str, servers: List[str], allowed: Optional[Dict[str, List[str]]] = None) -> str:
        """
        Send a prompt; Responses API will call MCP tools as needed.
        """
        # maintain tiny rolling context
        self.history.append({"role": "user", "content": prompt})
        messages = self.history[-8:]  # keep short on purpose

        tools = self._build_tools(servers, allowed)

        # Responses API call (model handles MCP tools)
        # (You can also pass `input=messages` if you'd like strict multi-turn role structure.)
        resp = await self.client.responses.create(
            model=self.model,
            input=messages,  # multi-turn
            tools=tools,
            tool_choice="auto",
        )

        # Convenience helper on SDK: output_text best-effort final text
        out = getattr(resp, "output_text", None)
        if out is None:
            # fall back to raw
            out = str(resp)
        out = (out or "").strip()
        self.history.append({"role": "assistant", "content": out})
        return out

# ------------ CLI ------------
def _print_usage() -> None:
    print("Multi-Server MCP Client (OpenAI Responses + MCP)")
    print("=" * 56)
    print("\nUsage:")
    print("  python mcp_client.py --servers <server1> [server2] ... [--openai-api-key KEY]")
    print("\nServer Format:")
    print("  http://127.0.0.1:9000/mcp            (HTTP MCP endpoint)")
    print("  host:9000/mcp                         (host:port; http:// is auto-added)")
    print("\nOptions:")
    print("  --openai-api-key KEY                 (or set OPENAI_API_KEY)")
    print("  --openai-model MODEL                 (default: gpt-4.1)")
    print("  --openai-base-url URL                (OpenAI-compatible base URL; must support Responses+MCP)")
    print("  --require-approval never|auto|always (default: never)")
    print("  --list-first                         (list tools from each server before REPL)")
    print("\nInteractive commands:")
    print("  help")
    print("  list")
    print("  call <server-index> <tool> <json-args>   (manual debug HTTP)")
    print("  ask <question>")
    print("  chat <message>   (alias of ask)")
    print("  reset")
    print("  quit\n")

async def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-Server MCP Client — Responses API")
    ap.add_argument("--servers", nargs="+", required=True, help="HTTP MCP endpoints")
    ap.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY"))
    ap.add_argument("--openai-model", default="gpt-4.1")
    ap.add_argument("--openai-base-url", default=os.getenv("OPENAI_BASE_URL"))
    ap.add_argument("--require-approval", choices=["never", "auto", "always"], default="never")
    ap.add_argument("--list-first", action="store_true")
    args = ap.parse_args()

    if not args.openai_api_key:
        print("❌ OPENAI_API_KEY missing. Set env or pass --openai-api-key.")
        sys.exit(1)

    servers = [_normalize_url(s) for s in args.servers]
    ai = ResponsesMCPClient(
        api_key=args.openai_api_key,
        model=args.openai_model,
        base_url=args.openai_base_url,
        require_approval=args.require_approval,
    )

    # Optional: list tools via raw HTTP JSON-RPC (debug visibility)
    lister = SimpleHTTPMCPClient()
    mapping: Dict[str, Dict[str, Any]] = {}

    if args.list_first:
        print("⏳ Listing tools (HTTP debug)...")
        mapping = await lister.connect_and_list(servers)
        for i, url in enumerate(servers, 1):
            tools = mapping.get(url, {}).get("tools", [])
            err = mapping.get(url, {}).get("error")
            status = f"{len(tools)} tools" if not err else f"ERROR: {err}"
            print(f"  {i}. {url} - {status}")
            for t in tools:
                print(f"     - {t.get('name')}")
        print()

    # REPL
    print("🚀 Multi-Server MCP Client (Responses API + MCP)")
    print("=" * 60)
    print(f"📡 Servers: {len(servers)}")
    for i, s in enumerate(servers, 1):
        print(f"  {i}. {s}")
    print("\n💬 Commands: help | list | call <idx> <tool> <json-args> | ask <q> | chat <m> | reset | quit\n")

    try:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 bye")
                break
            if not line:
                continue

            cmd = line.split()[0].lower()

            if cmd in ("quit", "exit"):
                break
            if cmd in ("help", "?"):
                _print_usage()
                continue
            if cmd == "list":
                mapping = await lister.connect_and_list(servers)
                for i, url in enumerate(servers, 1):
                    tools = mapping.get(url, {}).get("tools", [])
                    err = mapping.get(url, {}).get("error")
                    status = f"{len(tools)} tools" if not err else f"ERROR: {err}"
                    print(f"[{i}] {url} - {status}")
                    for t in tools:
                        print(f"    - {t.get('name')}")
                continue
            if cmd == "call":
                parts = line.split(" ", 3)
                if len(parts) < 4:
                    print('Usage: call <server-index> <tool-name> <json-args>')
                    continue
                _, sidx, tname, jargs = parts
                try:
                    idx = int(sidx)
                    if not (1 <= idx <= len(servers)):
                        print("Invalid server index")
                        continue
                    url = servers[idx - 1]
                    try:
                        obj = json.loads(jargs)
                        if not isinstance(obj, dict):
                            print("json-args must be a JSON object")
                            continue
                    except json.JSONDecodeError as je:
                        print(f"Bad JSON: {je}")
                        continue
                    res = await lister.call_tool(url, tname, obj)
                    out = json.dumps(res, ensure_ascii=False, indent=2)
                    print(out if len(out) <= 4000 else out[:4000] + " ... [truncated]")
                except Exception as e:
                    print(f"❌ call failed: {e}")
                continue
            if cmd in ("ask", "chat"):
                _, _, message = line.partition(" ")
                if not message:
                    print("Usage: ask <question> | chat <message>")
                    continue
                try:
                    print("🤔 Thinking via Responses API + MCP tools...")
                    reply = await ai.ask(message, servers)
                    print(f"\n🤖 {reply}\n")
                except Exception as e:
                    print(f"❌ AI error: {e}")
                continue
            if cmd == "reset":
                ai.history.clear()
                print("✅ Conversation history reset")
                continue

            print("Unknown command. Type 'help'.")
    finally:
        await lister.cleanup()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        _print_usage()
        sys.exit(0)
    asyncio.run(main())
