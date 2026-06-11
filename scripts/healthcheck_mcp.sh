#!/usr/bin/env bash
set -euo pipefail
ENDPOINT="${MCP_ENDPOINT:-http://127.0.0.1:7980/mcp}"
TIMEOUT="${MCP_TIMEOUT:-15}"
python3 - <<PY
import asyncio, sys
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def probe():
    async with streamablehttp_client("$ENDPOINT") as (read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=$TIMEOUT)
            tools = await session.list_tools()
            return len(tools.tools)

n = asyncio.run(probe())
print(f"healthy: {n} tools")
sys.exit(0 if n >= 1 else 1)
PY
