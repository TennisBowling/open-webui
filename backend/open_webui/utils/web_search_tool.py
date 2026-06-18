"""
Built-in Web Search Tools for Open WebUI
Provides web search and fetch capabilities as native tools that models can call directly.
Uses Exa API for search and Jina Reader for content fetching.
"""

import asyncio
import logging
import time
from typing import Optional, List, Tuple
from pydantic import BaseModel
from fastapi import Request

from open_webui.retrieval.web.exa import search_exa
from open_webui.retrieval.web.jina import (
    DEFAULT_JINA_READER_API_BASE_URL,
    fetch_jina_contents,
    normalize_jina_reader_api_base_url,
)

log = logging.getLogger(__name__)


def _classify_web_error_reason(message: str) -> str:
    """Turn a raw provider exception string into a short, human subline shown on
    the collapsed tool-call row (e.g. 'HTTP 402 (no credits)')."""
    text = (message or "").lower()
    if "402" in text:
        return "HTTP 402 (no credits)"
    if "401" in text or "unauthorized" in text:
        return "HTTP 401 (unauthorized)"
    if "403" in text or "forbidden" in text:
        return "HTTP 403 (forbidden)"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "HTTP 429 (rate limited)"
    if "timed out" in text or "timeout" in text:
        return "timed out"
    # Surface a generic 5xx if present; otherwise a trimmed message.
    for code in ("500", "502", "503", "504"):
        if code in text:
            return f"HTTP {code} (server error)"
    trimmed = (message or "").strip().replace("\n", " ")
    return (trimmed[:120] + "…") if len(trimmed) > 121 else (trimmed or "request failed")


class WebSearchTools:
    """
    Built-in tools that provide web search and content fetching functionality.
    
    Provides two tools:
    1. web_search - Search the web for information, returns URLs with snippets
    2. web_fetch - Fetch full content from specific URLs
    
    Configuration is managed through the admin panel web search settings.
    """

    class Valves(BaseModel):
        """Configuration placeholder - settings are managed via admin panel"""
        pass

    def __init__(self):
        self.valves = self.Valves()
        self._jina_usage_lock = asyncio.Lock()
        self._exa_status_lock = asyncio.Lock()

    async def web_search(
        self,
        query: str,
        __request__: Optional[Request] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the web for information about a query.

        This tool searches the web using Exa and returns relevant results including
        titles, URLs, and content snippets. Use this to find relevant pages, then
        use web_fetch to get full content from the most relevant URLs.

        Args:
            query: The search query to execute. Should be clear and specific.

        Returns:
            Formatted search results containing:
            - List of results with titles, URLs, and snippets
            - Use the URLs with web_fetch to get full content
        """
        log.info(f"WEB SEARCH: query='{query}'")

        if not __request__:
            log.error("WEB SEARCH: Request context not available")
            return "Error: Request context not available"

        config = __request__.app.state.config

        # Ordered list of (slot, key) to try. Slot is the 1-based key index used
        # in the admin status panel. Empty keys are dropped.
        keys: List[Tuple[str, str]] = []
        primary = (getattr(config, "EXA_API_KEY", "") or "").strip()
        secondary = (getattr(config, "EXA_API_KEY_2", "") or "").strip()
        if primary:
            keys.append(("1", primary))
        if secondary:
            keys.append(("2", secondary))

        if not keys:
            return {
                "content": (
                    "Error: Exa API key not configured. Please set it in "
                    "Admin Settings > Web Search."
                ),
                "_owui_meta": {
                    "error": True,
                    "reason": "API key not configured",
                },
            }

        # Search settings (shared across keys).
        num_results = getattr(config, "EXA_SEARCH_NUM_RESULTS", 10)
        search_type = getattr(config, "EXA_SEARCH_TYPE", "auto")
        include_domains = getattr(config, "EXA_INCLUDE_DOMAINS", [])
        exclude_domains = getattr(config, "EXA_EXCLUDE_DOMAINS", [])
        http_session = getattr(__request__.app.state, "http_session", None)

        last_error_reason = ""
        for attempt_idx, (slot, api_key) in enumerate(keys):
            try:
                # Reuse the app-level aiohttp session so a burst of subagent web
                # searches does not pay a fresh TCP/TLS setup cost per tool call.
                results = await search_exa(
                    api_key=api_key,
                    query=query,
                    num_results=num_results,
                    search_type=search_type,
                    include_domains=include_domains if include_domains else None,
                    exclude_domains=exclude_domains if exclude_domains else None,
                    session=http_session,
                )
            except Exception as e:
                # This key failed (credit/auth/timeout/etc.). Record its health,
                # then fall through to the next key if one exists.
                reason = _classify_web_error_reason(str(e))
                last_error_reason = reason
                log.error(
                    f"Web search error on Exa key {slot}: {e}", exc_info=True
                )
                await self._record_exa_key_status(config, slot, reason)
                continue

            # Success (an EMPTY result set is still success — never advance to the
            # backup key just because a query had no hits).
            await self._record_exa_key_status(config, slot, None)

            content = self._format_search_results(query, results)
            if attempt_idx == 0:
                return content

            # A backup key was used because the primary failed. Surface a clean,
            # non-error advisory in the chat row.
            return {
                "content": content,
                "_owui_meta": {
                    "notice": f"Primary key failed — used backup key {slot}",
                },
            }

        # Every configured key failed.
        return {
            "content": f"Error performing web search: {last_error_reason}",
            "_owui_meta": {
                "error": True,
                "reason": last_error_reason or "all Exa keys failed",
            },
        }

    def _format_search_results(self, query: str, results) -> str:
        """Format Exa search results into the markdown the model reads."""
        if not results:
            return f"No results found for query: '{query}'"

        formatted_results = [
            f"## Search Results for: {query}\n",
            f"Found {len(results)} results.\n\n",
        ]

        for idx, result in enumerate(results, 1):
            formatted_results.append(f"### Result {idx}\n")
            if result.title:
                formatted_results.append(f"**Title:** {result.title}\n")
            formatted_results.append(f"**URL:** {result.link}\n")
            if result.snippet:
                formatted_results.append(f"**Snippet:** {result.snippet}\n")
            formatted_results.append("\n")

        formatted_results.append("\n---\n")
        formatted_results.append(
            "*Use web_fetch with specific URLs to get full page content.*\n"
        )

        return "".join(formatted_results)

    async def _record_exa_key_status(
        self, config, slot: str, reason: Optional[str]
    ) -> None:
        """Persist per-key Exa health for the admin panel. `reason=None` clears
        the slot (key healthy). Writes only when the status actually changes, so
        a steady stream of healthy searches doesn't hammer the DB-backed config."""
        async with self._exa_status_lock:
            try:
                current = getattr(config, "EXA_KEY_STATUS", {}) or {}
                if not isinstance(current, dict):
                    current = {}
            except Exception:
                current = {}

            if reason is None:
                if slot not in current:
                    return  # already healthy → no write
                new_status = {k: v for k, v in current.items() if k != slot}
            else:
                existing = current.get(slot)
                if isinstance(existing, dict) and existing.get("error") == reason:
                    return  # same error already recorded → no write
                new_status = dict(current)
                new_status[slot] = {"error": reason, "at": int(time.time())}

            config.EXA_KEY_STATUS = new_status

    async def web_fetch(
        self,
        urls: str,
        __request__: Optional[Request] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch the full content from one or more URLs.

        This tool retrieves the full text content from web pages. Use this after
        web_search to get detailed information from specific pages.

        Args:
            urls: One or more URLs to fetch, separated by commas or newlines.
                  Example: "https://example.com/page1, https://example.com/page2"

        Returns:
            Full text content from each URL, formatted for easy reading.
        """
        log.info(f"WEB FETCH: urls='{urls}'")

        if not __request__:
            log.error("WEB FETCH: Request context not available")
            return "Error: Request context not available"

        try:
            config = __request__.app.state.config
            api_key = (getattr(config, "JINA_API_KEY", "") or "").strip()
            api_base_url = normalize_jina_reader_api_base_url(
                getattr(
                    config,
                    "JINA_READER_API_BASE_URL",
                    DEFAULT_JINA_READER_API_BASE_URL,
                )
            )
            hosted_jina_api_base_url = normalize_jina_reader_api_base_url(
                DEFAULT_JINA_READER_API_BASE_URL
            )

            if (
                not api_key
                and api_base_url.rstrip("/").lower()
                == hosted_jina_api_base_url.rstrip("/").lower()
            ):
                return {
                    "content": (
                        "Error: Jina API key not configured. Please set it in "
                        "Admin Settings > Web Search, or configure a self-hosted "
                        "Jina Reader API base URL."
                    ),
                    "_owui_meta": {
                        "error": True,
                        "reason": "API key not configured",
                    },
                }

            # Parse URLs from the input string
            url_list = self._parse_urls(urls)

            if not url_list:
                return "Error: No valid URLs provided. Please provide URLs separated by commas or newlines."

            # Limit to reasonable number of URLs
            max_urls = 5
            if len(url_list) > max_urls:
                log.warning(f"WEB FETCH: Limiting from {len(url_list)} to {max_urls} URLs")
                url_list = url_list[:max_urls]

            # Get Jina Reader settings from config
            viewport_width = getattr(config, 'JINA_READER_VIEWPORT_WIDTH', 1280)
            viewport_height = getattr(config, 'JINA_READER_VIEWPORT_HEIGHT', 12000)
            timeout_seconds = getattr(config, 'JINA_READER_TIMEOUT', 30)

            # Fetch content. Reuse the app-level aiohttp session; the per-call
            # semaphore below still limits Jina concurrency for this tool
            # invocation, while the shared connector avoids repeated TLS/DNS
            # setup across many simultaneous subagents.
            http_session = getattr(__request__.app.state, "http_session", None)
            results, fetch_errors = await fetch_jina_contents(
                api_key=api_key,
                urls=url_list,
                api_base_url=api_base_url,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_urls,
                session=http_session,
            )

            total_usage_tokens = sum(getattr(result, 'usage_tokens', 0) for result in results)
            if total_usage_tokens > 0:
                await self._increment_jina_token_usage(config, total_usage_tokens)

            if not results:
                # Distinguish a genuine empty fetch from an auth/credit failure:
                # if every URL raised, surface it as an error so the chat row and
                # model both see the real cause instead of a bland "no content".
                if fetch_errors:
                    reason = _classify_web_error_reason(fetch_errors[0])
                    return {
                        "content": f"Error fetching content: {reason}",
                        "_owui_meta": {"error": True, "reason": reason},
                    }
                return "Could not fetch content from the provided URLs."

            # Format results for the model
            formatted_results = [
                f"## Fetched Content\n",
                f"Retrieved content from {len(results)} URL(s).\n\n",
            ]

            for idx, result in enumerate(results, 1):
                formatted_results.append(f"### Page {idx}: {result.title or 'Untitled'}\n")
                formatted_results.append(f"**URL:** {result.url}\n")
                if result.published_date:
                    formatted_results.append(f"**Published:** {result.published_date}\n")
                if result.author:
                    formatted_results.append(f"**Author:** {result.author}\n")
                if result.description:
                    formatted_results.append(f"**Description:** {result.description}\n")
                formatted_results.append(f"\n**Content:**\n\n{result.text}\n")
                if result.images:
                    formatted_results.append("\n**Images:**\n")
                    for label, image_url in result.images.items():
                        formatted_results.append(f"- {label}: {image_url}\n")
                formatted_results.append("\n---\n\n")

            return "".join(formatted_results)

        except Exception as e:
            log.error(f"Web fetch error: {e}", exc_info=True)
            reason = _classify_web_error_reason(str(e))
            return {
                "content": f"Error fetching content: {str(e)}",
                "_owui_meta": {"error": True, "reason": reason},
            }

    def _parse_urls(self, urls_input: str) -> List[str]:
        """Parse URLs from a string that may contain commas, newlines, or spaces."""
        # Replace common separators with a standard delimiter
        normalized = urls_input.replace('\n', ',').replace('\r', ',').replace(' ', ',')
        
        # Split and filter
        url_list = []
        for url in normalized.split(','):
            url = url.strip()
            if url and (url.startswith('http://') or url.startswith('https://')):
                url_list.append(url)
        
        return url_list

    async def _increment_jina_token_usage(self, config, tokens: int) -> None:
        """Persistently add Jina Reader token usage for the configured API key."""
        async with self._jina_usage_lock:
            try:
                current_usage = int(getattr(config, 'JINA_READER_TOKEN_USAGE', 0) or 0)
            except Exception:
                current_usage = 0
            config.JINA_READER_TOKEN_USAGE = current_usage + int(tokens)


# Singleton instance
_web_search_tools_instance = None


def get_web_search_tools_instance():
    """Get or create the singleton WebSearchTools instance"""
    global _web_search_tools_instance
    if _web_search_tools_instance is None:
        _web_search_tools_instance = WebSearchTools()
    return _web_search_tools_instance
