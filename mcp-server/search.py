#!/usr/bin/env uv run
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from mcp.server.fastmcp import FastMCP
from duckduckgo_search import DDGS

# -----------------------------
# Basic setup
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DDG MCP")

mcp = FastMCP("DuckDuckGo Search Assistant")
tool = mcp.tool

# -----------------------------
# Helpers
# -----------------------------
def _iso_or_none(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def _normalize_ddg_news(items: List[dict]) -> List[dict]:
    """
    DDG news items typically include fields: title, date, body, url, image, source, etc.
    """
    out = []
    for it in items:
        # date can be datetime or string; DDGS returns datetime for .news()
        pub = it.get("date")
        if isinstance(pub, str):
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:
                pub_dt = None
        else:
            pub_dt = pub if isinstance(pub, datetime) else None

        out.append({
            "title": it.get("title"),
            "source": it.get("source"),
            "url": it.get("url"),
            "published_at": _iso_or_none(pub_dt),
            "description": it.get("body"),
            "image": it.get("image"),
        })
    return out

def _normalize_ddg_web(items: List[dict]) -> List[dict]:
    """
    DDG web results fields: title, href, body, etc.
    """
    out = []
    for it in items:
        out.append({
            "title": it.get("title"),
            "url": it.get("href"),
            "snippet": it.get("body"),
        })
    return out

def _map_since_to_ddg_timelimit(since: Optional[str]) -> Optional[str]:
    """
    Map friendly strings to DDG timelimit values:
      - 'day'/'1d' -> 'd'
      - 'week'/'7d' -> 'w'
      - 'month'/'30d' -> 'm'
      - 'year'/'365d' -> 'y'
    """
    if not since:
        return None
    s = since.strip().lower()
    if s in {"d", "day", "1d", "24h"}:
        return "d"
    if s in {"w", "week", "7d"}:
        return "w"
    if s in {"m", "month", "30d"}:
        return "m"
    if s in {"y", "year", "365d"}:
        return "y"
    # If someone passes 'h' etc., just return None to avoid errors
    return None

# -----------------------------
# MCP Tools
# -----------------------------
@tool()
async def search_news(
    query: str,
    region: str = "th-th",
    safesearch: str = "moderate",  # 'off' | 'moderate' | 'strict'
    since: Optional[str] = None,   # 'day'|'week'|'month'|'year' (or d/w/m/y)
    max_results: int = 25
) -> Dict[str, Any]:
    """
    Search DuckDuckGo News.

    Args:
      query: Search keywords (e.g., "Bangkok floods", "AI regulation")
      region: e.g., 'th-th', 'us-en', 'uk-en', 'wt-wt' (worldwide)
      safesearch: 'off' | 'moderate' | 'strict'
      since: Limit by recency: 'day'/'week'/'month'/'year' (or d/w/m/y)
      max_results: 1..100

    Returns:
      {
        "status": "success"|"error",
        "backend": "duckduckgo",
        "total": int,
        "articles": [{"title","source","url","published_at","description","image"}]
      }
    """
    try:
        tl = _map_since_to_ddg_timelimit(since)
        max_results = max(1, min(int(max_results), 100))
        with DDGS() as ddgs:
            results = list(ddgs.news(
                keywords=query,
                region=region,
                safesearch=safesearch,
                timelimit=tl,
                max_results=max_results
            ))
        articles = _normalize_ddg_news(results)
        return {"status": "success", "backend": "duckduckgo", "total": len(articles), "articles": articles}
    except Exception as e:
        logger.exception("search_news error")
        return {"status": "error", "error_message": str(e)}

@tool()
async def search_web(
    query: str,
    region: str = "th-th",
    safesearch: str = "moderate",
    max_results: int = 25
) -> Dict[str, Any]:
    """
    General DuckDuckGo web search (not just news).
    Useful for background/context when news is sparse.

    Args:
      query: Search keywords
      region: e.g., 'th-th', 'us-en', 'wt-wt'
      safesearch: 'off'|'moderate'|'strict'
      max_results: 1..100

    Returns:
      {
        "status": "success"|"error",
        "backend": "duckduckgo",
        "total": int,
        "results": [{"title","url","snippet"}]
      }
    """
    try:
        max_results = max(1, min(int(max_results), 20))
        with DDGS() as ddgs:
            results = list(ddgs.text(
                keywords=query,
                region=region,
                safesearch=safesearch,
                max_results=max_results
            ))
        normalized = _normalize_ddg_web(results)
        return {"status": "success", "backend": "duckduckgo", "total": len(normalized), "results": normalized}
    except Exception as e:
        logger.exception("search_web error")
        return {"status": "error", "error_message": str(e)}

# -----------------------------
# Entry
# -----------------------------
if __name__ == "__main__":
    try:
        logger.info("Starting DuckDuckGo MCP server")
        mcp.run()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Fatal server error: {e}")
        raise
