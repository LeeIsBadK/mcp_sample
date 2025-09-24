import asyncio
import json
import requests
from typing import Any, Dict, List

from fastmcp import Client as MCPClient  # FastMCP v2 client

OLLAMA_BASE = "http://localhost:11434/v1"
MODEL = "qwen3:8b"
MCP_URL = "http://localhost:8000/mcp/"  # fastmcp server URL

import ast
import json

last_rolls = None
last_sum = None

def coerce_rolls(value, last_rolls):
    if isinstance(value, list) and all(isinstance(x, int) for x in value):
        return value
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list) and all(isinstance(x, int) for x in parsed):
                return parsed
        except Exception:
            pass
    if last_rolls and isinstance(last_rolls, list) and all(isinstance(x, int) for x in last_rolls):
        return last_rolls
    raise ValueError("Could not coerce 'rolls' to a list[int].")


    # Fall back: use last known rolls if available
    if last_rolls and isinstance(last_rolls, list) and all(isinstance(x, int) for x in last_rolls):
        return last_rolls

    # Give up
    raise ValueError("Could not coerce 'rolls' to a list[int].")


import ast
import json

# --- Strict tool schemas (override introspection if needed) ---
DICE_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": "Roll n_dice 6-sided dice and return the results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n_dice": {"type": "integer", "minimum": 1, "maximum": 100}
                },
                "required": ["n_dice"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sum_dice",
            "description": "Sum a list of dice rolls. Use data type list[int] for input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rolls": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                    }
                },
                "required": ["rolls"],
                "additionalProperties": False,
            },
        },
    },
]



def to_openai_tool_schema(tool_obj) -> Dict[str, Any]:
    """
    Convert FastMCP tool (name, description, input_schema) to OpenAI/Ollama 'tools' schema.
    """
    # fastmcp.Client.list_tools() returns Tool objects with .name, .description, .inputSchema
    return {
        "type": "function",
        "function": {
            "name": tool_obj.name,
            "description": tool_obj.description or "",
            "parameters": getattr(tool_obj, "inputSchema", {"type": "object", "properties": {}})
        }
    }

def ollama_chat(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Call Ollama's OpenAI-compatible Chat Completions with tools.
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        # (optional) temperature/top_p etc.
    }
    # print full request for debugging
    # print("=== Ollama request ===")
    # print(json.dumps(payload, indent=2, ensure_ascii=False))
    r = requests.post(f"{OLLAMA_BASE}/chat/completions", json=payload, timeout=120)
    r.raise_for_status()
    return r.json()

async def main():
    # 1) Connect to MCP server
    mcp = MCPClient(MCP_URL)
    async with mcp:
        await mcp.ping()

        # You can still introspect, but we prefer our strict schema for reliability:
        # mcp_tools = await mcp.list_tools()
        tools_schema = DICE_TOOLS_SCHEMA
        print("Available tools:", [t["function"]["name"] for t in tools_schema])

        # Memory of the last dice rolls
        last_rolls = None

        # 2) Start conversation (+ instruction to obey schemas)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. When calling tools, you MUST provide JSON arguments that exactly match the declared JSON Schemas. For sum_dice, 'rolls' MUST be an array of integers. If you receive an input validation error, correct your arguments and try again. Never make up your own arguments or data types. Always think step by step. Use as few tool calls as possible."
                ),
            },
            {"role": "user", "content": "Roll 20 dice and tell me their sum and individual values. think step by step."},
        ]

        while True:
            resp = ollama_chat(messages, tools_schema)
            choice = resp["choices"][0]
            msg = choice["message"]

            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls")
            })

            print("\n=== Model response ===")
            print(msg.get("content", "").strip())

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                print("\n=== Assistant ===")
                print(msg.get("content", "").strip())
                break

            for call in tool_calls:
                print("Tool call:", call)
                fn = call["function"]["name"]
                raw_args = call["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}


                # --- Fix/patch arguments before execution ---
                if fn == "sum_dice":
                    rolls_arg = args.get("rolls", None)
                    try:
                        args["rolls"] = coerce_rolls(rolls_arg, last_rolls)
                    except Exception as e:
                        # If we cannot coerce, tell the model precisely what is needed
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": fn,
                            "content": (
                                "Input validation error: 'rolls' must be an array of integers "
                                f"(got {type(rolls_arg).__name__}). Please call sum_dice with e.g. "
                                '{"rolls": [1,2,3]}.'
                            ),
                        })
                        continue  # proceed to next tool call

                # Execute via MCP
                result = await mcp.call_tool(fn, args)

                def normalize_tool_output(result) -> str:
                    """
                    Convert FastMCP tool results (TextContent, lists, dataclasses, etc.)
                    into a plain string for the Chat API's role='tool' content.
                    """
                    import json
                    from dataclasses import asdict, is_dataclass

                    # Prefer a concrete value
                    val = None
                    if hasattr(result, "content"):
                        val = result.content
                    elif hasattr(result, "data"):
                        val = result.data
                    else:
                        val = result

                    # TextContent helper
                    def extract_text(obj):
                        # direct primitives
                        if obj is None or isinstance(obj, (bool, int, float, str)):
                            return obj
                        # FastMCP TextContent-like
                        if hasattr(obj, "text") and isinstance(getattr(obj, "text"), str):
                            return obj.text
                        # dataclass → dict
                        if is_dataclass(obj):
                            return asdict(obj)
                        # pydantic-ish
                        if hasattr(obj, "model_dump"):
                            return obj.model_dump()
                        if hasattr(obj, "dict"):
                            try:
                                return obj.dict()
                            except Exception:
                                pass
                        # containers
                        if isinstance(obj, dict):
                            return {k: extract_text(v) for k, v in obj.items()}
                        if isinstance(obj, (list, tuple)):
                            return [extract_text(x) for x in obj]
                        # last resort
                        return repr(obj)

                    plain = extract_text(val)
                    if isinstance(plain, str):
                        return plain
                    return json.dumps(plain, ensure_ascii=False)


                out = normalize_tool_output(result)

                print("Tool output:", out)

                # Update memory if we just rolled
                if fn == "roll_dice":
                    try:
                        tmp = json.loads(out) if isinstance(out, str) else out
                        if isinstance(tmp, list) and all(isinstance(x, int) for x in tmp):
                            last_rolls = tmp
                    except Exception:
                        # If not JSON, try literal_eval
                        try:
                            tmp = ast.literal_eval(out)
                            if isinstance(tmp, list) and all(isinstance(x, int) for x in tmp):
                                last_rolls = tmp
                        except Exception:
                            pass

                # Feed tool output back to the model
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": fn,
                    "content": out,
                })


if __name__ == "__main__":
    asyncio.run(main())
