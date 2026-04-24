"""Web search via Tavily API."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = "https://api.tavily.com/search"


def web_search(query: str, max_results: int = 5) -> dict:
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not set")

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": True,
    }

    response = httpx.post(TAVILY_URL, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()

    results = []
    if answer := data.get("answer"):
        results.append({"title": "Resumen directo", "content": answer, "url": ""})

    for r in data.get("results", [])[:max_results]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": (r.get("content") or "")[:600],
        })

    logger.info("[web_search] query=%r results=%d", query, len(results))
    return {"query": query, "results": results}
