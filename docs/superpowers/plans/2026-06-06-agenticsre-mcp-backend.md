# AgenticSRE_MCP Backend Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate AgenticSRE backend data reads from native (Prom/ES/Jaeger) and AliData SDK paths to Alibaba Cloud Observability MCP Server, via a thin `MCPClient` + three drop-in `SRETool` adapters that preserve existing tool names (`prometheus` / `elasticsearch` / `jaeger`) and output shapes — so 16 agents, web_app, and eval require zero behavioral change.

**Architecture:** Hybrid layered (option C). `tools/mcp_client.py` owns streamable-http transport via the `mcp` Python SDK. `tools/mcp_observability.py` defines `MCPMetricTool` / `MCPLogTool` / `MCPTraceTool` (SRETool subclasses, names `prometheus` / `elasticsearch` / `jaeger`), each calling the relevant `umodel_*` tools and reshaping responses to Prometheus / Elasticsearch / Jaeger formats. MCP server runs as a separate `mcp-server-aliyun-observability` Python process on `:7980` via streamable-http.

**Tech Stack:** Python 3.13, `mcp>=1.12.0` (already pinned via `mcp[cli]>=1.1.0` in requirements), `mcp-server-aliyun-observability==1.0.8` (new dep, MCP server), `pytest` (already in tests/), `httpx>=0.27.0` (already pinned, used by mcp SDK), Docker 26 (optional, for containerized MCP server), source host 8.152.156.185, source repo `/root/cpf/AgenticSRE_ALiData` (non-git), target repo `/root/cpf/AgenticSRE_MCP` (non-git, init with `git init` for commit tracking during this plan).

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `tools/mcp_client.py` | `MCPClient` class: streamable-http connect, `call_tool(name, args, timeout)`, transport retry. Zero business semantics. |
| `tools/mcp_exceptions.py` | Exception hierarchy: `MCPError → MCPTransportError / MCPProtocolError / MCPToolError → EntityNotFound / AuthError / QueryError`. |
| `tools/mcp_observability.py` | Three `SRETool` subclasses: `MCPMetricTool` (name=`prometheus`), `MCPLogTool` (name=`elasticsearch`), `MCPTraceTool` (name=`jaeger`). Each `_execute()` calls MCP tools, reshapes to legacy format. |
| `tools/mcp_bootstrap.py` | One-shot bootstrap: read `configs/config.yaml`, instantiate shared `MCPClient`, register three MCP tools into `ToolRegistry`. Replaces `tools/alidata_bootstrap.py`. |
| `scripts/start_mcp_server.sh` | Launch `mcp-server-aliyun-observability` with env aliases. |
| `scripts/healthcheck_mcp.sh` | Curl-based health probe used by deploy / smoke. |
| `tests/unit/test_mcp_client.py` | T1 unit tests for `MCPClient` against mock HTTP. |
| `tests/unit/test_mcp_observability.py` | T2 record/replay tests for the three adapters. |
| `tests/fixtures/mcp_replay/` | JSON fixtures recorded from live MCP server. |
| `tests/smoke/smoke_e2e.sh` | T3 end-to-end smoke. |
| `docs/superpowers/specs/2026-06-06-agenticsre-mcp-backend-design.md` | (already written) source spec for this plan. |

### Modified files

| Path | Change |
|---|---|
| `configs/config.yaml` | Replace `observability:` block; remove `prometheus_url / elasticsearch_url / jaeger_url / grafana_url / offline_*`. |
| `tools/__init__.py` | Stop importing `observability.py` and `alidata_observability.py`; route to `mcp_bootstrap`. |
| `main.py` | Call `mcp_bootstrap.init()` instead of `alidata_bootstrap.init()`. |
| `web_app/app.py` | Same redirect (the one place that imports `alidata_*`). |
| `requirements.txt` | Add `mcp-server-aliyun-observability==1.0.8`. |
| `tools/k8s_tools.py` | Audit per R5; delete or stub kubectl-data-read calls (decide per command in Task 14). |

### Deleted files (Task 14)

`tools/observability.py`, `tools/alidata_observability.py`, `tools/alidata_sdk/` (entire dir), `tools/alidata_bootstrap.py`, `data/problem_*` directories.

---

## Phase P0 · Project Bootstrap

### Task 1: Mirror source project into AgenticSRE_MCP, preserve spec

**Files:**
- Modify (on host 8.152.156.185): `/root/cpf/AgenticSRE_MCP/` (currently contains only `docs/superpowers/specs/2026-06-06-agenticsre-mcp-backend-design.md` and this plan file)

- [ ] **Step 1: Verify current state of target dir**

Run: `ssh root@8.152.156.185 'ls -la /root/cpf/AgenticSRE_MCP/ && ls -la /root/cpf/AgenticSRE_MCP/docs/superpowers/specs/'`
Expected: `docs/` subtree present, no other content.

- [ ] **Step 2: rsync source into target, preserving existing docs**

Run:
```bash
ssh root@8.152.156.185 'rsync -a --exclude="__pycache__" --exclude="*.pyc" \
  --exclude="agenticsre-image.tar.gz" --exclude="agenticsre-source.tar.gz" \
  --exclude="data/problem_*" --exclude="测试/" --exclude=".env" \
  /root/cpf/AgenticSRE_ALiData/ /root/cpf/AgenticSRE_MCP/'
```
Expected: exit 0. `docs/superpowers/specs/` survives untouched (rsync only adds/updates; source has no `docs/superpowers/` so spec stays).

- [ ] **Step 3: Copy `.env` separately and verify AK keys present**

Run:
```bash
ssh root@8.152.156.185 'cp /root/cpf/AgenticSRE_ALiData/.env /root/cpf/AgenticSRE_MCP/.env && \
  grep -E "^ALIBABA_CLOUD_ACCESS_KEY_(ID|SECRET)|^REGION|^WORKSPACE_NAME" /root/cpf/AgenticSRE_MCP/.env | sed -E "s/(=)([A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/\1\2****/"'
```
Expected: 4 lines, all keys present, values masked.

- [ ] **Step 4: Initialize git for commit tracking during this plan**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git init -q && \
  echo -e ".env\n__pycache__/\n*.pyc\nbin/\nlogs/\ntests/fixtures/mcp_replay/__cache__/\n" > .gitignore && \
  git add -A && git -c user.email="dev@local" -c user.name="dev" commit -qm "chore: initial mirror from AgenticSRE_ALiData"'
```
Expected: commit succeeds. `git log --oneline -1` shows one commit.

- [ ] **Step 5: Verify tree shape**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && ls && wc -l docs/superpowers/specs/*.md'`
Expected: top-level matches source (`agents/`, `tools/`, `configs/`, `web_app/`, ...) AND spec file exists with >300 lines.

---

### Task 2: Verify Python env and add MCP server dependency

**Files:**
- Modify: `/root/cpf/AgenticSRE_MCP/requirements.txt`

- [ ] **Step 1: Append MCP server package to requirements**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && \
  printf "\n# ─── Aliyun Observability MCP Server ───\nmcp-server-aliyun-observability==1.0.8\n" >> requirements.txt && \
  tail -5 requirements.txt'
```
Expected: last 5 lines end with `mcp-server-aliyun-observability==1.0.8`.

- [ ] **Step 2: Install dependencies (resolves both mcp client SDK and server)**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && pip install --index-url https://pypi.org/simple/ -r requirements.txt 2>&1 | tail -5'`
Expected: `Successfully installed ...` including `mcp-server-aliyun-observability-1.0.8` and `mcp-X.Y.Z` (Y>=12).

- [ ] **Step 3: Verify MCP server CLI is on PATH**

Run: `ssh root@8.152.156.185 'which mcp-server-aliyun-observability && mcp-server-aliyun-observability --help 2>&1 | head -20'`
Expected: path resolves; help output mentions `start` command and `--transport` (or similar) flag.

- [ ] **Step 4: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add requirements.txt && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "deps: add mcp-server-aliyun-observability==1.0.8"'
```

---

## Phase P1 · MCP Server Deployment

### Task 3: Write MCP server launch script

**Files:**
- Create: `/root/cpf/AgenticSRE_MCP/scripts/start_mcp_server.sh`

- [ ] **Step 1: Create scripts directory if absent**

Run: `ssh root@8.152.156.185 'mkdir -p /root/cpf/AgenticSRE_MCP/scripts'`

- [ ] **Step 2: Write the launch script**

Create `/root/cpf/AgenticSRE_MCP/scripts/start_mcp_server.sh` with content:

```bash
#!/usr/bin/env bash
# Launch alibabacloud-observability-mcp-server in streamable-http mode.
# Reads .env from project root; injects REGION/WORKSPACE_NAME as
# ALIBABA_CLOUD_REGION/WORKSPACE aliases expected by the server.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ ! -f .env ]]; then
  echo "FATAL: .env not found at $PROJECT_ROOT/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

export ALIBABA_CLOUD_REGION="${REGION:-cn-hongkong}"
export ALIBABA_CLOUD_WORKSPACE="${WORKSPACE_NAME:-}"

if [[ -z "${ALIBABA_CLOUD_ACCESS_KEY_ID:-}" || -z "${ALIBABA_CLOUD_ACCESS_KEY_SECRET:-}" ]]; then
  echo "FATAL: ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET missing in .env" >&2
  exit 1
fi

PORT="${MCP_SERVER_PORT:-7980}"
LOG_DIR="${PROJECT_ROOT}/logs"
mkdir -p "$LOG_DIR"

echo "Starting MCP server on :${PORT} (region=$ALIBABA_CLOUD_REGION workspace=$ALIBABA_CLOUD_WORKSPACE)"
exec mcp-server-aliyun-observability start \
    --transport streamable-http \
    --port "$PORT" \
    >> "$LOG_DIR/mcp_server.log" 2>&1
```

- [ ] **Step 3: Make executable**

Run: `ssh root@8.152.156.185 'chmod +x /root/cpf/AgenticSRE_MCP/scripts/start_mcp_server.sh'`

- [ ] **Step 4: Probe actual CLI flag names (the README and the installed CLI may differ)**

Run: `ssh root@8.152.156.185 'mcp-server-aliyun-observability start --help 2>&1 | head -40'`
Expected: confirms `--transport streamable-http` and `--port` are the correct flag names. **If flags differ**, edit the script accordingly (e.g. some versions use `--streamable-http` boolean + `--port`).

- [ ] **Step 5: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add scripts/start_mcp_server.sh && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "scripts: launch mcp server in streamable-http mode"'
```

---

### Task 4: Launch MCP server and verify health

**Files:**
- Create: `/root/cpf/AgenticSRE_MCP/scripts/healthcheck_mcp.sh`

- [ ] **Step 1: Write healthcheck script**

Create `/root/cpf/AgenticSRE_MCP/scripts/healthcheck_mcp.sh`:

```bash
#!/usr/bin/env bash
# Probe MCP server liveness by initializing an MCP session and listing tools.
# Exits 0 if server returns ≥1 tool, nonzero otherwise.
set -euo pipefail

ENDPOINT="${MCP_ENDPOINT:-http://localhost:7980/mcp}"
TIMEOUT="${MCP_TIMEOUT:-10}"

python3 - <<PY
import asyncio, sys
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def main():
    async with streamablehttp_client("$ENDPOINT") as (read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=$TIMEOUT)
            tools = await session.list_tools()
            n = len(tools.tools)
            print(f"healthy: {n} tools")
            sys.exit(0 if n >= 1 else 1)

asyncio.run(main())
PY
```

- [ ] **Step 2: Make executable and start MCP server in background**

Run:
```bash
ssh root@8.152.156.185 'chmod +x /root/cpf/AgenticSRE_MCP/scripts/healthcheck_mcp.sh && \
  cd /root/cpf/AgenticSRE_MCP && \
  nohup ./scripts/start_mcp_server.sh > /tmp/mcp_server_stdout.log 2>&1 &  \
  sleep 6 && \
  ps -ef | grep -v grep | grep mcp-server-aliyun-observability | head -3'
```
Expected: at least one process line matching `mcp-server-aliyun-observability start`.

- [ ] **Step 3: Probe with healthcheck**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && ./scripts/healthcheck_mcp.sh'`
Expected: `healthy: N tools` where N >= 30 (the server documents 32 tools). Exit 0.

**If failure**, inspect `logs/mcp_server.log` and re-check Task 3 Step 4 (flag names).

- [ ] **Step 4: Capture the actual tool list for use in later fixture work**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
import asyncio
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def main():
    async with streamablehttp_client(\"http://localhost:7980/mcp\") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            for t in tools.tools:
                print(t.name)

asyncio.run(main())
" > tests/fixtures/mcp_tool_list.txt && wc -l tests/fixtures/mcp_tool_list.txt'
```
Expected: ~32 lines. **Verify** `umodel_get_logs`, `umodel_get_golden_metrics`, `umodel_search_traces`, `umodel_get_traces`, `umodel_search_entities`, `umodel_get_events`, `umodel_get_metrics`, `umodel_get_neighbor_entities` are present.

- [ ] **Step 5: Commit healthcheck + tool list**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && mkdir -p tests/fixtures && git add scripts/healthcheck_mcp.sh tests/fixtures/mcp_tool_list.txt && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "scripts: mcp healthcheck + snapshot 32 tool names"'
```

---

## Phase P2 · MCPClient + Exceptions

### Task 5: Define exception hierarchy

**Files:**
- Create: `/root/cpf/AgenticSRE_MCP/tools/mcp_exceptions.py`
- Test: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_exceptions.py`

- [ ] **Step 1: Create tests directory layout**

Run: `ssh root@8.152.156.185 'mkdir -p /root/cpf/AgenticSRE_MCP/tests/unit /root/cpf/AgenticSRE_MCP/tests/fixtures/mcp_replay && touch /root/cpf/AgenticSRE_MCP/tests/__init__.py /root/cpf/AgenticSRE_MCP/tests/unit/__init__.py'`

- [ ] **Step 2: Write the failing test**

Create `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_exceptions.py`:

```python
import pytest
from tools.mcp_exceptions import (
    MCPError, MCPTransportError, MCPProtocolError,
    MCPToolError, EntityNotFound, AuthError, QueryError,
)


def test_hierarchy_roots():
    assert issubclass(MCPTransportError, MCPError)
    assert issubclass(MCPProtocolError, MCPError)
    assert issubclass(MCPToolError, MCPError)


def test_tool_error_subclasses():
    assert issubclass(EntityNotFound, MCPToolError)
    assert issubclass(AuthError, MCPToolError)
    assert issubclass(QueryError, MCPToolError)


def test_tool_error_carries_payload():
    err = MCPToolError(
        message="boom", tool_name="umodel_get_logs",
        code="EntityNotFound", raw={"foo": "bar"},
    )
    assert err.tool_name == "umodel_get_logs"
    assert err.code == "EntityNotFound"
    assert err.raw == {"foo": "bar"}
    assert "boom" in str(err)


def test_tool_error_inherits_message_from_args():
    err = AuthError(
        message="bad ak", tool_name="introduction",
        code="Unauthorized", raw={"http_status": 401},
    )
    assert isinstance(err, MCPToolError)
    assert isinstance(err, MCPError)
    assert err.code == "Unauthorized"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_exceptions.py -v 2>&1 | tail -10'`
Expected: `ModuleNotFoundError: No module named 'tools.mcp_exceptions'`.

- [ ] **Step 4: Implement the module**

Create `/root/cpf/AgenticSRE_MCP/tools/mcp_exceptions.py`:

```python
"""Exception hierarchy for the MCP backend layer.

Transport / Protocol / ToolSemantic errors are surfaced to agents as-is.
The adapter (`tools/mcp_observability.py`) is responsible for mapping
MCP tool error codes to the business exceptions defined here.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional


class MCPError(Exception):
    """Base for all MCP-layer errors."""


class MCPTransportError(MCPError):
    """Connection / read timeout / 5xx response after all retries exhausted."""


class MCPProtocolError(MCPError):
    """JSON-RPC malformed, schema mismatch, or session init failure."""


class MCPToolError(MCPError):
    """A tool invocation returned a semantic error.

    Attributes:
        tool_name: MCP tool that failed (e.g. ``umodel_get_logs``)
        code: server-supplied error code string
        raw: original error payload for debugging
    """

    def __init__(
        self,
        message: str = "",
        *,
        tool_name: str = "",
        code: str = "",
        raw: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.tool_name = tool_name
        self.code = code
        self.raw = raw or {}


class EntityNotFound(MCPToolError):
    """umodel entity does not exist in the workspace/domain."""


class AuthError(MCPToolError):
    """AK/SK invalid, expired, or lacks permission for the tool."""


class QueryError(MCPToolError):
    """SPL/SQL/PromQL syntax error or query semantic violation."""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_exceptions.py -v 2>&1 | tail -10'`
Expected: `4 passed`.

- [ ] **Step 6: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tools/mcp_exceptions.py tests/__init__.py tests/unit/__init__.py tests/unit/test_mcp_exceptions.py && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "feat(mcp): exception hierarchy for transport/protocol/tool errors"'
```

---

### Task 6: Write failing test for MCPClient.call_tool happy path

**Files:**
- Test: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_client.py`

- [ ] **Step 1: Write the test file (initial test only)**

Create `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_client.py`:

```python
"""Unit tests for tools.mcp_client.MCPClient.

Strategy: mock the underlying mcp SDK's ClientSession by patching
tools.mcp_client._open_session so MCPClient sees an in-memory fake.
"""
import json
import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from tools.mcp_client import MCPClient
from tools.mcp_exceptions import (
    MCPTransportError, MCPProtocolError, MCPToolError,
)


class _FakeContent:
    def __init__(self, payload):
        self.type = "text"
        self.text = json.dumps(payload)


class _FakeToolResult:
    def __init__(self, payload, is_error=False):
        self.content = [_FakeContent(payload)]
        self.isError = is_error


class _FakeSession:
    def __init__(self, tool_response):
        self._tool_response = tool_response
        self.calls = []

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if isinstance(self._tool_response, Exception):
            raise self._tool_response
        return self._tool_response

    async def list_tools(self):
        m = MagicMock()
        m.tools = []
        return m


def _install_fake_session(monkeypatch, session):
    async def _open(_endpoint, _timeout):
        return session, AsyncMock()  # session, closer-coroutine

    monkeypatch.setattr("tools.mcp_client._open_session", _open)


def test_call_tool_happy_path(monkeypatch):
    payload = {"hello": "world", "n": 3}
    session = _FakeSession(_FakeToolResult(payload))
    _install_fake_session(monkeypatch, session)

    client = MCPClient(endpoint="http://fake/mcp", timeout=5, retries=0)
    result = client.call_tool("umodel_get_logs", {"entity_id": "x"})

    assert result == payload
    assert session.calls == [("umodel_get_logs", {"entity_id": "x"})]
```

- [ ] **Step 2: Run to verify it fails**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_client.py::test_call_tool_happy_path -v 2>&1 | tail -10'`
Expected: `ModuleNotFoundError: No module named 'tools.mcp_client'`.

---

### Task 7: Implement MCPClient happy path

**Files:**
- Create: `/root/cpf/AgenticSRE_MCP/tools/mcp_client.py`

- [ ] **Step 1: Write minimal client**

Create `/root/cpf/AgenticSRE_MCP/tools/mcp_client.py`:

```python
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
import time
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
        # Try to extract a server-supplied code from the error text so the
        # adapter can promote to EntityNotFound / AuthError / QueryError
        # in _classify_tool_error(). Fall back to a generic code.
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

    # Prefer the first text content; concatenate if multiple
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
        return raw  # plain string


class MCPClient:
    """Transport-only MCP client.

    Args:
        endpoint: streamable-http URL, e.g. http://localhost:7980/mcp
        timeout: per-call timeout (seconds)
        retries: max retry count for transport-level errors (1 original + N retries)
    """

    def __init__(self, endpoint: str, timeout: float = 60.0, retries: int = 3):
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = retries

    def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Synchronously invoke an MCP tool. Returns decoded JSON or text."""
        return asyncio.run(self._call_tool_async(name, args))

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
                # Tool semantic errors: do not retry.
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
                # Protocol-level: bad schema, malformed JSON-RPC, etc.
                raise MCPProtocolError(f"protocol error calling {name}: {e}") from e

            if attempt < self.retries:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s

        raise MCPTransportError(
            f"transport failed after {self.retries + 1} attempts: {last_exc}"
        ) from last_exc

    def list_tools(self) -> list:
        """Return list of available MCP tool names."""
        return asyncio.run(self._list_tools_async())

    async def _list_tools_async(self) -> list:
        session, closer = await _open_session(self.endpoint, self.timeout)
        try:
            res = await session.list_tools()
            return [t.name for t in res.tools]
        finally:
            await closer()

    def close(self) -> None:
        """No-op; sessions are opened per-call."""
        return None
```

- [ ] **Step 2: Run the happy path test**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_client.py::test_call_tool_happy_path -v 2>&1 | tail -10'`
Expected: `1 passed`.

- [ ] **Step 3: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tools/mcp_client.py tests/unit/test_mcp_client.py && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "feat(mcp): MCPClient happy-path call_tool"'
```

---

### Task 8: Add MCPClient retry and error-classification tests

**Files:**
- Modify: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_client.py`

- [ ] **Step 1: Append failing tests**

Append to `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_client.py`:

```python


def test_transport_retry_then_failure(monkeypatch):
    """Connection errors retry N times then raise MCPTransportError."""
    attempts = {"n": 0}

    async def _flaky_open(_endpoint, _timeout):
        attempts["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setattr("tools.mcp_client._open_session", _flaky_open)
    # Patch sleep to skip real backoff
    async def _noop_sleep(_s): return None
    monkeypatch.setattr("tools.mcp_client.asyncio.sleep", _noop_sleep)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=3)
    with pytest.raises(MCPTransportError):
        client.call_tool("any_tool", {})
    assert attempts["n"] == 4   # 1 original + 3 retries


def test_transport_retry_then_success(monkeypatch):
    """First two attempts fail with transport error, third succeeds."""
    state = {"calls": 0}

    real_session = _FakeSession(_FakeToolResult({"ok": 1}))

    async def _open(_endpoint, _timeout):
        state["calls"] += 1
        if state["calls"] < 3:
            raise ConnectionError("transient")
        return real_session, AsyncMock()

    monkeypatch.setattr("tools.mcp_client._open_session", _open)
    async def _noop_sleep(_s): return None
    monkeypatch.setattr("tools.mcp_client.asyncio.sleep", _noop_sleep)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=3)
    result = client.call_tool("umodel_get_logs", {})
    assert result == {"ok": 1}
    assert state["calls"] == 3


def test_tool_semantic_error_does_not_retry(monkeypatch):
    """isError=True is surfaced as MCPToolError on first attempt; no retry."""
    state = {"calls": 0}

    async def _open(_endpoint, _timeout):
        state["calls"] += 1
        return _FakeSession(_FakeToolResult({"err": "boom"}, is_error=True)), AsyncMock()

    monkeypatch.setattr("tools.mcp_client._open_session", _open)
    async def _noop_sleep(_s): return None
    monkeypatch.setattr("tools.mcp_client.asyncio.sleep", _noop_sleep)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=3)
    with pytest.raises(MCPToolError):
        client.call_tool("any_tool", {})
    assert state["calls"] == 1   # NO retries


def test_protocol_error_wraps_unexpected_exception(monkeypatch):
    """Non-transport, non-tool exceptions become MCPProtocolError."""

    class _BadSession(_FakeSession):
        async def call_tool(self, name, arguments):
            raise ValueError("schema mismatch")

    async def _open(_endpoint, _timeout):
        return _BadSession(None), AsyncMock()

    monkeypatch.setattr("tools.mcp_client._open_session", _open)

    client = MCPClient(endpoint="http://fake/mcp", timeout=1, retries=0)
    with pytest.raises(MCPProtocolError):
        client.call_tool("any_tool", {})
```

- [ ] **Step 2: Run all client tests**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_client.py -v 2>&1 | tail -15'`
Expected: `5 passed`. (1 happy + 4 new.)

- [ ] **Step 3: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tests/unit/test_mcp_client.py && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "test(mcp): retry, semantic error, protocol error paths"'
```

---

### Task 9: Live smoke against the real MCP server

**Files:** (none new; one-shot probe)

- [ ] **Step 1: Probe live MCP server using the real MCPClient**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
from tools.mcp_client import MCPClient
c = MCPClient(endpoint=\"http://localhost:7980/mcp\", timeout=15, retries=1)
print(\"tools count:\", len(c.list_tools()))
result = c.call_tool(\"introduction\", {})
print(\"introduction sample:\", str(result)[:200])
"'
```
Expected: `tools count: 32` (or close), `introduction sample:` shows a non-empty string. **No exception.** This confirms `MCPClient` works end-to-end against the real server.

- [ ] **Step 2: If failure, diagnose**

Common issues:
- Server not running → `ssh root@8.152.156.185 'cat logs/mcp_server.log | tail -50'`
- Tool name mismatch → re-check `tests/fixtures/mcp_tool_list.txt` from Task 4 Step 4
- Auth failure → grep server log for "Unauthorized" / "credentials"

Do NOT proceed past this gate until the live smoke prints non-empty data.

---

## Phase P3 · MCPObservability Adapter

### Task 10: Write failing tests for MCPMetricTool (Prometheus shape)

**Files:**
- Test: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`
- Fixture: `/root/cpf/AgenticSRE_MCP/tests/fixtures/mcp_replay/umodel_get_golden_metrics_sample.json`

- [ ] **Step 1: Record a real `umodel_get_golden_metrics` response**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
import json, time
from tools.mcp_client import MCPClient

c = MCPClient(endpoint=\"http://localhost:7980/mcp\", timeout=30, retries=1)

# First, find a real entity_id via search_entities
entities = c.call_tool(\"umodel_search_entities\", {\"domain\": \"apm\", \"limit\": 3})
print(\"entities sample:\", json.dumps(entities, ensure_ascii=False)[:400])

# Pick first entity (manual inspect; for fixture we hardcode after seeing output)
" 2>&1 | tail -20'
```

**Manual inspection step:** read the output and pick one `entity_id` (or service name). If `umodel_search_entities` returns empty for `apm` domain, try `k8s` or omit domain. **Edit Step 2 below with the chosen entity_id.**

- [ ] **Step 2: Record golden metrics for the chosen entity**

Run (replace `<ENTITY_ID>` with one observed in Step 1):
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
import json, time
from tools.mcp_client import MCPClient

now = int(time.time())
c = MCPClient(endpoint=\"http://localhost:7980/mcp\", timeout=30, retries=1)
result = c.call_tool(\"umodel_get_golden_metrics\", {
    \"entity_id\": \"<ENTITY_ID>\",
    \"from\": now - 3600,
    \"to\": now,
})
with open(\"tests/fixtures/mcp_replay/umodel_get_golden_metrics_sample.json\", \"w\") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(\"saved fixture, top-level keys:\", list(result.keys()) if isinstance(result, dict) else type(result).__name__)
"'
```
Expected: fixture file written; top-level keys printed.

- [ ] **Step 3: Write the failing test**

Create `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`:

```python
"""Unit tests for MCP-backed SRETool adapters.

Strategy: replay-mode — mock MCPClient.call_tool to return fixtures
recorded from the live server. Verifies the adapter reshapes responses
into the legacy Prometheus / Elasticsearch / Jaeger format that agents
expect.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from tools.mcp_observability import MCPMetricTool, MCPLogTool, MCPTraceTool
from tools.mcp_exceptions import EntityNotFound, AuthError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mcp_replay"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _mock_client(tool_responses):
    """tool_responses: dict {mcp_tool_name: response_dict}"""
    client = MagicMock()
    def _call(tool_name, args):
        if tool_name not in tool_responses:
            raise KeyError(f"unexpected tool: {tool_name}")
        return tool_responses[tool_name]
    client.call_tool.side_effect = _call
    return client


def test_metric_tool_returns_prometheus_shape():
    fixture = _load("umodel_get_golden_metrics_sample.json")
    client = _mock_client({"umodel_get_golden_metrics": fixture})

    tool = MCPMetricTool(client=client, default_workspace="ws", default_domain="apm")
    result = tool.execute(query="latency", start="100", end="200")

    assert result.success
    assert result.source == "prometheus"
    data = result.data
    # Prometheus query_range envelope
    assert "results" in data and "result_count" in data
    assert isinstance(data["results"], list)
    if data["results"]:
        sample = data["results"][0]
        assert "metric" in sample and "values" in sample
        # __name__ label present
        assert "__name__" in sample["metric"]
```

- [ ] **Step 4: Run to verify it fails**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_observability.py::test_metric_tool_returns_prometheus_shape -v 2>&1 | tail -10'`
Expected: `ModuleNotFoundError: No module named 'tools.mcp_observability'`.

---

### Task 11: Implement MCPMetricTool

**Files:**
- Create: `/root/cpf/AgenticSRE_MCP/tools/mcp_observability.py` (initial: metric tool only)

- [ ] **Step 1: Write initial module with MCPMetricTool**

Create `/root/cpf/AgenticSRE_MCP/tools/mcp_observability.py`:

```python
"""MCP-backed SRETool adapters.

Three tools preserve the names ``prometheus`` / ``elasticsearch`` /
``jaeger`` so registered consumers (16 agents + web_app + eval) see no
change. Each adapter calls one or more ``umodel_*`` MCP tools via a
shared ``MCPClient`` and reshapes the response into the legacy format
the original Prometheus / ES / Jaeger / AliData backends produced.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base_tool import SRETool, ToolResult
from .mcp_client import MCPClient
from .mcp_exceptions import (
    MCPError, MCPTransportError, MCPToolError,
    EntityNotFound, AuthError, QueryError,
)

logger = logging.getLogger(__name__)


# ───────────── Helpers ─────────────

def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _classify_tool_error(e: MCPToolError) -> MCPToolError:
    """Promote generic MCPToolError to specific subclasses based on code/text."""
    code = (e.code or "").lower()
    msg = str(e).lower()
    if "notfound" in code or "not found" in msg or "no such entity" in msg:
        return EntityNotFound(str(e), tool_name=e.tool_name, code=e.code, raw=e.raw)
    if "unauth" in code or "permission" in msg or "401" in msg or "403" in msg:
        return AuthError(str(e), tool_name=e.tool_name, code=e.code, raw=e.raw)
    if "syntax" in msg or "invalid query" in msg:
        return QueryError(str(e), tool_name=e.tool_name, code=e.code, raw=e.raw)
    return e


# ───────────── Metric Tool (Prometheus shape) ─────────────

class MCPMetricTool(SRETool):
    """Fetches metrics via ``umodel_get_golden_metrics`` and
    ``umodel_get_metrics``, returns Prometheus-compatible envelope."""

    name = "prometheus"
    description = "Fetch metrics via MCP umodel_* tools — Prometheus-compatible adapter"

    def __init__(
        self,
        client: MCPClient,
        default_workspace: str = "",
        default_domain: str = "apm",
    ):
        self.client = client
        self.default_workspace = default_workspace
        self.default_domain = default_domain

    def _execute(
        self,
        query: str = "",
        query_type: str = "instant",
        start: str = "",
        end: str = "",
        step: str = "60s",
        natural_language: str = "",
        namespace: str = "",
        entity_id: str = "",
        max_results: Optional[int] = 50,
    ) -> ToolResult:
        effective_query = query or natural_language or ""

        if "ALERTS" in effective_query:
            return ToolResult(success=True, data={
                "query": effective_query, "result_count": 0, "results": [],
            })

        try:
            from_ts = _to_int(start) or 0
            to_ts = _to_int(end) or 0

            args: Dict[str, Any] = {}
            if self.default_workspace:
                args["workspace"] = self.default_workspace
            if self.default_domain:
                args["domain"] = self.default_domain
            if entity_id:
                args["entity_id"] = entity_id
            if from_ts:
                args["from"] = from_ts
            if to_ts:
                args["to"] = to_ts

            raw = self.client.call_tool("umodel_get_golden_metrics", args)
            results = self._to_prom_format(raw, effective_query, namespace, max_results)
            return ToolResult(success=True, data={
                "query": effective_query,
                "result_count": len(results),
                "results": results,
            })

        except MCPToolError as e:
            raise _classify_tool_error(e)
        except MCPError as e:
            return ToolResult(success=False, error=f"MCP error: {e}")

    @staticmethod
    def _to_prom_format(
        raw: Any,
        query: str,
        namespace: str,
        max_results: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Reshape ``umodel_get_golden_metrics`` into Prometheus result format.

        Expected raw shape (best-effort, defensive):
            {"metrics": [
                {"metric_name": ..., "entity": {"service": ..., "pod": ...},
                 "datapoints": [{"timestamp": ts, "value": v}, ...]},
                ...
             ]}
        Falls back to empty list if shape unrecognized.
        """
        if not isinstance(raw, dict):
            return []
        items = raw.get("metrics") or raw.get("data") or []
        if not isinstance(items, list):
            return []

        results: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            metric_name = item.get("metric_name") or item.get("name") or ""
            entity = item.get("entity") or {}
            svc = entity.get("service") or item.get("service") or ""
            pod = entity.get("pod") or item.get("pod") or ""

            if query and query.lower() not in metric_name.lower() \
                    and query.lower() not in svc.lower():
                continue

            datapoints = item.get("datapoints") or item.get("values") or []
            values: List[List[Any]] = []
            for dp in datapoints:
                if isinstance(dp, dict):
                    ts = _to_int(dp.get("timestamp"))
                    val = dp.get("value")
                elif isinstance(dp, (list, tuple)) and len(dp) >= 2:
                    ts = _to_int(dp[0])
                    val = dp[1]
                else:
                    continue
                values.append([ts, str(val)])

            results.append({
                "metric": {
                    "__name__": metric_name,
                    "service": svc,
                    "pod": pod,
                    "namespace": namespace,
                },
                "values": values,
                "value": values[-1] if values else [0, "0"],
            })
            if max_results and len(results) >= max_results:
                break
        return results

    def health_check(self) -> bool:
        try:
            self.client.call_tool("introduction", {})
            return True
        except Exception:
            return False
```

- [ ] **Step 2: Run the metric test**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_observability.py -v 2>&1 | tail -10'`
Expected: `1 passed`.

- [ ] **Step 3: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tools/mcp_observability.py tests/unit/test_mcp_observability.py tests/fixtures/mcp_replay/umodel_get_golden_metrics_sample.json && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "feat(mcp): MCPMetricTool with Prometheus-shape adapter"'
```

---

### Task 12: Add MCPLogTool (ES shape) with record/replay test

**Files:**
- Fixture: `/root/cpf/AgenticSRE_MCP/tests/fixtures/mcp_replay/umodel_get_logs_sample.json`
- Modify: `/root/cpf/AgenticSRE_MCP/tools/mcp_observability.py`
- Modify: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`

- [ ] **Step 1: Record a real `umodel_get_logs` response**

Run (use the same `<ENTITY_ID>` from Task 10):
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
import json, time
from tools.mcp_client import MCPClient

now = int(time.time())
c = MCPClient(endpoint=\"http://localhost:7980/mcp\", timeout=30, retries=1)
result = c.call_tool(\"umodel_get_logs\", {
    \"entity_id\": \"<ENTITY_ID>\",
    \"from\": now - 3600,
    \"to\": now,
})
with open(\"tests/fixtures/mcp_replay/umodel_get_logs_sample.json\", \"w\") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(\"saved; top-level keys:\", list(result.keys()) if isinstance(result, dict) else type(result).__name__)
"'
```

- [ ] **Step 2: Append failing test**

Append to `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`:

```python


def test_log_tool_returns_es_shape():
    fixture = _load("umodel_get_logs_sample.json")
    client = _mock_client({"umodel_get_logs": fixture})

    tool = MCPLogTool(client=client, default_workspace="ws", default_domain="apm")
    result = tool.execute(query="", time_range="1h", size=20)

    assert result.success
    assert result.source == "elasticsearch"
    data = result.data
    assert "total_hits" in data
    assert "returned" in data
    assert isinstance(data["entries"], list)
    if data["entries"]:
        e = data["entries"][0]
        for key in ("timestamp", "level", "message", "service"):
            assert key in e
```

- [ ] **Step 3: Run to verify it fails**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_observability.py::test_log_tool_returns_es_shape -v 2>&1 | tail -10'`
Expected: `ImportError: cannot import name 'MCPLogTool'`.

- [ ] **Step 4: Implement MCPLogTool**

Append to `/root/cpf/AgenticSRE_MCP/tools/mcp_observability.py`:

```python


# ───────────── Log Tool (Elasticsearch shape) ─────────────

_TIME_RANGE_SECONDS = {
    "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200, "24h": 86400,
}


def _parse_range_seconds(s: str) -> int:
    return _TIME_RANGE_SECONDS.get(s, 3600)


class MCPLogTool(SRETool):
    """Fetches logs via ``umodel_get_logs``, returns ES-compatible envelope."""

    name = "elasticsearch"
    description = "Fetch logs via MCP umodel_get_logs — Elasticsearch-compatible adapter"

    def __init__(
        self,
        client: MCPClient,
        default_workspace: str = "",
        default_domain: str = "apm",
    ):
        self.client = client
        self.default_workspace = default_workspace
        self.default_domain = default_domain

    def _execute(
        self,
        query: str = "",
        index: str = "",
        time_range: str = "1h",
        level: str = "",
        size: int = 100,
        namespace: str = "",
        entity_id: str = "",
    ) -> ToolResult:
        try:
            import time as _t
            now = int(_t.time())
            span = _parse_range_seconds(time_range)
            args: Dict[str, Any] = {
                "from": now - span,
                "to": now,
            }
            if self.default_workspace:
                args["workspace"] = self.default_workspace
            if self.default_domain:
                args["domain"] = self.default_domain
            if entity_id:
                args["entity_id"] = entity_id
            if query:
                args["query"] = query

            raw = self.client.call_tool("umodel_get_logs", args)
            entries = self._to_es_entries(raw, query, level, namespace, size)
            return ToolResult(success=True, data={
                "total_hits": len(entries),
                "returned": len(entries),
                "entries": entries,
            })
        except MCPToolError as e:
            raise _classify_tool_error(e)
        except MCPError as e:
            return ToolResult(success=False, error=f"MCP error: {e}")

    @staticmethod
    def _to_es_entries(
        raw: Any, query: str, level: str, namespace: str, size: int,
    ) -> List[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return []
        items = raw.get("logs") or raw.get("data") or raw.get("entries") or []
        if not isinstance(items, list):
            return []

        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            item_ns = item.get("namespace") or ""
            if namespace and item_ns and namespace.lower() != item_ns.lower():
                continue

            raw_level = (item.get("level") or item.get("log_type") or "info").lower()
            if "error" in raw_level:
                norm_level = "error"
            elif "warn" in raw_level:
                norm_level = "warn"
            else:
                norm_level = "info"
            if level and level.lower() != norm_level:
                continue

            message = (
                item.get("message")
                or item.get("content")
                or item.get("text")
                or ""
            )
            if query and query.lower() not in message.lower():
                continue

            out.append({
                "timestamp": item.get("timestamp") or item.get("__time__") or "",
                "level": norm_level,
                "message": str(message)[:500],
                "pod": item.get("pod") or item.get("pod_name") or "",
                "namespace": item_ns,
                "service": item.get("service") or item.get("service_name") or "",
            })
            if len(out) >= size:
                break
        return out

    def health_check(self) -> bool:
        try:
            self.client.call_tool("introduction", {})
            return True
        except Exception:
            return False
```

- [ ] **Step 5: Run all observability tests**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_observability.py -v 2>&1 | tail -12'`
Expected: `2 passed`.

- [ ] **Step 6: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tools/mcp_observability.py tests/unit/test_mcp_observability.py tests/fixtures/mcp_replay/umodel_get_logs_sample.json && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "feat(mcp): MCPLogTool with ES-shape adapter"'
```

---

### Task 13: Add MCPTraceTool (Jaeger shape) with record/replay test

**Files:**
- Fixture: `/root/cpf/AgenticSRE_MCP/tests/fixtures/mcp_replay/umodel_search_traces_sample.json`
- Fixture: `/root/cpf/AgenticSRE_MCP/tests/fixtures/mcp_replay/umodel_get_traces_sample.json`
- Modify: `/root/cpf/AgenticSRE_MCP/tools/mcp_observability.py`
- Modify: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`

- [ ] **Step 1: Record `umodel_search_traces` and `umodel_get_traces`**

Run (replace `<ENTITY_ID>`):
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
import json, time
from tools.mcp_client import MCPClient

now = int(time.time())
c = MCPClient(endpoint=\"http://localhost:7980/mcp\", timeout=30, retries=1)
search = c.call_tool(\"umodel_search_traces\", {
    \"entity_id\": \"<ENTITY_ID>\",
    \"from\": now - 3600,
    \"to\": now,
    \"limit\": 5,
})
with open(\"tests/fixtures/mcp_replay/umodel_search_traces_sample.json\", \"w\") as f:
    json.dump(search, f, ensure_ascii=False, indent=2)

# Pick first trace id from search result
tids = []
if isinstance(search, dict):
    for it in (search.get(\"traces\") or search.get(\"data\") or []):
        tid = it.get(\"trace_id\") if isinstance(it, dict) else None
        if tid: tids.append(tid)
if tids:
    detail = c.call_tool(\"umodel_get_traces\", {\"trace_ids\": [tids[0]]})
    with open(\"tests/fixtures/mcp_replay/umodel_get_traces_sample.json\", \"w\") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    print(\"saved both fixtures; first tid=\", tids[0])
else:
    print(\"WARN: no trace ids returned\")
"'
```

If no traces are returned, **expand the time window** to `now - 86400` and retry. The smoke depends on having real trace data.

- [ ] **Step 2: Append failing test**

Append to `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`:

```python


def test_trace_tool_returns_jaeger_shape():
    search_fx = _load("umodel_search_traces_sample.json")
    detail_fx = _load("umodel_get_traces_sample.json")
    client = _mock_client({
        "umodel_search_traces": search_fx,
        "umodel_get_traces": detail_fx,
    })

    tool = MCPTraceTool(client=client, default_workspace="ws", default_domain="apm")
    result = tool.execute(service="payment", lookback="1h", limit=5)

    assert result.success
    assert result.source == "jaeger"
    data = result.data
    assert "service" in data
    assert "trace_count" in data
    assert isinstance(data["traces"], list)
    if data["traces"]:
        t = data["traces"][0]
        assert "trace_id" in t
        assert "spans" in t or "services" in t
```

- [ ] **Step 3: Run to verify it fails**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_observability.py::test_trace_tool_returns_jaeger_shape -v 2>&1 | tail -10'`
Expected: `ImportError: cannot import name 'MCPTraceTool'`.

- [ ] **Step 4: Implement MCPTraceTool**

Append to `/root/cpf/AgenticSRE_MCP/tools/mcp_observability.py`:

```python


# ───────────── Trace Tool (Jaeger shape) ─────────────

class MCPTraceTool(SRETool):
    """Fetches traces via ``umodel_search_traces`` + ``umodel_get_traces``,
    returns Jaeger-compatible envelope."""

    name = "jaeger"
    description = "Fetch traces via MCP umodel_* tools — Jaeger-compatible adapter"

    def __init__(
        self,
        client: MCPClient,
        default_workspace: str = "",
        default_domain: str = "apm",
    ):
        self.client = client
        self.default_workspace = default_workspace
        self.default_domain = default_domain

    def _execute(
        self,
        service: str = "",
        operation: str = "",
        min_duration: str = "",
        max_duration: str = "",
        limit: int = 20,
        lookback: str = "1h",
        trace_id: str = "",
        entity_id: str = "",
    ) -> ToolResult:
        try:
            import time as _t
            now = int(_t.time())
            span = _parse_range_seconds(lookback)

            # Exact trace lookup
            if trace_id:
                detail = self.client.call_tool(
                    "umodel_get_traces", {"trace_ids": [trace_id]}
                )
                return ToolResult(success=True, data=self._build_trace_envelope(
                    service=service, traces=self._extract_traces(detail),
                ))

            # Search → optionally fetch details
            args: Dict[str, Any] = {
                "from": now - span, "to": now, "limit": limit,
            }
            if self.default_workspace:
                args["workspace"] = self.default_workspace
            if self.default_domain:
                args["domain"] = self.default_domain
            if entity_id:
                args["entity_id"] = entity_id
            if service:
                args["service"] = service
            if operation:
                args["operation"] = operation

            search = self.client.call_tool("umodel_search_traces", args)
            traces = self._extract_traces(search)
            return ToolResult(success=True, data=self._build_trace_envelope(
                service=service, traces=traces,
            ))
        except MCPToolError as e:
            raise _classify_tool_error(e)
        except MCPError as e:
            return ToolResult(success=False, error=f"MCP error: {e}")

    @staticmethod
    def _extract_traces(raw: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return []
        items = raw.get("traces") or raw.get("data") or []
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            tid = it.get("trace_id") or it.get("traceID") or ""
            spans = it.get("spans") or []
            services = it.get("services") or list({
                sp.get("service") for sp in spans if isinstance(sp, dict)
            })
            out.append({
                "trace_id": tid,
                "spans": spans,
                "services": [s for s in services if s],
                "duration_ms": it.get("duration_ms") or it.get("duration") or 0,
            })
        return out

    @staticmethod
    def _build_trace_envelope(service: str, traces: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "service": service,
            "trace_count": len(traces),
            "traces": traces,
        }

    def health_check(self) -> bool:
        try:
            self.client.call_tool("introduction", {})
            return True
        except Exception:
            return False
```

- [ ] **Step 5: Run all observability tests**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_observability.py -v 2>&1 | tail -15'`
Expected: `3 passed`.

- [ ] **Step 6: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tools/mcp_observability.py tests/unit/test_mcp_observability.py tests/fixtures/mcp_replay/umodel_search_traces_sample.json tests/fixtures/mcp_replay/umodel_get_traces_sample.json && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "feat(mcp): MCPTraceTool with Jaeger-shape adapter"'
```

---

### Task 14: Add error-classification tests (EntityNotFound, AuthError)

**Files:**
- Modify: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`

- [ ] **Step 1: Append two error tests**

Append to `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_observability.py`:

```python


def test_metric_tool_promotes_entity_not_found():
    from tools.mcp_exceptions import MCPToolError
    def _raise(_name, _args):
        raise MCPToolError(
            message="entity not found",
            tool_name="umodel_get_golden_metrics",
            code="EntityNotFound", raw={},
        )
    client = MagicMock()
    client.call_tool.side_effect = _raise

    tool = MCPMetricTool(client=client)
    with pytest.raises(EntityNotFound):
        tool._execute(entity_id="missing")


def test_log_tool_promotes_auth_error():
    from tools.mcp_exceptions import MCPToolError
    def _raise(_name, _args):
        raise MCPToolError(
            message="401 unauthorized",
            tool_name="umodel_get_logs",
            code="Unauthorized", raw={},
        )
    client = MagicMock()
    client.call_tool.side_effect = _raise

    tool = MCPLogTool(client=client)
    with pytest.raises(AuthError):
        tool._execute()
```

- [ ] **Step 2: Run and verify all 5 pass**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_observability.py -v 2>&1 | tail -15'`
Expected: `5 passed`.

- [ ] **Step 3: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tests/unit/test_mcp_observability.py && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "test(mcp): error classification (EntityNotFound, AuthError)"'
```

---

### Task 15: Write bootstrap module + integration test

**Files:**
- Create: `/root/cpf/AgenticSRE_MCP/tools/mcp_bootstrap.py`
- Test: `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_bootstrap.py`

- [ ] **Step 1: Write failing test**

Create `/root/cpf/AgenticSRE_MCP/tests/unit/test_mcp_bootstrap.py`:

```python
from unittest.mock import MagicMock, patch
from tools.base_tool import ToolRegistry


def test_bootstrap_registers_three_tools(monkeypatch):
    fake_client = MagicMock()
    fake_client.list_tools.return_value = ["introduction", "umodel_get_logs"]

    with patch("tools.mcp_bootstrap.MCPClient", return_value=fake_client):
        from tools.mcp_bootstrap import init

        registry = ToolRegistry()
        registry.reset()

        config = {
            "observability": {
                "backend": "mcp",
                "mcp_endpoint": "http://localhost:7980/mcp",
                "mcp_timeout_seconds": 60,
                "mcp_transport_retry": 3,
                "default_domain": "apm",
            }
        }
        client = init(config)

        assert client is fake_client
        assert "prometheus" in registry
        assert "elasticsearch" in registry
        assert "jaeger" in registry
        registry.reset()


def test_bootstrap_rejects_non_mcp_backend(monkeypatch):
    from tools.mcp_bootstrap import init

    config = {"observability": {"backend": "alidata"}}
    import pytest
    with pytest.raises(ValueError):
        init(config)
```

- [ ] **Step 2: Run to verify failure**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/test_mcp_bootstrap.py -v 2>&1 | tail -10'`
Expected: `ModuleNotFoundError: No module named 'tools.mcp_bootstrap'`.

- [ ] **Step 3: Implement bootstrap**

Create `/root/cpf/AgenticSRE_MCP/tools/mcp_bootstrap.py`:

```python
"""Bootstrap MCP backend: instantiate MCPClient, register adapters."""
from __future__ import annotations

import os
import logging
from typing import Any, Dict, Optional

from .base_tool import ToolRegistry
from .mcp_client import MCPClient
from .mcp_observability import MCPMetricTool, MCPLogTool, MCPTraceTool

logger = logging.getLogger(__name__)


def init(config: Dict[str, Any]) -> MCPClient:
    """Initialize MCP backend from config. Returns the shared MCPClient.

    Args:
        config: parsed configs/config.yaml as dict.

    Raises:
        ValueError: if observability.backend is not "mcp".
    """
    obs = config.get("observability") or {}
    backend = obs.get("backend", "").lower()
    if backend != "mcp":
        raise ValueError(
            f"mcp_bootstrap.init() requires observability.backend='mcp', got '{backend}'"
        )

    endpoint = obs.get("mcp_endpoint") or "http://localhost:7980/mcp"
    timeout = float(obs.get("mcp_timeout_seconds") or 60)
    retries = int(obs.get("mcp_transport_retry") or 3)
    default_domain = obs.get("default_domain") or "apm"
    default_workspace = os.environ.get("WORKSPACE_NAME", "")

    client = MCPClient(endpoint=endpoint, timeout=timeout, retries=retries)
    logger.info("MCPClient initialized: endpoint=%s timeout=%s retries=%s",
                endpoint, timeout, retries)

    registry = ToolRegistry()
    registry.register(
        MCPMetricTool(client, default_workspace, default_domain),
        category="observability",
    )
    registry.register(
        MCPLogTool(client, default_workspace, default_domain),
        category="observability",
    )
    registry.register(
        MCPTraceTool(client, default_workspace, default_domain),
        category="observability",
    )
    logger.info("Registered MCP tools: prometheus, elasticsearch, jaeger")
    return client
```

- [ ] **Step 4: Run all unit tests**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/ -v 2>&1 | tail -20'`
Expected: total `2 (bootstrap) + 5 (observability) + 5 (client) + 4 (exceptions) = 16 passed`.

- [ ] **Step 5: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tools/mcp_bootstrap.py tests/unit/test_mcp_bootstrap.py && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "feat(mcp): bootstrap module registers 3 adapters"'
```

---

## Phase P4 · Cutover

### Task 16: Replace observability config block

**Files:**
- Modify: `/root/cpf/AgenticSRE_MCP/configs/config.yaml`

- [ ] **Step 1: Inspect current observability block**

Run: `ssh root@8.152.156.185 'grep -n "^observability:" /root/cpf/AgenticSRE_MCP/configs/config.yaml; sed -n "/^observability:/,/^[a-z]/p" /root/cpf/AgenticSRE_MCP/configs/config.yaml | head -20'`
Expected: prints current block, ends at next top-level key.

- [ ] **Step 2: Use Python to replace the YAML block deterministically**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 << "PY"
import re
p = "configs/config.yaml"
text = open(p).read()
new_block = """observability:
  backend: \"mcp\"                              # only supported backend
  mcp_endpoint: \"http://localhost:7980/mcp\"
  mcp_timeout_seconds: 60
  mcp_transport_retry: 3
  default_domain: \"apm\"
"""
text = re.sub(
    r"^observability:.*?(?=^[a-zA-Z][a-zA-Z_]*:)",
    new_block + "\n",
    text,
    count=1,
    flags=re.DOTALL | re.MULTILINE,
)
open(p, "w").write(text)
print("rewritten")
PY
grep -A 6 "^observability:" configs/config.yaml'
```
Expected: prints the new block.

- [ ] **Step 3: Verify yaml is still parseable**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "import yaml; print(yaml.safe_load(open(\"configs/config.yaml\"))[\"observability\"])"'`
Expected: dict with `backend: mcp`, `mcp_endpoint`, etc.

- [ ] **Step 4: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add configs/config.yaml && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "config: switch observability backend to mcp"'
```

---

### Task 17: Wire main.py and web_app/app.py to mcp_bootstrap

**Files:**
- Modify: `/root/cpf/AgenticSRE_MCP/main.py`
- Modify: `/root/cpf/AgenticSRE_MCP/web_app/app.py`

- [ ] **Step 1: Find existing bootstrap call site in main.py**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && grep -n "alidata_bootstrap\|observability\.\|Observability(" main.py | head -10'`
Expected: prints a small number of lines showing the current init call.

- [ ] **Step 2: Replace bootstrap import + call in main.py**

Use Edit-style precision: read the relevant range first (`sed -n '<line>,<line>p' main.py`) then replace. The replacement rule is:
- Any `from tools.alidata_bootstrap import ...` → `from tools.mcp_bootstrap import init as mcp_init`
- Any `alidata_bootstrap.init(...)` call → `mcp_init(config)`
- Any `from tools.observability import ...` → remove
- Any `from tools.alidata_observability import ...` → remove

Execute the rewrite:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 << "PY"
import re
for path in ("main.py", "web_app/app.py"):
    s = open(path).read()
    s = re.sub(r"from tools\.alidata_bootstrap import [^\n]+", "from tools.mcp_bootstrap import init as mcp_init", s)
    s = re.sub(r"from tools\.observability import [^\n]+\n", "", s)
    s = re.sub(r"from tools\.alidata_observability import [^\n]+\n", "", s)
    s = re.sub(r"alidata_bootstrap\.init\(([^)]*)\)", r"mcp_init(\1)", s)
    open(path, "w").write(s)
    print(f"rewrote {path}")
PY'
```

- [ ] **Step 3: Verify no leftover references**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && grep -n "alidata_bootstrap\|tools\.observability\|tools\.alidata_observability" main.py web_app/app.py || echo OK_no_refs'`
Expected: `OK_no_refs`.

- [ ] **Step 4: Verify main.py imports do not crash**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
# Manual import audit without running main; verify symbols resolve
import importlib.util, sys
for mod in (\"tools.mcp_bootstrap\", \"tools.mcp_client\", \"tools.mcp_observability\"):
    spec = importlib.util.find_spec(mod)
    print(mod, \"OK\" if spec else \"MISSING\")
"'
```
Expected: all three `OK`.

- [ ] **Step 5: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add main.py web_app/app.py && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "cutover: route main.py + web_app to mcp_bootstrap"'
```

---

### Task 18: Audit and clean agents/ for stale imports

**Files:**
- Modify (selectively): any file under `/root/cpf/AgenticSRE_MCP/agents/` referencing the old modules.

- [ ] **Step 1: List stale imports across agents/**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && grep -RIn "from tools\.observability\|from tools\.alidata_observability\|alidata_bootstrap" agents/ | head -30'`
Expected: list of lines. Agents normally pull tools via `ToolRegistry.get("prometheus" / "elasticsearch" / "jaeger")`, so direct imports should be rare. **If many lines appear,** continue to Step 2; **if zero,** skip to Step 4.

- [ ] **Step 2: Bulk-remove direct backend imports**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 << "PY"
import os, re
for root, _, files in os.walk("agents"):
    for fn in files:
        if not fn.endswith(".py"): continue
        p = os.path.join(root, fn)
        s = open(p).read()
        orig = s
        s = re.sub(r"^from tools\.observability import [^\n]+\n", "", s, flags=re.MULTILINE)
        s = re.sub(r"^from tools\.alidata_observability import [^\n]+\n", "", s, flags=re.MULTILINE)
        s = re.sub(r"^from tools\.alidata_bootstrap import [^\n]+\n", "", s, flags=re.MULTILINE)
        if s != orig:
            open(p, "w").write(s)
            print("modified", p)
PY'
```

- [ ] **Step 3: Confirm no leftovers**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && grep -RIn "from tools\.observability\|from tools\.alidata_observability\|alidata_bootstrap" agents/ || echo OK'`
Expected: `OK`.

- [ ] **Step 4: Verify all agent modules import cleanly**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
import importlib, pathlib
for p in pathlib.Path(\"agents\").glob(\"*.py\"):
    if p.name == \"__init__.py\": continue
    mod = f\"agents.{p.stem}\"
    try:
        importlib.import_module(mod)
        print(mod, \"OK\")
    except Exception as e:
        print(mod, \"FAIL\", repr(e))
"'
```
Expected: every line ends in `OK`. **Any FAIL** must be triaged before moving on (likely a missing helper that used to live in `tools/observability.py` — promote it to a small `tools/_compat.py` shim, or inline into the agent).

- [ ] **Step 5: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add -A agents/ && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "cutover: scrub stale backend imports from agents/"'
```

---

### Task 19: Audit eval/ and delete legacy backend files

**Files:**
- Modify (selectively): files under `/root/cpf/AgenticSRE_MCP/eval/` referencing the old modules.
- Delete: `/root/cpf/AgenticSRE_MCP/tools/observability.py`, `tools/alidata_observability.py`, `tools/alidata_bootstrap.py`, `tools/alidata_sdk/`

- [ ] **Step 1: Apply same bulk scrub to eval/**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 << "PY"
import os, re
for root, _, files in os.walk("eval"):
    for fn in files:
        if not fn.endswith(".py"): continue
        p = os.path.join(root, fn)
        s = open(p).read()
        orig = s
        s = re.sub(r"^from tools\.observability import [^\n]+\n", "", s, flags=re.MULTILINE)
        s = re.sub(r"^from tools\.alidata_observability import [^\n]+\n", "", s, flags=re.MULTILINE)
        s = re.sub(r"^from tools\.alidata_bootstrap import [^\n]+\n", "", s, flags=re.MULTILINE)
        if s != orig:
            open(p, "w").write(s)
            print("modified", p)
PY'
```

- [ ] **Step 2: Verify no leftover references anywhere**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && grep -RIn "from tools\.observability\|from tools\.alidata_observability\|from tools\.alidata_bootstrap\|tools\.alidata_sdk" tools/ agents/ web_app/ eval/ main.py 2>/dev/null | grep -v "tools/observability.py\|tools/alidata_observability.py\|tools/alidata_bootstrap.py\|tools/alidata_sdk" || echo OK'`
Expected: `OK`.

- [ ] **Step 3: Delete legacy files**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && \
  git rm -q tools/observability.py tools/alidata_observability.py tools/alidata_bootstrap.py && \
  git rm -qr tools/alidata_sdk/ && \
  ls tools/ | head'
```
Expected: `tools/` listing no longer contains those files; `mcp_*` files remain.

- [ ] **Step 4: Delete data/problem_* (offline_mode is dead)**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && \
  if ls data/problem_* >/dev/null 2>&1; then git rm -qr data/problem_*; echo deleted; else echo none_present; fi'
```

- [ ] **Step 5: Re-run unit tests + import audit**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -m pytest tests/unit/ -v 2>&1 | tail -10'`
Expected: still `16 passed`.

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "
import importlib, pathlib
for d in (\"agents\", \"eval\"):
    for p in pathlib.Path(d).glob(\"**/*.py\"):
        if p.name == \"__init__.py\": continue
        mod = str(p.with_suffix(\"\")).replace(\"/\", \".\")
        try:
            importlib.import_module(mod)
        except Exception as e:
            print(mod, \"FAIL\", repr(e)[:120])
print(\"done\")
"'
```
Expected: only `done` printed, no FAIL lines. (Any FAIL must be triaged — most likely an inlined helper to fix.)

- [ ] **Step 6: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add -A && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "cutover: delete legacy backends + offline data, scrub eval/ imports"'
```

---

### Task 20: Audit tools/k8s_tools.py per R5

**Files:**
- Modify (selectively): `/root/cpf/AgenticSRE_MCP/tools/k8s_tools.py`

- [ ] **Step 1: List subprocess/kubectl call sites**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && grep -nE "subprocess\.|kubectl|os\.popen" tools/k8s_tools.py | head -20'`

- [ ] **Step 2: Decide per command**

For each call site list:
- If it reads cluster **state** (logs/metrics/traces) → **remove** (MCP covers it via `umodel_get_events / umodel_get_entities domain=k8s`).
- If it performs **remediation** (apply / delete / scale) → **keep** — that path isn't read-only and isn't an MCP migration target.

Document each decision in a comment in the file at the top:
```python
# MCP migration audit (2026-06-06):
#   <list each remaining or removed call site with one-line rationale>
```

- [ ] **Step 3: Apply removals (manual edit — read line range and replace specific call sites)**

(No automated rewrite here because the call sites need human judgment. Use `sed -n` to inspect each, delete with targeted block deletions, and re-test imports after each removal.)

- [ ] **Step 4: Confirm k8s_tools.py imports cleanly**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && python3 -c "import tools.k8s_tools; print(\"OK\")"'`
Expected: `OK`.

- [ ] **Step 5: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tools/k8s_tools.py && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "cutover: audit k8s_tools.py — remove read-path kubectl, keep remediation"'
```

---

## Phase P5 · End-to-End Smoke

### Task 21: Write smoke shell script

**Files:**
- Create: `/root/cpf/AgenticSRE_MCP/tests/smoke/smoke_e2e.sh`

- [ ] **Step 1: Create smoke dir**

Run: `ssh root@8.152.156.185 'mkdir -p /root/cpf/AgenticSRE_MCP/tests/smoke'`

- [ ] **Step 2: Write the smoke script**

Create `/root/cpf/AgenticSRE_MCP/tests/smoke/smoke_e2e.sh`:

```bash
#!/usr/bin/env bash
# End-to-end smoke for the MCP backend. Assumes MCP server already running.
# Exits 0 on success.
set -euo pipefail
cd "$(dirname "$0")/../.."

echo "=== Smoke 1: registry contains 3 MCP tools ==="
python3 -c "
import yaml
from tools.base_tool import ToolRegistry
from tools.mcp_bootstrap import init

config = yaml.safe_load(open('configs/config.yaml'))
init(config)
reg = ToolRegistry()
for name in ('prometheus', 'elasticsearch', 'jaeger'):
    assert name in reg, f'missing {name}'
print('OK')
"

echo "=== Smoke 2: detection_agent collects evidence for one entity ==="
python3 -c "
import yaml, sys, os
from tools.base_tool import ToolRegistry
from tools.mcp_bootstrap import init

config = yaml.safe_load(open('configs/config.yaml'))
init(config)
reg = ToolRegistry()

# Pick whichever entity has data; iterate a few candidates
metric_tool = reg.get('prometheus')
log_tool = reg.get('elasticsearch')

m = metric_tool.execute(query='', start='', end='')
assert m.success, f'metric failed: {m.error}'

l = log_tool.execute(query='', time_range='1h', size=5)
assert l.success, f'log failed: {l.error}'

print(f'metric: {m.data[\"result_count\"]} results | log: {l.data[\"returned\"]} entries')
"

echo "=== Smoke 3: benchmark_runner 1 case ==="
if [[ -f eval/benchmark_runner.py ]]; then
  timeout 300 python3 eval/benchmark_runner.py --cases 1 2>&1 | tail -10 || {
    echo "WARN: benchmark_runner exit nonzero — inspect output above"
    exit 1
  }
fi

echo "=== ALL SMOKES PASSED ==="
```

- [ ] **Step 3: Make executable**

Run: `ssh root@8.152.156.185 'chmod +x /root/cpf/AgenticSRE_MCP/tests/smoke/smoke_e2e.sh'`

- [ ] **Step 4: Run smoke**

Run: `ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && ./tests/smoke/smoke_e2e.sh 2>&1 | tail -30'`
Expected: ends with `=== ALL SMOKES PASSED ===` and exit 0.

**If Smoke 2 returns empty results,** revisit the entity_id chosen during fixture recording (Tasks 10-13). If Smoke 3 fails because `benchmark_runner.py` doesn't accept `--cases 1`, inspect its actual CLI and adapt the line (this is an upstream eval contract; the smoke just needs to exercise one case end-to-end).

- [ ] **Step 5: Commit**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf/AgenticSRE_MCP && git add tests/smoke/smoke_e2e.sh && \
  git -c user.email="dev@local" -c user.name="dev" commit -qm "test(mcp): end-to-end smoke (registry + agent + 1-case eval)"'
```

---

## Phase P6 · Migration to 100.88.35.70 (after P5 green)

### Task 22: Package AgenticSRE_MCP for transfer

**Files:** (none new)

- [ ] **Step 1: Tar source from 8.152**

Run:
```bash
ssh root@8.152.156.185 'cd /root/cpf && tar czf /tmp/agenticsre_mcp.tgz \
  --exclude="AgenticSRE_MCP/__pycache__" \
  --exclude="AgenticSRE_MCP/**/__pycache__" \
  --exclude="AgenticSRE_MCP/bin" \
  --exclude="AgenticSRE_MCP/logs" \
  --exclude="AgenticSRE_MCP/.env" \
  AgenticSRE_MCP && ls -lh /tmp/agenticsre_mcp.tgz'
```
Expected: tgz file ≤ 50MB.

- [ ] **Step 2: Pull to Mac**

Run (from Mac, where Claude lives):
```bash
scp root@8.152.156.185:/tmp/agenticsre_mcp.tgz /tmp/agenticsre_mcp.tgz && ls -lh /tmp/agenticsre_mcp.tgz
```

- [ ] **Step 3: Push to 100.88.35.70**

Run:
```bash
scp /tmp/agenticsre_mcp.tgz wenxidao.wxd@100.88.35.70:/tmp/agenticsre_mcp.tgz && \
ssh wenxidao.wxd@100.88.35.70 'cd ~ && tar xzf /tmp/agenticsre_mcp.tgz && ls AgenticSRE_MCP/ | head'
```

- [ ] **Step 4: Clean Mac + 8.152 staging**

Run:
```bash
rm /tmp/agenticsre_mcp.tgz && ssh root@8.152.156.185 'rm /tmp/agenticsre_mcp.tgz'
```

---

### Task 23: Configure 100.88.35.70 environment

**Files:**
- Create on 100: `~/AgenticSRE_MCP/.env`

- [ ] **Step 1: Recreate .env on 100**

Run:
```bash
ssh wenxidao.wxd@100.88.35.70 'cat > ~/AgenticSRE_MCP/.env <<EOF
LLM_API_KEY=<paste from 8.152 .env or new value>
ALIBABA_CLOUD_ACCESS_KEY_ID=<from 8.152 .env>
ALIBABA_CLOUD_ACCESS_KEY_SECRET=<from 8.152 .env>
REGION=cn-hongkong
WORKSPACE_NAME=<from 8.152 .env>
EOF
chmod 600 ~/AgenticSRE_MCP/.env'
```

**Manual values:** the operator running this task fills in real values by copy-paste from `ssh root@8.152.156.185 'cat /root/cpf/AgenticSRE_MCP/.env'`.

- [ ] **Step 2: Install dependencies on 100**

Run:
```bash
ssh wenxidao.wxd@100.88.35.70 'cd ~/AgenticSRE_MCP && pip install --user --index-url https://pypi.org/simple/ -r requirements.txt 2>&1 | tail -5'
```
Expected: `Successfully installed ...` including `mcp-server-aliyun-observability-1.0.8`.

- [ ] **Step 3: Confirm out-of-cluster network reachability for ALIBABA_CLOUD endpoints**

Run:
```bash
ssh wenxidao.wxd@100.88.35.70 'curl -s -o /dev/null -w "%{http_code}\n" https://arms.cn-hongkong.aliyuncs.com/ 2>&1; echo "(non-200 here is fine; we only check connectivity)"'
```
Expected: a numeric status (any number means we reached it). **Connection refused / timeout** means 100 cannot reach the public Aliyun endpoint and you must stop and confirm network config (likely needs an HTTP proxy via env vars). The migration cannot succeed without this.

---

### Task 24: Launch MCP server + run full smoke on 100

**Files:** (none new)

- [ ] **Step 1: Start MCP server on 100**

Run:
```bash
ssh wenxidao.wxd@100.88.35.70 'cd ~/AgenticSRE_MCP && nohup ./scripts/start_mcp_server.sh > /tmp/mcp_server_stdout.log 2>&1 & sleep 6 && ./scripts/healthcheck_mcp.sh'
```
Expected: `healthy: N tools` with N ≥ 30. If failure, inspect `~/AgenticSRE_MCP/logs/mcp_server.log` on 100.

- [ ] **Step 2: Run unit tests on 100**

Run: `ssh wenxidao.wxd@100.88.35.70 'cd ~/AgenticSRE_MCP && python3 -m pytest tests/unit/ -v 2>&1 | tail -10'`
Expected: `16 passed`.

- [ ] **Step 3: Run smoke on 100**

Run: `ssh wenxidao.wxd@100.88.35.70 'cd ~/AgenticSRE_MCP && ./tests/smoke/smoke_e2e.sh 2>&1 | tail -30'`
Expected: ends with `=== ALL SMOKES PASSED ===`.

- [ ] **Step 4: Tag the migration milestone in git**

Run:
```bash
ssh wenxidao.wxd@100.88.35.70 'cd ~/AgenticSRE_MCP && \
  git -c user.email="dev@local" -c user.name="dev" tag -a mcp-cutover-100 -m "AgenticSRE_MCP running on 100.88.35.70"'
```

---

## Done When

- ✅ `pytest tests/unit/` reports 16+ tests passing on both 8.152 and 100
- ✅ `./tests/smoke/smoke_e2e.sh` prints `=== ALL SMOKES PASSED ===` on both hosts
- ✅ `git log --oneline` shows a linear history from the initial mirror through `mcp-cutover-100` tag
- ✅ `grep -RI "observability\.py\|alidata_observability\|alidata_sdk\|alidata_bootstrap" tools/ agents/ eval/ web_app/ main.py` returns nothing
- ✅ The MCP server process is the only thing reading from Aliyun observability endpoints
