#!/usr/bin/env python3
# weather_client.py (robust)
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any, Dict, Optional

STATE_RE = re.compile(r"^[A-Za-z]{2}$")
LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

class MCPClientStdio:
    """Minimal MCP stdio JSON-RPC client with robust tools/list compatibility."""
    def __init__(self, cmd: str):
        self.cmd = shlex.split(cmd)
        self.proc: Optional[subprocess.Popen[str]] = None
        self._id = 0

    def start(self):
        if self.proc:
            return
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.proc = None

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("Server process not started")

        req_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"No response from server. Stderr:\n{err}")
            line = line.strip()
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # ignore stray non-JSON lines
                continue

            if isinstance(msg, dict) and msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"RPC error: {msg['error']}")
                return msg.get("result", {})

    def _notify(self, method: str, params: Dict[str, Any]):
        """JSON-RPC notification (no id, no response expected)."""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("Server process not started")
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    # -------- MCP helpers --------

    def initialize(self):
        res = self._send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "weather-client", "version": "0.1"},
                "capabilities": {},
            },
        )
        # Some servers expect an 'initialized' notification after initialize
        try:
            self._notify("initialized", {})
        except Exception:
            pass
        return res

    def tools_list(self):
        # Try modern shape
        attempts = [
            ("tools/list", {}),                    # spec-conformant
            ("tools/list", {"cursor": None}),      # some FastMCP builds want this
            ("list_tools", {}),                    # legacy
        ]
        last_err = None
        for method, params in attempts:
            try:
                res = self._send(method, params)
                # Normalize different shapes
                if isinstance(res, dict):
                    if "tools" in res:
                        return res["tools"]
                    # Sometimes { "cursor": ..., "tools": [...] }
                    if "cursor" in res and "tools" in res:
                        return res["tools"]
                if isinstance(res, list):
                    return res
            except Exception as e:
                last_err = e
        raise last_err or RuntimeError("Unable to list tools")

    def tools_call(self, name: str, arguments: Dict[str, Any]):
        # Spec method
        try:
            return self._send("tools/call", {"name": name, "arguments": arguments})
        except Exception as e:
            # Some custom servers expose the tool as a direct method; try fallback.
            try:
                return self._send(name, arguments)
            except Exception:
                raise e

def print_usage():
    print("Weather MCP Client (stdio)")
    print("==========================")
    print("Usage:")
    print('  python weather_client.py --server "python3 weather_server.py --transport stdio"')
    print()
    print("Commands:")
    print("  tools             -> list tools")
    print("  CA                -> get_alerts(state='CA')")
    print("  37.77,-122.42     -> get_forecast(latitude=37.77, longitude=-122.42)")
    print("  quit / exit       -> leave\n")

def parse_intent(text: str):
    s = text.strip()
    if STATE_RE.match(s):
        return ("get_alerts", {"state": s.upper()})
    m = LATLON_RE.match(s)
    if m:
        lat = float(m.group(1))
        lon = float(m.group(2))
        return ("get_forecast", {"latitude": lat, "longitude": lon})
    return None

def main():
    parser = argparse.ArgumentParser(description="Weather MCP Client")
    parser.add_argument(
        "--server",
        required=True,
        help='Command to start the MCP server, e.g. "python3 weather_server.py --transport stdio"',
    )
    args = parser.parse_args()

    client = MCPClientStdio(args.server)
    try:
        client.start()
        init_res = client.initialize()
        print(f"Initialized:\n{json.dumps(init_res, indent=2)}\n")

        tools = client.tools_list()
        print("Available tools:")
        print(json.dumps(tools, indent=2), "\n")

        print("Enter a two-letter US state code for alerts (e.g., CA),")
        print("or enter 'lat,lon' for forecast (e.g., 37.77,-122.42).")
        print("Type 'tools' to list tools again, or 'quit' to exit.\n")

        while True:
            try:
                line = input("weather> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                break
            if line.lower() == "tools":
                tools = client.tools_list()
                print(json.dumps(tools, indent=2))
                continue

            intent = parse_intent(line)
            if not intent:
                print("Unrecognized input. Try 'CA' or '37.77,-122.42'.")
                continue

            tool, args_dict = intent
            try:
                result = client.tools_call(tool, args_dict)
                if isinstance(result, (dict, list)):
                    print(json.dumps(result, indent=2), "\n")
                else:
                    print(str(result) + "\n")
            except Exception as e:
                print(f"Error: {e}\n")

    finally:
        client.stop()

if __name__ == "__main__":
    print_usage()
    main()
