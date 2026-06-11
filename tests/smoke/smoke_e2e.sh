#!/usr/bin/env bash
# End-to-end smoke for the MCP backend. Assumes MCP server already running.
set -euo pipefail
cd "$(dirname "$0")/../.."

echo "=== Smoke 1: registry contains 3 MCP tools (via build_tool_registry) ==="
python3 -c "
from tools import build_tool_registry
reg = build_tool_registry()
for name in ('prometheus', 'elasticsearch', 'jaeger'):
    assert name in reg, f'missing {name}'
print('OK — 3 MCP tools registered')
"

echo "=== Smoke 2: live MCP query via each tool ==="
python3 -c "
from tools import build_tool_registry
reg = build_tool_registry()

m = reg.get('prometheus').execute(query='', entity_id='5923d7596dae6413e902be416bee5710')
assert m.success, f'metric failed: {m.error}'

l = reg.get('elasticsearch').execute(query='', time_range='1h', size=5)
assert l.success, f'log failed: {l.error}'

t = reg.get('jaeger').execute(lookback='1h', limit=3)
assert t.success, f'trace failed: {t.error}'

print(f'metric: {m.data[\"result_count\"]} results')
print(f'log: {l.data[\"returned\"]} entries')
print(f'trace: {t.data[\"trace_count\"]} traces')
"

echo "=== ALL SMOKES PASSED ==="
