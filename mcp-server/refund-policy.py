#!/usr/bin/env python3
"""
Return & Refund Policy MCP Server

This MCP (Model Context Protocol) server exposes tools to retrieve and query a
specific Return & Refund Policy document (provided below in Markdown).

Tools provided:
- get_policy(): Return the entire policy as Markdown
- list_sections(levels=6): List headings (H1–Hn) with levels and anchors
- get_section(title_or_anchor): Return the Markdown content under a heading
- search_policy(query, max_results=8, context_chars=120): Keyword search with snippets

Run:  python mcp_policy_server.py
Then register this server in your MCP-compatible client (e.g., Claude Desktop or an
OpenAI MCP client) by pointing to the command above.
"""

from __future__ import annotations
import re
import logging
from typing import List, Dict, Any
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("policy-mcp")

mcp = FastMCP("Return & Refund Policy Server")
_tool = mcp.tool

# --- The Markdown policy content to serve ---
POLICY_MD = r"""# Return & Refund Policy


"""

# --- helpers ---
_heading_re = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")


def _slugify(title: str) -> str:
    s = title.strip().lower()
    # Remove inline formatting characters and HTML tags
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[*_`~]+", "", s)
    # Keep alphanum, spaces, & dashes
    s = re.sub(r"[^a-z0-9\-\s]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def _parse_headings(md: str) -> List[Dict[str, Any]]:
    """Return a flat list of headings with (level, title, anchor, line_idx)."""
    headings: List[Dict[str, Any]] = []
    for idx, line in enumerate(md.splitlines()):
        m = _heading_re.match(line)
        if m:
            level = len(m.group("hashes"))
            title = m.group("title").strip()
            headings.append({
                "level": level,
                "title": title,
                "anchor": _slugify(title),
                "line": idx,
            })
    return headings


def _section_by_title(md: str, title_or_anchor: str) -> Dict[str, Any] | None:
    headings = _parse_headings(md)
    target = None
    key = title_or_anchor.strip().lower()
    for h in headings:
        if h["title"].strip().lower() == key or h["anchor"] == key:
            target = h
            break
    if not target:
        return None

    lines = md.splitlines()
    start = target["line"]
    level = target["level"]

    # Find the next heading at same or higher level to delimit the section
    end = len(lines)
    for h in headings:
        if h["line"] > start and h["level"] <= level:
            end = h["line"]
            break

    content = "\n".join(lines[start:end]).rstrip() + "\n"
    return {
        "title": target["title"],
        "anchor": target["anchor"],
        "level": level,
        "markdown": content,
        "start_line": start,
        "end_line": end - 1,
    }


# --- Tools ---
@mcp.tool()
def get_policy() -> Dict[str, Any]:
    """Return the entire Return & Refund Policy in Markdown."""
    return {"markdown": POLICY_MD, "length": len(POLICY_MD)}


@mcp.tool()
def list_sections(levels: int = 6) -> Dict[str, Any]:
    """List headings (H1..Hn). Set `levels` to limit depth (default: 6)."""
    hs = [h for h in _parse_headings(POLICY_MD) if h["level"] <= max(1, int(levels))]
    return {"sections": hs}


@mcp.tool()
def get_section(title_or_anchor: str) -> Dict[str, Any]:
    """Return the Markdown content under the given section title or anchor slug.

    Examples of valid inputs:
      - "Return Conditions"
      - "matrix-of-conditions" (anchor)
      - "refund-process-if-return-is-accepted-or-order-is-cancelled"
    """
    sec = _section_by_title(POLICY_MD, title_or_anchor)
    if not sec:
        return {"error": f"Section not found: {title_or_anchor}"}
    return sec


@mcp.tool()
def search_policy(query: str, max_results: int = 8, context_chars: int = 120) -> Dict[str, Any]:
    """Search the policy for `query` (case-insensitive) and return snippets.

    Parameters
    ----------
    query : str
        The search query string.
    max_results : int
        Maximum matches to return (default 8).
    context_chars : int
        Number of context characters to include on each side of the match (default 120).
    """
    q = (query or "").strip()
    if not q:
        return {"error": "Empty query"}

    text = POLICY_MD
    matches = []
    for m in re.finditer(re.escape(q), text, flags=re.IGNORECASE):
        start, end = m.start(), m.end()
        left = max(0, start - context_chars)
        right = min(len(text), end + context_chars)
        snippet = text[left:right]
        # Compute line number and section info
        line_no = text[:start].count("\n")
        sec_info = None
        for h in reversed(_parse_headings(text)):
            if h["line"] <= line_no:
                sec_info = {"title": h["title"], "anchor": h["anchor"], "level": h["level"]}
                break
        matches.append({
            "match": text[start:end],
            "position": {"start": start, "end": end, "line": line_no},
            "section": sec_info,
            "snippet": snippet,
        })
        if len(matches) >= max(1, int(max_results)):
            break

    return {"query": q, "results": matches}


if __name__ == "__main__":
    logger.info("Starting Return & Refund Policy MCP server…")
    mcp.run()
