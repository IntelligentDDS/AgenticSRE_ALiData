"""Streamable-HTTP MCP client wrapper.

Single-purpose transport. No business semantics. Each call_tool() opens
an MCP session, invokes one tool, parses the JSON response, closes the
session. (We re-open per call rather than holding a long-lived session
because the underlying anyio-based session is awkward to share across
sync call sites; latency is acceptable for the per-case granularity
the adapter operates at.)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .mcp_exceptions import (
    MCPError, MCPTransportError, MCPProtocolError, MCPToolError,
)

logger = logging.getLogger(__name__)


async def _open_session(endpoint: str, timeout: float):
    """Async helper for opening an MCP session over streamable-http.

    Returns a tuple (session, closer) where `closer` is a coroutine that
    must be awaited to tear down transport resources.
    """
    cm = streamablehttp_client(endpoint)
    read, write, _meta = await cm.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await asyncio.wait_for(session.initialize(), timeout=timeout)

    async def closer():
        await session.__aexit__(None, None, None)
        await cm.__aexit__(None, None, None)

    return session, closer


def _decode_tool_result(result) -> Any:
    """Decode an mcp.types.CallToolResult into a Python value."""
    if getattr(result, "isError", False):
        text = ""
        for c in getattr(result, "content", []) or []:
            text += getattr(c, "text", "") or ""
        lower = text.lower()
        code = "ToolError"
        for kw, c in (("unauthorized", "Unauthorized"),
                      ("not found", "EntityNotFound"),
                      ("syntax", "SyntaxError"),
                      ("invalid query", "QueryError")):
            if kw in lower:
                code = c
                break
        raise MCPToolError(
            message=text or "tool returned isError=True",
            tool_name="",
            code=code,
            raw={"isError": True, "text": text},
        )

    contents = getattr(result, "content", []) or []
    if not contents:
        return None

    text_parts = []
    for c in contents:
        t = getattr(c, "text", None)
        if t is None:
            continue
        text_parts.append(t)
    raw = "".join(text_parts) if text_parts else None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


class MCPClient:
    """Transport-only MCP client."""

    def __init__(self, endpoint: str, timeout: float = 60.0, retries: int = 3):
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = retries

    def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Synchronously invoke an MCP tool. Returns decoded JSON or text.

        Safe to call from sync OR async contexts. When a loop is already
        running (caller is inside ``await``), we run the coroutine in a
        dedicated worker thread with its own event loop. When no loop is
        running, we use ``asyncio.run`` directly.

        Cancellation safety: if the calling coroutine is cancelled
        (frontend aborted the HTTP request, etc.), we surface this as
        an ``MCPTransportError`` rather than letting CancelledError
        propagate raw — the latter leaves FastAPI with a stale event-loop
        state and returns an empty body.
        """
        try:
            try:
                asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, self._call_tool_async(name, args))
                    return future.result(timeout=self.timeout + 30)
            except RuntimeError:
                return asyncio.run(self._call_tool_async(name, args))
        except asyncio.CancelledError as e:
            raise MCPTransportError(f"call_tool({name}) cancelled mid-flight") from e
        except Exception as e:
            # Wrap any other unexpected failure so callers get a typed error.
            if isinstance(e, (MCPTransportError, MCPProtocolError, MCPToolError)):
                raise
            raise MCPTransportError(f"call_tool({name}) failed: {e}") from e

    async def _call_tool_async(self, name: str, args: Dict[str, Any]) -> Any:
        last_exc: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            try:
                session, closer = await _open_session(self.endpoint, self.timeout)
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(name, args),
                        timeout=self.timeout,
                    )
                    return _decode_tool_result(result)
                finally:
                    try:
                        await closer()
                    except Exception:
                        logger.debug("close error ignored", exc_info=True)
            except MCPToolError:
                raise
            except asyncio.TimeoutError as e:
                last_exc = e
                logger.warning("MCP timeout (attempt %d/%d) tool=%s",
                               attempt + 1, self.retries + 1, name)
            except (ConnectionError, OSError) as e:
                last_exc = e
                logger.warning("MCP transport error (attempt %d/%d) tool=%s: %s",
                               attempt + 1, self.retries + 1, name, e)
            except Exception as e:
                raise MCPProtocolError(f"protocol error calling {name}: {e}") from e

            if attempt < self.retries:
                await asyncio.sleep(2 ** attempt)

        raise MCPTransportError(
            f"transport failed after {self.retries + 1} attempts: {last_exc}"
        ) from last_exc

    def list_tools(self) -> list:
        """Return list of available MCP tool names. Safe in sync OR async caller."""
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self._list_tools_async())
                return future.result(timeout=self.timeout + 30)
        except RuntimeError:
            return asyncio.run(self._list_tools_async())

    async def _list_tools_async(self) -> list:
        session, closer = await _open_session(self.endpoint, self.timeout)
        try:
            res = await session.list_tools()
            return [t.name for t in res.tools]
        finally:
            await closer()

    def close(self) -> None:
        return None
