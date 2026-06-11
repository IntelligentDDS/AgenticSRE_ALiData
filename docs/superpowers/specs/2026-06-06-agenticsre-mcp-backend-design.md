# AgenticSRE_MCP — Backend MCP Migration Design

- **Date**: 2026-06-06
- **Author**: 温希道 (with Claude)
- **Status**: Approved for planning
- **Source project**: `/root/cpf/AgenticSRE_ALiData` (8.152.156.185, non-git)
- **Target project**: `/root/cpf/AgenticSRE_MCP` (same host; later migrated to 100.88.35.70)
- **MCP server**: https://github.com/aliyun/alibabacloud-observability-mcp-server

---

## 1. Goals & Non-Goals

### Goals
所有后台数据读取统一走 Alibaba Cloud Observability MCP Server,streamable-http
传输。删除 native (kubectl / Prometheus / Elasticsearch / Jaeger) 路径和
AliData SDK 直调路径,保留 LLM、orchestrator、Web UI、eval harness。

### Non-goals
- 不改 16 个 agent 的业务逻辑、prompt、推理流程
- 不改 Web UI 前端、不改 eval framework 的评分逻辑
- 不实现 `offline_mode`(废弃),不写 mock MCP server
- 不向后兼容旧 backend(干净切换)

### 保留
LLM 客户端、orchestrator、paradigms、memory、web_app、eval harness、
configs(只改 `observability` 段)。

### 删除
- `tools/observability.py` (native Prom/ES/Jaeger)
- `tools/alidata_observability.py` (AliData SDK 直调)
- `tools/alidata_sdk/` 整目录
- `tools/k8s_tools.py` 里的 kubectl 数据读取部分(实施期对每条命令评估后再决定删除或保留,见 R5)
- `configs/config.yaml` 里 `prometheus_url / elasticsearch_url / jaeger_url /
  grafana_url / offline_mode / offline_data_dir / offline_problem_id /
  offline_data_type`
- `data/problem_*` 离线数据(offline_mode 废弃)

---

## 2. Architecture (Hybrid layered, option C)

```
┌──────────────────────────────────────────────────────────────────┐
│ 16 Agents (detection / log / metric / trace / hypothesis / ...) │
│                Web UI (web_app/app.py)                          │
│              Eval Harness (eval/benchmark_runner.py)            │
└─────────────────────────┬────────────────────────────────────────┘
                          │ 调用稳定的 Python 接口
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  tools/mcp_observability.py                                      │
│    class MCPObservability:                                       │
│      get_logs / get_metrics / get_traces / search_traces /      │
│      get_entities / get_neighbor_entities / get_events /        │
│      get_golden_metrics / list_domains / list_workspace          │
│      ──── escape hatches (MCP-only) ────                         │
│      raw_tool_call / sls_execute_spl / cms_execute_promql /     │
│      cms_natural_language_query                                  │
└─────────────────────────┬────────────────────────────────────────┘
                          │ 调用 tool by name
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  tools/mcp_client.py                                             │
│    class MCPClient:                                              │
│      connect / list_tools / call_tool / close                    │
│    streamable-http transport, JSON-RPC, transport-only retry     │
│    (3x exp backoff on conn-err/timeout, NOT on tool err)         │
└─────────────────────────┬────────────────────────────────────────┘
                          │ HTTP
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  alibabacloud-observability-mcp-server                          │
│  .env: ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET                       │
│  inject: ALIBABA_CLOUD_REGION=$REGION                           │
│          ALIBABA_CLOUD_WORKSPACE=$WORKSPACE_NAME                │
│  exposes 32 tools (16 PaaS / 13 IaaS / 3 shared) on :7980       │
└──────────────────────────────────────────────────────────────────┘
```

### 两层职责切分

- **`MCPClient`**(`tools/mcp_client.py`)— 纯传输层。只关心:连
  streamable-http、发 tool call、收响应、transport-only retry。**不知道任何
  业务语义**,可被任何想直接玩 MCP 的代码复用。
- **`MCPObservability`**(`tools/mcp_observability.py`)— 业务接口层。把
  agent 期望的 `get_logs(service, ...)` 翻译成 `client.call_tool(
  "umodel_get_logs", {...})`,处理参数对齐、结果反序列化、字段映射。
  **Agent 不知道有 MCP 这回事**。

### 进程拓扑(8.152 单机)

- MCP Server 单进程,`start --streamable-http --port 7980`,
  systemd / nohup / docker-compose service。
- AgenticSRE_MCP 主进程 + web_app + eval runner 各自实例化一个
  `MCPClient(endpoint="http://localhost:7980/mcp")`。
- MCP Server 重启不影响 AgenticSRE 进程,反之亦然。

---

## 3. Configuration, Startup, Tool Mapping

### Config (`configs/config.yaml`)

```yaml
observability:
  backend: "mcp"                              # 唯一选项
  mcp_endpoint: "http://localhost:7980/mcp"
  mcp_timeout_seconds: 60
  mcp_transport_retry: 3                      # 仅 connection/timeout 重试
  default_domain: "apm"
```

删除字段:`prometheus_url / elasticsearch_url / jaeger_url / grafana_url /
offline_mode / offline_data_dir / offline_problem_id / offline_data_type`。

### `.env` (不变)

```
ALIBABA_CLOUD_ACCESS_KEY_ID=...
ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
REGION=cn-hongkong
WORKSPACE_NAME=rca-...
```

### 启动脚本 `scripts/start_mcp_server.sh`

```bash
#!/bin/bash
set -e
source .env
export ALIBABA_CLOUD_REGION="$REGION"           # alias 注入
export ALIBABA_CLOUD_WORKSPACE="$WORKSPACE_NAME"
exec ./bin/alibabacloud-observability-mcp-server start \
    --streamable-http --port 7980
```

MCP server 二进制从 release 下载或 `make build`,放 `./bin/`。

### 工具映射(MCP tool → adapter method)

| Adapter method | MCP tool | 备注 |
|---|---|---|
| `get_logs(service, time_range, query)` | `umodel_get_logs` | service → entity_id |
| `get_golden_metrics(entity, time_range)` | `umodel_get_golden_metrics` | RED metrics |
| `get_metrics(entity, metric_name, time_range)` | `umodel_get_metrics` | 自定义指标 |
| `get_traces(service, time_range)` | `umodel_search_traces` + `umodel_get_traces` | 两步 |
| `get_events(entity, time_range)` | `umodel_get_events` | 事件流 |
| `get_entities(domain, filter)` | `umodel_search_entities` | 实体发现 |
| `get_neighbor_entities(entity_id)` | `umodel_get_neighbor_entities` | 拓扑游走 |
| `get_entity_set(name)` | `umodel_get_entity_set` | 实体集合 |
| `list_domains()` | `list_domains` | shared |
| `list_workspace()` | `list_workspace` | shared |
| `sls_execute_spl(query)` | `sls_execute_spl` | escape hatch |
| `cms_execute_promql(query)` | `cms_execute_promql` | escape hatch |
| `cms_natural_language_query(text)` | `cms_natural_language_query` | escape hatch |
| `raw_tool_call(name, args)` | 任意 32 个 tool | 万能逃生通道 |

剩余未直接映射的 MCP tools(`umodel_get_profiles / umodel_list_data_set /
umodel_search_entity_set / umodel_list_related_entity_set /
umodel_get_relation_metrics / sls_list_projects / sls_list_logstores /
sls_text_to_sql / sls_text_to_spl / sls_get_context_logs / sls_log_explore /
sls_log_compare / sls_sop / introduction`)通过 `raw_tool_call` 即时访问。
某个 agent 用得多了再上升为一等方法。

### Agent 改动面

- 16 agent 里 `import` 行从 `from tools.observability import ...` /
  `from tools.alidata_observability import ...` 改为 `from
  tools.mcp_observability import MCPObservability`
- 实例化从 `Observability(config)` / `AliDataObservability(config)` 改为
  `MCPObservability(config)`
- 方法签名对齐:adapter 接口尽量复刻旧两个 backend 的并集,避免 agent
  内部逻辑改动。不可兼容字段记入风险段,实施期对照 fixture 解决。

---

## 4. Error Handling & Data Flow

### Error 分类(fail-fast)

| 层级 | 触发 | `MCPClient` | `MCPObservability` | Agent |
|---|---|---|---|---|
| **Transport** | TCP refused / timeout / 5xx | 1 次原始尝试 + 最多 3 次重试(共 4 次),退避 1s/2s/4s | 不感知 | 4 次后 `MCPTransportError` |
| **Protocol** | JSON-RPC malformed / schema mismatch | 不重试 | 不重试 | `MCPProtocolError` |
| **Tool semantic** | tool 返回 `error`、AK 失效、entity 不存在、SPL 语法错 | 不重试,透传 | 翻译成 `EntityNotFound / AuthError / QueryError` | 业务异常 |
| **Empty result** | tool 成功 + 0 行 | 不重试 | 返回空 list/None | 空结果(合法状态) |

**铁律**:透传 = adapter / client **不吞错误、不写 fallback、不返回 mock
data**。Agent 拿到 exception 自己决定下一步。

### 异常类(`tools/mcp_exceptions.py`)

```python
class MCPError(Exception): ...
class MCPTransportError(MCPError): ...
class MCPProtocolError(MCPError): ...
class MCPToolError(MCPError):
    tool_name: str
    code: str
    raw: dict
class EntityNotFound(MCPToolError): ...
class AuthError(MCPToolError): ...
class QueryError(MCPToolError): ...
```

### 典型数据流 — `detection_agent` 查 service 日志

```
detection_agent.collect_evidence("payment-svc", t0, t1)
  ↓
adapter.get_logs(service="payment-svc", time_range=(t0,t1), query="ERROR")
  ↓ service → entity_id (用 umodel_search_entities,缓存)
  ↓
client.call_tool("umodel_get_logs", {
      "workspace": "rca-...",
      "domain": "apm",
      "entity_id": "apm.service@payment-svc",
      "from": t0, "to": t1,
      "query": "ERROR"
  }, timeout=60)
  ↓ HTTP POST http://localhost:7980/mcp (JSON-RPC 2.0 tools/call)
  ↓ MCP Server: SLS log query → logs[]
  ↓
client 解析 JSON-RPC → raw dict
  ↓
adapter 反序列化为 List[LogEntry(timestamp, level, message, trace_id, ...)]
  ↓
detection_agent 收到 List[LogEntry] (字段对齐旧 backend)
```

### 复合调用 — `get_traces` 两步

```
adapter.get_traces(service, time_range)
  ├─ call_tool("umodel_search_traces", {entity, from, to}) → [trace_id, ...]
  └─ for tid in trace_ids:
       call_tool("umodel_get_traces", {trace_ids: [tid]}) → spans
     合并 → List[Trace]
```

任一步骤失败 → 抛业务异常,不继续。

### 启动顺序

1. `.env` 加载 → 注入 `ALIBABA_CLOUD_REGION/WORKSPACE` alias
2. `./scripts/start_mcp_server.sh` (nohup/systemd) → MCP server :7980
3. Healthcheck:调 `introduction` tool,非空即活
4. AgenticSRE 主进程 → `MCPClient` → `MCPObservability`
5. Adapter 启动时调一次 `list_workspace` + `list_domains` 验证连通 +
   权限
6. 失败 → AgenticSRE 拒绝启动,日志区分 transport / auth / workspace
   三种诊断

### 关闭顺序

AgenticSRE 退出 → `MCPClient.close()` (关闭 HTTP keep-alive)。MCP server
独立生命周期。

---

## 5. Testing, Risks, Phased Delivery

### Testing(确认方案 2:单测 + 端到端 smoke)

- **T1 `MCPClient` 单测** (`tests/test_mcp_client.py`)
  Mock HTTP server,验证 JSON-RPC 请求格式、tools/call payload、timeout、
  transport retry 4 次后 `MCPTransportError`。验证 tool semantic error 不
  重试,直接透传 raw dict。

- **T2 `MCPObservability` record/replay 单测**
  (`tests/test_mcp_observability.py`)
  对真实 MCP server 每个方法 record 一份响应到
  `tests/fixtures/mcp_replay/*.json`。Replay 模式 mock `MCPClient.call_tool`
  ,验证 adapter 反序列化 → 业务对象字段正确。12 方法 × 1 fixture =
  12 replay 测试 + 异常路径 4 (EntityNotFound / AuthError / QueryError /
  empty)。

- **T3 端到端 smoke** (`tests/smoke_e2e.sh`)
  真启 MCP server + AgenticSRE,跑 1 个 detection agent 完整 case
  (固定 entity + 5min 窗口),断言 final report evidence 段非空。再跑
  `eval/benchmark_runner.py --cases 1 --backend mcp`,断言退出码 0、score
  JSON 字段齐。

- **Stretch(可选)**:同 case 在旧 `AgenticSRE_ALiData` 和新
  `AgenticSRE_MCP` 各跑一次,diff detected entity + fault type。
  **不阻塞交付**。

### 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | MCP tool 参数 / 返回字段与旧 AliData SDK 不一致 → 翻译信息丢失 | Section 3 的 12 映射逐个 fixture record;字段差异在 design 期识别 |
| R2 | HTTP 往返延迟比 SDK 直调高 50-200ms | 单 case 可接受;eval N×M 慢则加 `MCPClient` 端 LRU 缓存 (tool+args hash) |
| R3 | 16 agent 实际用到的字段集 > 当前 adapter 接口 → 跑起来发现缺方法 | T3 smoke 用最广路径 detection_agent 覆盖;扩展走 `raw_tool_call` |
| R4 | MCP server 二进制依赖 Go 1.23+ 或 release;8.152 没装 | 先 release 下载;无对应 arch 用 `make build`(需确认 Go 工具链或 Docker) |
| R5 | 现有 agent 直接 `subprocess` kubectl(容器视角)— MCP 不覆盖 | `umodel_get_entities(domain="k8s")` + `umodel_get_events` 覆盖大部分;余下实施期逐条评估,要么删要么记 gap |
| R6 | web_app 前端 JS API 字段如果变 → 前端要改 | adapter 接口保持旧字段命名,后端无感,前端不动 |
| R7 | `.env` 真实 AK 仍在 8.152 | 新项目共用同一 .env(symlink 或单独 cp),两项目共用一组 AK 无冲突 |

### 已知 gap(设计期决定不解 / 延后)

- `paradigms/react.py` 非 observability 工具调用(LLM-direct / 文件 IO)
  不动
- `tools/anomaly_detection.py` / `tools/hero_analysis.py` 纯算法保留;只要
  数据**来源**走 MCP
- `vllm_fault_injector/` 与读路径无关,不动
- `data/problem_*` 删,offline_mode 废弃

### Phased delivery

| Phase | 内容 | 验证门 |
|---|---|---|
| **P0 复制** | `rsync -a /root/cpf/AgenticSRE_ALiData/ /root/cpf/AgenticSRE_MCP/`(保留先写入的 docs/);清理 `.pyc / __pycache__`、tarball、`data/problem_*`、`测试/` | `ls AgenticSRE_MCP` 干净 |
| **P1 MCP server 部署** | 下载/编译二进制到 `./bin/`,写启动脚本,注入 region/workspace alias,`introduction` tool 自检 | curl 7980 healthy,`introduction` 返回非空 |
| **P2 `MCPClient` 层** | 实现 + 单测 T1 | `pytest tests/test_mcp_client.py` 全绿 |
| **P3 `MCPObservability` 层** | 实现 12 方法 + 4 escape hatches + 异常类;record fixtures;单测 T2 | `pytest tests/test_mcp_observability.py` 全绿 |
| **P4 切流** | 删 `tools/observability.py / alidata_observability.py / alidata_sdk/`、旧 config 段;16 agent 改 import;web_app / eval 改 import | `python -c "import agents.detection_agent"` 等无 import 错 |
| **P5 Smoke** | T3 端到端 — detection agent 单 case + benchmark_runner 1 case | 退出码 0,report/score JSON 字段齐 |
| **P6 迁移到 100.88.35.70**(跑通后) | 8.152 → Mac → 100 三段式 tar;100 上重配 .env + 启 MCP server;跑同 smoke | 100 上 smoke 全绿 |

---

## Appendix · 关键事实速查

- 源项目:`/root/cpf/AgenticSRE_ALiData/` 在 8.152.156.185,非 git 仓库。
- 新项目:`/root/cpf/AgenticSRE_MCP/` 同主机。
- 现 `.env` 已含 `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET / REGION /
  WORKSPACE_NAME`。
- 现 config 已有 `observability.backend: alidata | native` 切换、`mcp.
  external_servers` 字段(为本设计预留)。
- 16 agent 全在 `agents/`,数据读取经 `tools/observability.py` /
  `tools/alidata_observability.py`。
- web_app + eval 也走同两个 backend。
- 8.152 与 100.88.35.70 互不可 ssh(已实测 timeout),迁移走 Mac 中转。
