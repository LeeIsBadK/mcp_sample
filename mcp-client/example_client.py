#!/usr/bin/env python3
# weather_server.py
from typing import Any
import sys
import argparse
import os
import httpx
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("Weather MCP Server")

# Constants
NWS_API_BASE = "https://api.weather.gov"
USER_AGENT = "weather-mcp/1.0 (+https://example.com)"

# ---------------- Utilities ----------------

async def make_nws_request(url: str) -> dict[str, Any] | None:
    """Make a request to the NWS API with proper error handling."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers, timeout=30.0)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

def format_alert(feature: dict) -> str:
    props = feature.get("properties", {})
    return (
        f"Event: {props.get('event', 'Unknown')}\n"
        f"Area: {props.get('areaDesc', 'Unknown')}\n"
        f"Severity: {props.get('severity', 'Unknown')}\n"
        f"Description: {props.get('description', 'No description available')}\n"
        f"Instructions: {props.get('instruction', 'No instructions provided')}"
    )

# ---------------- MCP Tools ----------------

@mcp.tool()
async def get_alerts(state: str) -> str:
    """Get active weather alerts for a US state (2-letter code, e.g. CA, NY)."""
    url = f"{NWS_API_BASE}/alerts/active/area/{state}"
    data = await make_nws_request(url)
    if not data or "features" not in data:
        return "Unable to fetch alerts or response invalid."
    feats = data.get("features", [])
    if not feats:
        return f"No active alerts for {state}."
    return "\n\n---\n\n".join(format_alert(f) for f in feats)

@mcp.tool()
async def get_forecast(latitude: float, longitude: float) -> str:
    """Get the next periods forecast for given latitude,longitude."""
    points_url = f"{NWS_API_BASE}/points/{latitude},{longitude}"
    points = await make_nws_request(points_url)
    if not points or "properties" not in points or "forecast" not in points["properties"]:
        return "Unable to resolve forecast grid for this location."
    forecast_url = points["properties"]["forecast"]
    forecast = await make_nws_request(forecast_url)
    if not forecast or "properties" not in forecast or "periods" not in forecast["properties"]:
        return "Unable to fetch detailed forecast."
    periods = forecast["properties"]["periods"][:5]  # show next 5 periods
    parts = []
    for p in periods:
        parts.append(
            f"{p.get('name','Period')}:\n"
            f"  Temperature: {p.get('temperature','?')}°{p.get('temperatureUnit','')}\n"
            f"  Wind: {p.get('windSpeed','?')} {p.get('windDirection','')}\n"
            f"  Forecast: {p.get('detailedForecast','N/A')}"
        )
    return "\n\n---\n\n".join(parts)

# ---------------- Entrypoint ----------------

def main():
    parser = argparse.ArgumentParser(description="Weather MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    transport = os.environ.get("MCP_TRANSPORT", args.transport)
    port = int(os.environ.get("PORT", args.port))

    # ⚠️ For stdio, DO NOT print to stdout. Use stderr for human logs.
    print(f"Starting Weather MCP Server (transport={transport}, port={port})", file=sys.stderr)

    if transport == "streamable-http":
        # FastMCP currently uses 8000 for streamable-http
        print("HTTP endpoint: http://127.0.0.1:8000/mcp", file=sys.stderr)
        print("Tools: get_alerts(state), get_forecast(latitude, longitude)", file=sys.stderr)

    mcp.run(transport=transport)  # blocks

if __name__ == "__main__":
    main()
