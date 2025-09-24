#!/usr/bin/env python3
"""
LangChain + LangGraph agent using MCP tools (dice server).
- No state_modifier (inject system prompt via messages)
- ChatOllama for tool binding
- sum_dice pre-coercion (coerce_rolls) + last_rolls memory
- Tool call logging

pip install -U langchain langgraph langchain-ollama langchain-mcp-adapters
"""

import asyncio
import ast
import json
from typing import Any, Dict, List

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_ollama import ChatOllama

from langchain.callbacks.base import AsyncCallbackHandler
from langchain_core.tools import BaseTool, Tool
from langchain_core.runnables.config import RunnableConfig


# ---------- your coercion helper ----------
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


# ---------- tool call logger ----------
class ToolLogger(AsyncCallbackHandler):
    async def on_tool_start(self, serialized, input_str, **kwargs):
        name = serialized.get("name", "<unknown_tool>")
        print("\n" + "=" * 80)
        print(f"🔧 Tool START: {name}")
        try:
            args = json.loads(input_str)
        except Exception:
            args = input_str
        pretty = json.dumps(args, ensure_ascii=False, indent=2) if isinstance(args, dict) else str(args)
        print("🧾 Args:", pretty)

    async def on_tool_end(self, output, **kwargs):
        try:
            parsed = json.loads(output)
            pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(output)
        if len(pretty) > 2000:
            pretty = pretty[:2000] + " …(truncated)"
        print("📦 Output:", pretty)
        print("🔧 Tool END")
        print("=" * 80 + "\n")


# ---------- wrap MCP tools to add your behavior ----------
def wrap_sum_and_track_rolls(tools: List[BaseTool]) -> List[BaseTool]:
    last_rolls_box = {"value": None}

    def update_last_rolls(out: Any):
        data = out
        if isinstance(out, str):
            for parser in (json.loads, ast.literal_eval):
                try:
                    data = parser(out)
                    break
                except Exception:
                    pass
        if isinstance(data, list) and all(isinstance(x, int) for x in data):
            last_rolls_box["value"] = data

    wrapped: List[BaseTool] = []

    for t in tools:
        name = t.name

        # Generic pass-through creator that preserves args_schema
        def mk_passthrough(tool_obj: BaseTool) -> BaseTool:
            async def _arun(**kwargs):
                return await tool_obj.ainvoke(kwargs)
            def _run(**kwargs):
                return tool_obj.invoke(kwargs)
            return Tool.from_function(
                name=tool_obj.name,
                description=tool_obj.description or "",
                coroutine=_arun,
                func=_run,
                args_schema=getattr(tool_obj, "args_schema", None),
            )

        if name == "roll_dice":
            async def roll_arun(**kwargs):
                out = await t.ainvoke(kwargs)
                update_last_rolls(out)
                return out
            def roll_run(**kwargs):
                out = t.invoke(kwargs)
                update_last_rolls(out)
                return out
            wrapped.append(
                Tool.from_function(
                    name=t.name,
                    description=t.description or "",
                    coroutine=roll_arun,
                    func=roll_run,
                    args_schema=getattr(t, "args_schema", None),
                )
            )
        elif name == "sum_dice":
            async def sum_arun(**kwargs):
                args = dict(kwargs)
                try:
                    args["rolls"] = coerce_rolls(args.get("rolls"), last_rolls_box["value"])
                except Exception as e:
                    return (
                        "Input validation error: 'rolls' must be an array of integers. "
                        f"Details: {e}"
                    )
                return await t.ainvoke(args)
            def sum_run(**kwargs):
                args = dict(kwargs)
                try:
                    args["rolls"] = coerce_rolls(args.get("rolls"), last_rolls_box["value"])
                except Exception as e:
                    return (
                        "Input validation error: 'rolls' must be an array of integers. "
                        f"Details: {e}"
                    )
                return t.invoke(args)
            wrapped.append(
                Tool.from_function(
                    name=t.name,
                    description=t.description or "",
                    coroutine=sum_arun,
                    func=sum_run,
                    args_schema=getattr(t, "args_schema", None),
                )
            )
        else:
            wrapped.append(mk_passthrough(t))

    return wrapped


async def main():
    # 1) MCP client & tools
    client = MultiServerMCPClient(
        {
            "dice": {
                "url": "http://localhost:8000/mcp",
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()
    tools = wrap_sum_and_track_rolls(tools)

    # 2) Chat model
    llm = ChatOllama(model="llama3.1", base_url="http://localhost:11434")

    # 3) Build agent (no state_modifier in this version)
    agent = create_react_agent(llm, tools)

    # 4) Invoke with system+user messages
    system_prompt = (
        "You are a helpful assistant. When calling tools, you MUST provide JSON arguments that exactly "
        "match the declared JSON Schemas. For sum_dice, 'rolls' MUST be an array of integers. If you "
        "receive an input validation error, correct your arguments and try again. Never invent argument "
        "types. Think step by step. Use as few tool calls as possible."
    )

    callbacks = [ToolLogger()]
    config = RunnableConfig(callbacks=callbacks)

    result = await agent.ainvoke(
        {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Roll 20 dice and tell me their sum and individual values. think step by step."},
            ]
        },
        config=config,
    )

    # 5) Print final response
    messages = result.get("messages", [])
    final_text = None
    if messages:
        last = messages[-1]
        final_text = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)

    print("\n" + "#" * 80)
    print("🤖 Final Assistant Response:")
    print(final_text or "(no text content)")
    print("#" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
