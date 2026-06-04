# AgenticSRE 技术手册

## 目录

1. [系统概述](#1-系统概述)
2. [系统架构](#2-系统架构)
3. [环境要求](#3-环境要求)
4. [部署指南](#4-部署指南)
5. [配置说明](#5-配置说明)
6. [使用指南](#6-使用指南)
7. [模块详解](#7-模块详解)
8. [多智能体协作范式](#8-多智能体协作范式)
9. [智能体泛化与持续演化](#9-智能体泛化与持续演化)
10. [Web Dashboard](#10-web-dashboard)
11. [MCP Server 集成](#11-mcp-server-集成)
12. [评估与测试](#12-评估与测试)
13. [故障排查](#13-故障排查)
14. [API 参考](#14-api-参考)

---

## 1. 系统概述

AgenticSRE 是一个面向 Kubernetes 微服务环境的多智能体 SRE（站点可靠性工程）协作平台，提供假设驱动的自动化根因分析（RCA）与智能运维能力。

### 1.1 核心能力

| 能力 | 说明 |
|------|------|
| **多信号证据采集** | 指标（Prometheus）、日志（Elasticsearch）、链路追踪（Jaeger）、K8s 事件四路并行 |
| **假设驱动推理** | 生成候选根因假设 → 证据验证 → 迭代重排序 → 收敛 |
| **跨信号关联分析** | Hero 风格的多信号关联矩阵，复合评分定位故障服务 |
| **图定位** | 基于服务拓扑的图算法根因定位 |
| **6 种协作范式** | Chain / ReAct / Reflection / Plan-and-Execute / Debate / Voting |
| **持续学习** | WeRCA 风格的规则自动提炼与入库，向量化相似故障检索 |
| **质量评判** | RCA Judge 对推理过程打分，低于阈值标记人工审核 |
| **自动修复** | 生成修复计划 → 执行 kubectl 命令 → 验证 → 失败回滚 |
| **7x24 守护** | 持续检测 → 去重 → 并发 Pipeline → 状态追踪 |
| **领域适配** | 可切换 Kubernetes / 通用 Linux 等领域配置，跨系统泛化 |
| **持续演化** | 专家反馈激活监督学习，演化追踪系统改进趋势 |
| **告警压缩** | 语义化告警聚合，根因推荐准确率 ≥80% |

### 1.2 五阶段 Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│ Phase 1  DETECTION      — 轮询 Prometheus / K8s 事件 / ES 告警    │
│ Phase 2  HYPOTHESIS     — 假设生成 + 历史上下文富化                │
│ Phase 3  INVESTIGATION  — 多智能体证据采集 + 假设重排序             │
│ Phase 4  REASONING      — 关联分析 + 图RCA + 最终报告 + 质量评估    │
│ Phase 5  RECOVERY       — 有条件自愈：修复 → 验证 → 回滚           │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 系统架构

```
                    ┌──────────────────────────────────────┐
                    │         AgenticSRE Web Dashboard      │
                    │     (FastAPI + SSE Real-time Push)     │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────┐
                    │         Orchestrator Layer             │
                    │  ┌─────────┐ ┌──────────┐ ┌────────┐ │
                    │  │ Pipeline│ │  Daemon   │ │  RCA   │ │
                    │  │ Manager │ │ (7x24)    │ │ Engine │ │
                    │  └─────────┘ └──────────┘ └────────┘ │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────┐
                    │      Paradigm Comparison Layer        │
                    │  Chain │ ReAct │ Reflection │ P&E    │
                    │  Debate │ Voting │ Registry           │
                    └──────────────┬───────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
    ┌────▼─────┐            ┌─────▼──────┐           ┌─────▼──────┐
    │ Detection │            │  Planning   │           │  Recovery   │
    │  Agents   │            │  & Reason   │           │   Agent     │
    │• Alert    │            │• Hypothesis │           │• Remediation│
    │• Metric   │            │• Correlation│           │• ActionStack│
    │• Log      │            │• RCA Judge  │           │• Rollback   │
    │• Event    │            │• Planning   │           │             │
    │• Trace    │            │             │           │             │
    └────┬─────┘            └─────┬──────┘           └─────┬──────┘
         │                         │                         │
    ┌────▼─────────────────────────▼─────────────────────────▼──────┐
    │                        Tool Layer                              │
    │  K8s Ops │ Prometheus │ Elasticsearch │ Jaeger │ Anomaly Det  │
    │  Hero Analysis │ RCA Localization │ Action Stack               │
    └────┬─────────────────────────┬─────────────────────────┬──────┘
         │                         │                         │
    ┌────▼─────────────────────────▼─────────────────────────▼──────┐
    │              Memory & Evolution Layer                          │
    │  FaultContextStore │ ContextLearner │ RCAJudge │ TraceStore   │
    │  DomainAdapter │ ContextBuilder │ ExpertFeedback │ Evolution  │
    └───────────────────────────────────────────────────────────────┘
```

### 2.1 核心模块

| 模块 | 目录 | 职责 |
|------|------|------|
| **编排层** | `orchestrator/` | RCA 引擎、5 阶段 Pipeline、7x24 守护进程、会话管理 |
| **范式层** | `paradigms/` | 6 种多智能体协作范式的对比框架 |
| **智能体层** | `agents/` | 11 个专用智能体（检测、分析、推理、修复） |
| **工具层** | `tools/` | K8s、Prometheus、ES、Jaeger、异常检测、图 RCA |
| **记忆层** | `memory/` | 故障上下文持久化、持续学习、质量评判、领域适配、演化追踪 |
| **可观测层** | `observability/` | 智能体执行追踪、性能收集、行为验证 |
| **Web UI** | `web_app/` | FastAPI Dashboard、SSE 实时推送 |
| **MCP** | `mcp_server.py` | Model Context Protocol，集成 Claude / Copilot |
| **评估** | `eval/` | 基准测试、多范式对比评估 |

---

## 3. 环境要求

### 3.1 基础依赖

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.9+ | 推荐 3.11 |
| pip | 23.0+ | 包管理 |

### 3.2 可观测性后端（按需）

| 组件 | 用途 |
|------|------|
| Kubernetes 集群 | 目标运维环境 |
| Prometheus | 指标采集 |
| Elasticsearch | 日志存储 |
| Jaeger | 分布式追踪 |

### 3.3 LLM 服务

AgenticSRE 通过 OpenAI 兼容 API 调用 LLM，支持：
- DeepSeek（默认配置）
- OpenAI GPT 系列
- 任何 OpenAI 兼容的 API（通义千问、Moonshot 等）

---

## 4. 部署指南

### 4.1 本地开发部署

```bash
# 1. 克隆项目
cd /path/to/workspace
git clone <repo_url> AgenticSRE
cd AgenticSRE

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 LLM API Key
#   LLM_API_KEY=sk-your-api-key

# 5. 编辑配置文件
# 修改 configs/config.yaml 中的 LLM、K8s、可观测性后端地址

# 6. 验证安装
python main.py health
```

### 4.2 远程集群部署（一键脚本）

项目提供 `deploy.sh` 一键部署脚本，通过 SSH 跳板机将代码上传到 K8s 集群节点：

```bash
# 设置 API Key 环境变量
export LLM_API_KEY="sk-your-api-key"

# 执行部署（自动上传代码、安装依赖、启动 Web Dashboard）
./deploy.sh
```

部署流程：
1. 通过 SSH 跳板机连接目标节点
2. 打包并上传代码（排除 .venv、__pycache__）
3. 使用集群配置 `configs/config_cluster.yaml`
4. 创建 Python 虚拟环境，安装依赖
5. 启动 Web Dashboard
6. 建立 SSH 隧道映射到 localhost:8080

### 4.3 目录结构

```
AgenticSRE/
├── main.py                  # CLI 主入口
├── mcp_server.py            # MCP Server
├── deploy.sh                # 远程部署脚本
├── requirements.txt         # Python 依赖
├── configs/
│   ├── config.yaml          # 主配置
│   ├── config_cluster.yaml  # 集群配置
│   └── domains/             # 领域适配配置
│       ├── kubernetes.yaml
│       └── generic_linux.yaml
├── agents/                  # 11 个智能体
├── tools/                   # 工具层
├── memory/                  # 记忆与演化
│   ├── fault_context_store.py
│   ├── context_learner.py
│   ├── rca_judge.py
│   ├── trace_store.py
│   ├── domain_adapter.py    # 领域适配
│   ├── context_builder.py   # 上下文构建
│   ├── expert_feedback.py   # 专家反馈
│   └── evolution_tracker.py # 演化追踪
├── orchestrator/            # 编排层
├── paradigms/               # 6 种协作范式
├── observability/           # 可观测性
├── web_app/                 # Web Dashboard
├── eval/                    # 评估模块
└── doc/                     # 文档
```

---

## 5. 配置说明

主配置文件：`configs/config.yaml`

### 5.1 LLM 配置

```yaml
llm:
  api_key: "${LLM_API_KEY}"          # 从环境变量读取
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  temperature: 0.1
  max_tokens: 8192
  timeout: 120
```

支持环境变量替换语法 `${VAR}` 和 `${VAR:default}`。

### 5.2 Kubernetes 配置

```yaml
kubernetes:
  namespace: "default"
  kubeconfig: ""              # 空 = in-cluster 或 ~/.kube/config
  use_ssh: false              # true = 通过 SSH 跳板机访问
  use_dry_run: true           # true = 修复命令 dry-run 模式
  forbid_unsafe_commands:
    - "delete namespace"
    - "delete node"
    - "drain --force"
  ssh_jump_host: "user@jump-host"
  ssh_target: "user@k8s-node"
```

### 5.3 可观测性后端

```yaml
observability:
  prometheus_url: "http://localhost:9090"
  elasticsearch_url: "http://localhost:9200"
  jaeger_url: "http://localhost:16686"
  grafana_url: "http://localhost:3000"
```

### 5.4 记忆与学习

```yaml
memory:
  enabled: true
  backend: "chromadb"         # chromadb | json
  db_path: "./data/memory"
  auto_learn: true            # 自动从 RCA 结果提炼规则
  judge_threshold: 0.65       # 低于此分数标记人工审核
  judge_llm_weight: 0.4       # LLM 评判权重
```

### 5.5 Pipeline 配置

```yaml
pipeline:
  max_evidence_iterations: 3
  hypothesis_confidence_threshold: 0.85
  enable_correlation: true
  enable_graph_rca: true
  enable_recovery: false      # 谨慎开启自愈
```

### 5.6 守护进程配置

```yaml
daemon:
  poll_interval_seconds: 30
  dedup_ttl_seconds: 300
  max_concurrent_pipelines: 3
  default_namespace: ""
  status_file: "./data/daemon_status.json"
```

### 5.7 范式配置

```yaml
paradigm:
  default: "plan_and_execute"
  max_react_steps: 8
  max_reflection_rounds: 2
  debate_perspectives:
    - "infrastructure"
    - "application"
    - "holistic"
  voting_temperatures: [0.1, 0.5, 0.8]
```

### 5.8 领域适配配置

```yaml
domain:
  active_profile: "kubernetes"    # kubernetes | generic_linux
  profiles_dir: ""                # 空 = configs/domains/
  auto_detect: false              # 自动检测运行环境
```

### 5.9 演化追踪配置

```yaml
evolution:
  enabled: true
  snapshot_dir: ""                # 空 = ./data/evolution/
  max_snapshots: 1000
  auto_record: true               # 每次范式运行后自动记录快照
```

### 5.10 自愈配置

```yaml
remediation:
  enabled: false
  confidence_threshold: 0.85
  max_rollback_depth: 5
  require_approval: true
```

---

## 6. 使用指南

### 6.1 命令行接口

AgenticSRE 使用子命令模式：

```bash
python main.py <command> [options]
```

#### 单次 RCA 分析

```bash
python main.py rca "pod CrashLoopBackOff in namespace default"
python main.py rca -n monitoring "high latency in prometheus service" -o result.json
```

#### 完整 Pipeline（5 阶段）

```bash
python main.py pipeline "service degradation detected"
python main.py pipeline -n production "multiple pods restarting"
```

#### 单范式运行

```bash
# 查看可用范式
python main.py paradigm list

# 运行指定范式
python main.py paradigm plan_and_execute "CPU stress on pod frontend-xxx"
python main.py paradigm react "OOMKilled pods in namespace default"
python main.py paradigm debate "network timeout between services" -o result.json
```

#### 多范式对比

```bash
# 对比所有范式
python main.py compare

# 对比指定范式
python main.py compare --paradigms "chain,react,plan_and_execute"

# 运行特定任务
python main.py compare --task task_001
```

#### 7x24 守护进程

```bash
# 前台运行
python main.py daemon

# 指定命名空间和轮询间隔
python main.py daemon -n production -i 60

# 后台运行
nohup python main.py daemon > logs/daemon.log 2>&1 &
```

#### 告警扫描

```bash
python main.py alert-scan
python main.py alert-scan -n monitoring -r 30m
```

#### 状态检查

```bash
python main.py status    # 集群和工具状态
python main.py health    # 工具健康检查
```

#### 专家反馈（监督学习）

```bash
python main.py feedback \
  --incident-id rca-001 \
  --diagnosis "OOMKill caused by memory leak in payment service" \
  --comment "Memory limit should be increased to 2Gi"
```

#### 演化报告

```bash
python main.py evolution
```

#### Web Dashboard

```bash
python main.py web                # 默认 8080 端口
python main.py web -p 3000        # 自定义端口
python main.py web --reload       # 开发模式热重载
```

---

## 7. 模块详解

### 7.1 智能体模块 (`agents/`)

| 智能体 | 文件 | 职责 |
|--------|------|------|
| **MetricAgent** | `metric_agent.py` | 查询 Prometheus 指标，检测异常模式 |
| **LogAgent** | `log_agent.py` | 搜索 Elasticsearch 日志，提取错误模式 |
| **TraceAgent** | `trace_agent.py` | 分析 Jaeger 分布式追踪，定位延迟瓶颈 |
| **EventAgent** | `event_agent.py` | 检查 K8s 事件，识别 Warning 信号 |
| **AlertAgent** | `alert_agent.py` | 告警压缩与语义聚合，根因推荐 |
| **HypothesisAgent** | `hypothesis_agent.py` | 生成根因假设，注入历史知识，重排序 |
| **CorrelationAgent** | `correlation_agent.py` | Hero 风格跨信号关联矩阵 |
| **PlanningAgent** | `planning_agent.py` | 生成调查计划 |
| **DetectionAgent** | `detection_agent.py` | 持续异常检测（Daemon 模式） |
| **RemediationAgent** | `remediation_agent.py` | 自愈操作执行与回滚 |
| **ProfilingAgent** | `profiling_agent.py` | 性能 Profiling 分析 |

### 7.2 工具层 (`tools/`)

| 工具 | 文件 | 功能 |
|------|------|------|
| **ToolRegistry** | `base_tool.py` | 工具注册器，统一的 Tool 抽象基类 |
| **K8sTools** | `k8s_tools.py` | kubectl 操作封装（get/describe/logs/exec） |
| **ObservabilityTools** | `observability.py` | Prometheus PromQL、ES 查询、Jaeger 追踪 |
| **AnomalyDetection** | `anomaly_detection.py` | Z-score / 滑动窗口异常检测算法 |
| **HeroAnalysis** | `hero_analysis.py` | Hero 风格多信号关联分析引擎 |
| **RCALocalization** | `rca_localization.py` | 图推理根因定位（PageRank 变种） |
| **ActionStack** | `action_stack.py` | 操作回滚栈（自愈操作的安全网） |
| **LLMClient** | `llm_client.py` | OpenAI 兼容 LLM 客户端（sync + async） |

### 7.3 记忆层 (`memory/`)

| 组件 | 文件 | 功能 |
|------|------|------|
| **FaultContextStore** | `fault_context_store.py` | ChromaDB 向量存储（规则库 + 故障指纹库） |
| **ContextLearner** | `context_learner.py` | 从 RCA 结果自动提炼诊断规则（auto + supervised） |
| **RCAJudge** | `rca_judge.py` | 规则 + LLM 双维度质量评判 |
| **TraceStore** | `trace_store.py` | 智能体执行轨迹存储 |
| **DomainAdapter** | `domain_adapter.py` | 领域配置加载与切换 |
| **ContextBuilder** | `context_builder.py` | 统一上下文组装器 |
| **ExpertFeedbackStore** | `expert_feedback.py` | 专家反馈存储，激活监督学习 |
| **EvolutionTracker** | `evolution_tracker.py` | 系统演化趋势追踪 |

### 7.4 编排层 (`orchestrator/`)

| 组件 | 文件 | 功能 |
|------|------|------|
| **RCA Engine** | `rca_engine.py` | 核心假设驱动 RCA 循环（9 步） |
| **Pipeline** | `pipeline.py` | 5 阶段 Pipeline 管理器 |
| **Daemon** | `daemon.py` | 7x24 守护进程（轮询检测 + 信号去重 + 并发分发） |
| **Session** | `session.py` | RCA 会话状态管理 |

### 7.5 可观测层 (`observability/`)

| 组件 | 文件 | 功能 |
|------|------|------|
| **AgentTracer** | `tracer.py` | 智能体执行追踪（输入/输出/延迟/Token） |
| **BehaviorValidator** | `validator.py` | 行为异常检测（Z-score） |

---

## 8. 多智能体协作范式

AgenticSRE 支持 6 种可对比的多智能体协作范式，所有范式共享同一个 `AgentPool`（相同的工具、LLM、智能体实例），确保对比公平。

### 8.1 Chain（链式）

```
Event → Metric → Log → Trace → LLM 综合报告
```

- 顺序执行，每步的输出作为下一步的上下文
- 确定性最高，延迟最大
- 适合简单、线性的故障场景

### 8.2 ReAct（推理-行动循环）

```
Thought → Action → Observation → Thought → ... → Conclude
```

- LLM 动态决定每步调用哪个智能体
- 最灵活，Token 开销最高
- 适合复杂、需要动态探索的场景

### 8.3 Reflection（自省式）

```
并行证据采集 → 初始分析 → 批评审查 → 补充调查 → 改进报告
```

- 最多 2 轮反思改进
- 适合需要深度分析的场景

### 8.4 Plan-and-Execute（假设驱动）

```
假设生成 → 调查计划 → 并行证据采集 → 假设重排序 → 关联分析 → 最终报告
```

- 与 RCA Engine 的核心逻辑一致
- 支持历史规则注入和迭代收敛
- **默认范式**

### 8.5 Debate（多视角辩论）

```
共享证据 → 基础设施视角 / 应用视角 / 全局视角（并行）→ 裁判综合
```

- 三个独立视角并行分析，避免单一视角偏见
- 适合复杂的跨层故障

### 8.6 Voting（集成投票）

```
共享证据 → 保守分析(T=0.1) / 探索分析(T=0.5) / 创意分析(T=0.8) → 多数投票聚合
```

- 三次独立 LLM 分析（不同温度），投票聚合
- 鲁棒性最高，适合关键决策场景

### 8.7 范式对比

```bash
# 对比所有范式
python main.py compare

# 对比指定范式
python main.py compare --paradigms "chain,plan_and_execute,debate"
```

---

## 9. 智能体泛化与持续演化

### 9.1 领域适配

AgenticSRE 通过 YAML 领域配置文件实现跨系统泛化。每个领域配置包含：
- **agent_context_hints**：每个智能体的领域专用提示词
- **log_error_keywords**：日志错误关键词列表
- **event_patterns**：事件匹配模式
- **thresholds**：领域专用阈值

内置领域配置：

| 领域 | 文件 | 适用场景 |
|------|------|---------|
| Kubernetes | `configs/domains/kubernetes.yaml` | K8s 微服务环境 |
| Generic Linux | `configs/domains/generic_linux.yaml` | 传统 Linux 服务器 |

切换领域：
```yaml
# configs/config.yaml
domain:
  active_profile: "generic_linux"
```

自定义领域配置：在 `configs/domains/` 下创建新 YAML 文件，遵循相同结构即可自动加载。

### 9.2 上下文构建

`ContextBuilder` 从多个来源组装统一的 `AgentContext`：

```
ContextBuilder.build_context(query)
├── FaultContextStore → 历史规则 + 相似故障指纹
├── ExpertFeedbackStore → 近期专家反馈
├── DomainAdapter → 领域专用提示词
└── TraceStore → 近期执行轨迹 + 性能统计
      ↓
AgentContext → enrich_query() → 富化后的 query
      ↓
每个 domain agent 收到针对自身角色的领域提示
```

所有 6 种范式在运行时自动调用 `AgentPool.run_all_domain_agents_enriched()`，为每个领域智能体注入特定的领域上下文。

### 9.3 专家反馈与监督学习

专家反馈机制激活 `ContextLearner.learn_supervised()` 代码路径：

```bash
python main.py feedback \
  --incident-id rca-001 \
  --diagnosis "OOMKill caused by memory leak in payment service"
```

工作流程：
1. 专家提交事件 ID + 正确诊断
2. `ExpertFeedbackStore` 持久化反馈
3. 调用 `ContextLearner.learn_supervised(agent_diagnosis, ground_truth)`
4. LLM 对比智能体诊断与真实根因，提炼正面/负面规则
5. 规则入库到 `FaultContextStore`，后续分析自动召回

### 9.4 演化追踪

`EvolutionTracker` 在每次范式运行后自动记录快照：

```python
snapshot = {
    "rule_count": 42,          # 规则库规模
    "fault_context_count": 15, # 故障指纹数
    "feedback_count": 8,       # 专家反馈数
    "rca_confidence": 0.87,    # 诊断置信度
    "rca_latency_s": 45.2,    # 诊断耗时
    "judge_score": 0.73,      # 质量评分
    "paradigm_name": "plan_and_execute",
}
```

查看演化报告：
```bash
python main.py evolution
```

输出示例：
```
══════════════════════════════════════════════════════════════
  AgenticSRE Evolution Report
══════════════════════════════════════════════════════════════
  Snapshots      : 47
  Time Range     : 2026-01-15 09:30 - 2026-02-26 14:20
  Span           : 1012.8h
──────────────────────────────────────────────────────────────
  Knowledge Base Growth
    Initial rules    : 5
    Current rules    : 42
    Net growth       : +37
──────────────────────────────────────────────────────────────
  Diagnostic Confidence
    Average          : 78.3%
    Latest           : 87.0%
    Trend            : improving
──────────────────────────────────────────────────────────────
  Summary: System has processed 47 incidents over 1012.8h.
  Knowledge base: 42 rules, 15 fault contexts. Confidence trend: improving.
══════════════════════════════════════════════════════════════
```

---

## 10. Web Dashboard

### 10.1 启动

```bash
python main.py web              # http://localhost:8080
python main.py web -p 3000      # 自定义端口
```

### 10.2 功能

| 功能 | 说明 |
|------|------|
| 集群概览 | 实时展示 Node / Pod 状态、告警数量 |
| RCA 触发 | 输入事件描述，一键触发 RCA 分析 |
| 实时日志 | SSE 推送 RCA 执行过程日志 |
| 检测扫描 | 手动触发异常检测 |
| 告警压缩 | 展示告警分组与根因推荐 |
| 守护进程控制 | 启动/停止 7x24 Daemon，查看状态 |
| 历史记录 | 查看历史 Pipeline 执行结果 |

### 10.3 API 端点

Dashboard 的 REST API 可供外部系统集成：

```
GET  /api/cluster/status     # 集群状态
GET  /api/detection/scan     # 检测扫描
POST /api/rca/trigger        # 触发 RCA {"query": "..."}
GET  /api/rca/stream/{id}    # SSE 日志流
GET  /api/daemon/status      # Daemon 状态
POST /api/daemon/start       # 启动 Daemon
POST /api/daemon/stop        # 停止 Daemon
```

---

## 11. MCP Server 集成

AgenticSRE 实现了 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) Server，可集成到 Claude Desktop 或 VS Code Copilot。

### 11.1 启动

```bash
python mcp_server.py                          # stdio 模式
python mcp_server.py --transport sse --port 8765  # SSE 模式
```

### 11.2 Claude Desktop 配置

在 Claude Desktop 的 `settings.json` 中添加：

```json
{
  "mcpServers": {
    "AgenticSRE": {
      "command": "/path/to/AgenticSRE/.venv/bin/python3",
      "args": ["/path/to/AgenticSRE/mcp_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/AgenticSRE",
        "LLM_API_KEY": "sk-xxx"
      }
    }
  }
}
```

### 11.3 暴露的工具

MCP Server 暴露了 AgenticSRE 的所有核心工具供 LLM 调用：
- K8s 操作（get/describe/logs）
- Prometheus 查询
- Elasticsearch 日志搜索
- Jaeger 追踪查询
- 异常检测
- 完整 RCA 工作流
- 告警压缩

---

## 12. 评估与测试

### 12.1 基准测试

```bash
# 运行基准测试（使用 eval/eval_tasks.yaml 定义的任务）
python -m eval.benchmark_runner

# 多范式对比评估
python main.py compare
python main.py compare --paradigms "chain,plan_and_execute" --task task_001
```

### 12.2 评估任务定义

评估任务在 `eval/eval_tasks.yaml` 中定义，每个任务包含：
- `id`: 唯一任务标识
- `category`: 故障类别（resource / network / config / application）
- `description`: 故障描述
- `expected_root_cause`: 预期根因
- `expected_fault_type`: 预期故障类型

### 12.3 验证命令

```bash
# 语法检查
python -c "from memory import *; print('OK')"

# 领域适配验证
python -c "
from memory.domain_adapter import DomainAdapter
da = DomainAdapter()
print(da.list_profiles())
print(da.get_active_profile().domain_name)
"

# 上下文构建验证
python -c "
from memory.context_builder import ContextBuilder
from memory.domain_adapter import DomainAdapter
builder = ContextBuilder(domain_adapter=DomainAdapter())
ctx = builder.build_context('CPU stress on pod')
print(ctx.to_context_string('metric_agent')[:200])
"

# 演化追踪验证
python -c "
from memory.evolution_tracker import EvolutionTracker
tracker = EvolutionTracker(snapshot_dir='/tmp/test_evo')
tracker.record_snapshot(paradigm_name='test')
print(tracker.get_evolution_report())
"
```

---

## 13. 故障排查

### 13.1 常见问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `ImportError: chromadb` | ChromaDB 未安装 | `pip install chromadb>=0.5.0` |
| `LLM timeout` | API 不可达或超时 | 检查 `llm.base_url` 和网络，增大 `llm.timeout` |
| `K8s connection refused` | kubeconfig 配置错误 | 检查 `kubernetes.kubeconfig` 或 SSH 配置 |
| `No detection signals` | 可观测后端未配置 | 检查 `observability.*_url` 配置 |
| `Judge score too low` | 推理质量不足 | 调低 `memory.judge_threshold` 或检查 LLM 输出 |

### 13.2 日志查看

```bash
# 运行时日志
python main.py -v rca "some query"    # -v 开启 DEBUG 级别

# Web Dashboard 日志
tail -f logs/web.log

# Daemon 日志
tail -f logs/daemon.log
```

### 13.3 数据目录

| 路径 | 内容 |
|------|------|
| `./data/memory/` | ChromaDB 向量数据 + JSON 降级存储 |
| `./data/memory/traces/` | 执行轨迹数据 |
| `./data/expert_feedback/` | 专家反馈记录 |
| `./data/evolution/` | 演化快照 |
| `./data/daemon_status.json` | 守护进程状态 |
| `./logs/` | 运行日志 |

### 13.4 重置数据

```bash
# 清除所有学习数据（规则、故障指纹、反馈、演化快照）
rm -rf ./data/

# 仅清除演化快照
rm -rf ./data/evolution/

# 仅清除专家反馈
rm -rf ./data/expert_feedback/
```

---

## 14. API 参考

### 14.1 核心函数

#### `orchestrator.rca_engine.run_rca()`

```python
async def run_rca(
    incident_query: str,
    namespace: str = "",
    config=None,
    log_callback=None,
    registry=None,
) -> Dict
```

执行完整 RCA 流程（9 步），返回包含 `session_id`, `status`, `result`, `judge`, `hypotheses`, `phases` 的字典。

#### `orchestrator.pipeline.Pipeline.run()`

```python
async def run(
    trigger: str,
    namespace: str = "",
    log_callback=None,
) -> PipelineResult
```

执行 5 阶段 Pipeline，返回 `PipelineResult`。

#### `paradigms.base.AgentPool`

```python
pool = AgentPool(config)
pool.build_context(query)                          # 构建统一上下文
await pool.run_all_domain_agents(query, ns)        # 原始并行调用
await pool.run_all_domain_agents_enriched(query, ns, ctx)  # 富化并行调用
```

### 14.2 Memory API

```python
from memory import (
    FaultContextStore, ContextLearner, RCAJudge, TraceStore,
    DomainAdapter, ContextBuilder, ExpertFeedbackStore, EvolutionTracker,
)

# 故障上下文
store = FaultContextStore(config)
store.add_rule({"condition": "...", "conclusion": "..."})
store.query_similar_rules("CPU spike", n=5)
store.get_historical_context("pod crash")

# 持续学习
learner = ContextLearner(llm, store, config)
learner.learn_from_trace(reasoning, root_cause, confidence, judge_level)
learner.learn_supervised(agent_diagnosis, ground_truth)

# 质量评判
judge = RCAJudge(llm, config)
result = judge.judge(reasoning, root_cause, confidence)

# 领域适配
adapter = DomainAdapter.from_config()
profile = adapter.get_active_profile()

# 上下文构建
builder = ContextBuilder(fault_store, feedback_store, domain_adapter, trace_store)
ctx = builder.build_context(query)
enriched = builder.enrich_query(query, ctx, "metric_agent")

# 专家反馈
fb = ExpertFeedbackStore()
fb.submit_feedback("rca-001", "OOMKill diagnosis", context_learner=learner)

# 演化追踪
tracker = EvolutionTracker.from_config()
tracker.record_snapshot(fault_store=store, rca_result=result)
report = tracker.get_evolution_report()
```

### 14.3 CLI 命令速查

| 命令 | 用途 |
|------|------|
| `python main.py rca "query"` | 单次 RCA |
| `python main.py pipeline "query"` | 5 阶段 Pipeline |
| `python main.py paradigm NAME "query"` | 运行单个范式 |
| `python main.py compare` | 多范式对比 |
| `python main.py daemon` | 7x24 守护进程 |
| `python main.py web` | Web Dashboard |
| `python main.py status` | 集群状态 |
| `python main.py health` | 工具健康检查 |
| `python main.py alert-scan` | 告警扫描 |
| `python main.py feedback --incident-id X --diagnosis "..."` | 专家反馈 |
| `python main.py evolution` | 演化报告 |
