#!/usr/bin/env python3
"""
Multi-Server MCP Client (HTTP JSON-RPC or SSE) with OpenAI Integration — self-contained, no mcpeval deps.

- Parses server specs: path[:arg1,arg2]^ENV=val
- Connects to HTTP MCP endpoints (e.g., http://localhost:8000/mcp)
- Lists tools via "tools/list"
- Interactive: call <server-index> <tool-name> <json-args>
- OpenAI Integration: ask questions and let AI use tools automatically
"""

import asyncio
import sys
import logging
import argparse
import json
import re
import os
from typing import List, Tuple, Dict, Any, Optional
import openai
import httpx
import datetime

current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    print("Multi-Server MCP Client (HTTP JSON-RPC) with OpenAI AI")
    print("=" * 54)
    print("\nUsage:")
    print("  uv run client.py --servers <server1> [server2] [server3] ...")
    print("  uv run client.py --servers <server1:args> [server2:args] [server3:args] ...")
    print("\nServer Format:")
    print("  server_path                    - Server with no arguments or env vars")
    print("  server_path:arg1,arg2          - Server with arguments (comma-separated)")
    print("  server_path^ENV_VAR=value      - Server with environment variables")
    print("  server_path:arg1^ENV_VAR=value - Server with both args and env vars")
    print("\nOpenAI Options:")
    print("  --openai-api-key KEY           - OpenAI API key (or set OPENAI_API_KEY env var)")
    print("  --openai-model MODEL           - OpenAI model name (default: gpt-4)")
    print("  --openai-base-url URL          - OpenAI API base URL (for Ollama/local: http://localhost:11434/v1)")
    print("\nExamples:")
    print("  uv run client.py --servers http://localhost:8000/mcp")
    print("  uv run client.py --servers 127.0.0.1:9000/mcp --openai-model gpt-3.5-turbo")
    print("  uv run client.py --servers http://localhost:8000/mcp --openai-base-url http://localhost:11434/v1 --openai-model llama3.1")
    print("\nAI Commands (when OpenAI is connected):")
    print("  ask <question>                 - Ask AI to suggest and call appropriate tools")
    print("  chat <message>                 - Have a conversation with AI about MCP tools")
    print("  reset                          - Reset conversation history")
    print("\nNotes:")
    print("  * This client talks HTTP JSON-RPC and handles SSE responses.")
    print("  * Non-HTTP specs are displayed but not connected.")
    print("  * Requires OpenAI API key for AI features.")
    print("  * Use --openai-base-url for Ollama or other OpenAI-compatible APIs.\n")

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

# ---------- OpenAI Integration ----------
class OpenAIMCPIntegration:
    """
    Integrates OpenAI with MCP client for intelligent tool usage.
    """
    def __init__(self, api_key: str, model: str = "llama3.1", base_url: Optional[str] = None):
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url= "localhost:11434/v1" if base_url is None else base_url
        )
        self.model = model
        self.conversation_history: List[Dict[str, Any]] = []
        self.system_prompt = (
            "You are a helpful AI assistant that can use multiple tools to help the user. "
            "When the user asks a question, analyze what tools are available and use them appropriately. "
            # "Analyze the user's request, decide which tool(s) to call with what arguments, and call them. "
            "Always explain what you're doing and provide clear, helpful responses."
            "If you facing an error on tool call, try to fix the arguments or choose another tool. and try again."
            "Sometimes you may need to call multiple tools to get the final answer."
            "Current time: {current_time}"
        )
        
    def reset_conversation(self):
        """Reset the conversation history."""
        self.conversation_history = []
        
    def add_system_message(self, content: str):
        """Add a system message to the conversation."""
        self.conversation_history.append({"role": "system", "content": content})
        
    def add_user_message(self, content: str):
        """Add a user message to the conversation."""
        self.conversation_history.append({"role": "user", "content": content})
        
    def add_assistant_message(self, content: str):
        """Add an assistant message to the conversation."""
        self.conversation_history.append({"role": "assistant", "content": content})
        
    def create_tools_description(self, server_mapping: Dict[str, Dict[str, Any]]) -> str:
        """Create a description of available tools for the AI."""
        tools_desc = "Available tools:\n\n"
        
        for idx, (url, info) in enumerate(server_mapping.items(), 1):
            tools = info.get("tools", [])
            if not tools:
                continue
                
            tools_desc += f"Server {idx} ({url}):\n"
            for tool in tools:
                name = tool.get("name", "unknown")
                desc = tool.get("description", "No description")
                
                # Add parameter info if available
                input_schema = tool.get("inputSchema", {})
                properties = input_schema.get("properties", {})
                if properties:
                    params = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in properties.items())
                    tools_desc += f"  - {name}({params}): {desc}\n"
                else:
                    tools_desc += f"  - {name}(): {desc}\n"
            tools_desc += "\n"
            
        return tools_desc
        
    def create_openai_tools(self, server_mapping: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Create OpenAI function definitions from MCP tools."""
        openai_tools = []
        
        for idx, (url, info) in enumerate(server_mapping.items(), 1):
            tools = info.get("tools", [])
            for tool in tools:
                name = tool.get("name", "unknown")
                desc = tool.get("description", "No description")
                input_schema = tool.get("inputSchema", {})
                
                # Create unique function name with server index
                function_name = f"server{idx}_{name}"
                
                openai_tool = {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": f"[Server {idx}] {desc}",
                        "parameters": input_schema if input_schema else {"type": "object", "properties": {}}
                    }
                }
                openai_tools.append(openai_tool)
                
        return openai_tools
        
    async def chat_with_tools(self, user_message: str, mcp_client: SimpleHTTPMCPClient, 
                            server_mapping: Dict[str, Dict[str, Any]]) -> str:
        """
        Have a conversation with the AI using available MCP tools.
        """
        # Add user message to history
        self.add_user_message(user_message)
        
        # Create tools description for system context
        if not any(msg["role"] == "system" for msg in self.conversation_history):
            tools_desc = self.create_tools_description(server_mapping)
            system_message = f"{self.system_prompt}\n\n{tools_desc}"
            self.conversation_history.insert(0, {"role": "system", "content": system_message})
        
        # Create OpenAI tools
        openai_tools = self.create_openai_tools(server_mapping)
        
        try:
            # Make the chat completion request
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                tools=openai_tools if openai_tools else None,
                tool_choice="auto" if openai_tools else None
            )
            
            message = response.choices[0].message
            
            # Handle tool calls
            if message.tool_calls:
                self.conversation_history.append({
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [tc.dict() for tc in message.tool_calls]
                })
                logging.info(f"Tool calls: {message.tool_calls}")
                
                # Execute each tool call
                for tool_call in message.tool_calls:
                    function_name = tool_call.function.name
                    arguments = json.loads(tool_call.function.arguments)
                    
                    # Parse server index and tool name
                    if function_name.startswith("server") and "_" in function_name:
                        parts = function_name.split("_", 1)
                        server_idx = int(parts[0][6:])  # Remove "server" prefix
                        tool_name = parts[1]
                        
                        # Get server URL
                        server_urls = list(server_mapping.keys())
                        if 1 <= server_idx <= len(server_urls):
                            server_url = server_urls[server_idx - 1]
                            
                            try:
                                # Call the tool
                                result = await mcp_client.call_tool(server_url, tool_name, arguments)
                                result_content = json.dumps(result, indent=2) if result else "Tool executed successfully"
                                
                                # Add tool result to conversation
                                self.conversation_history.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": result_content
                                })
                                
                            except Exception as e:
                                error_msg = f"Error calling tool {tool_name}: {str(e)}"
                                self.conversation_history.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": error_msg
                                })
                
                # Get final response after tool execution
                final_response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=self.conversation_history
                )
                
                final_message = final_response.choices[0].message.content
                self.add_assistant_message(final_message)
                return final_message
                
            else:
                # No tool calls, just return the response
                content = message.content or "I apologize, but I couldn't generate a response."
                self.add_assistant_message(content)
                return content
                
        except Exception as e:
            error_msg = f"Error communicating with AI: {str(e)}"
            logger.error(error_msg)
            return error_msg

# ---------- Main ----------
async def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Server MCP Client (HTTP JSON-RPC) with OpenAI")
    parser.add_argument('--servers', nargs='+', help='Server specs: url[:args]^ENV=VAL ...')
    parser.add_argument('--openai-api-key', help='OpenAI API key (or set OPENAI_API_KEY env var)')
    parser.add_argument('--openai-model', default='gpt-4', help='OpenAI model name (default: gpt-4)')
    parser.add_argument('--openai-base-url', help='OpenAI API base URL (for Ollama: http://localhost:11434/v1)')
    parser.add_argument('--help-usage', action='store_true', help='Show detailed usage information')
    args = parser.parse_args()

    if args.help_usage or len(sys.argv) == 1:
        print_usage()
        return

    if not args.servers:
        print("❌ Error: --servers argument is required\n")
        print_usage()
        sys.exit(1)

    # Get OpenAI API key
    openai_api_key = args.openai_api_key or os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        print("⚠️  Warning: No OpenAI API key provided. AI features will be disabled.")
        print("Set OPENAI_API_KEY environment variable or use --openai-api-key option for AI features.")
        openai_integration = None
    else:
        openai_integration = OpenAIMCPIntegration(
            api_key=openai_api_key,
            model=args.openai_model,
            base_url=args.openai_base_url
        )

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

    print("🚀 Multi-Server MCP Client (HTTP JSON-RPC) with OpenAI")
    print("=" * 60)
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
        if openai_integration:
            print("  ask <question>                -> ask AI to use tools to answer your question")
            print("  chat <message>                -> chat with AI using available tools")
            print("  reset                         -> reset AI conversation history")
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
                commands = ["help", "list", "call <server-index> <tool-name> <json-args>"]
                if openai_integration:
                    commands.extend(["ask <question>", "chat <message>", "reset"])
                commands.append("quit")
                print("Commands:\n  " + "\n  ".join(commands) + "\n")
                continue
            if line.lower() == "list":
                for idx, url in enumerate(urls, 1):
                    tools = mapping[url].get("tools", [])
                    print(f"[{idx}] {url} - {len(tools)} tools")
                    for t in tools:
                        print("    -", t.get("name"))
                continue
            
            # OpenAI integration commands
            if openai_integration and (line.startswith("ask ") or line.startswith("chat ")):
                command, _, message = line.partition(" ")
                if not message:
                    print("Usage: ask <question> or chat <message>")
                    continue
                    
                try:
                    print("🤔 Thinking...")
                    print(mapping)
                    response = await openai_integration.chat_with_tools(message, client, mapping)
                    print(f"\n🤖 {response}\n")
                except Exception as e:
                    print(f"❌ AI error: {e}")
                continue
                
            if openai_integration and line.lower() == "reset":
                openai_integration.reset_conversation()
                print("✅ Conversation history reset")
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

            # Handle AI commands when OpenAI integration is not available
            if line.startswith("ask ") or line.startswith("chat ") or line.lower() == "reset":
                print("⚠️  AI features not available. Please provide OpenAI API key.")
                continue

            print("Unknown command. Type 'help'.")

    finally:
        await client.cleanup()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print_usage()
        sys.exit(0)
    asyncio.run(main())