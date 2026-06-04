# AgenticSRE — 多智能体协作智能运维系统

> 基于"发现-假设-规划-调查-推理"范式的多智能体运维系统

## 项目概述

AgenticSRE 是一个面向通算与智算场景的高效率、可解释、自演化的智能运维系统。
系统通过多智能体协作实现从"被动响应"向"主动诊断与自适应进化"的跨越。

### 核心特性

- 🔍 **主动故障检测**：多维度持续监控（指标/日志/调用链/事件/告警）
- 🧠 **假设驱动RCA**：基于"发现→假设→规划→调查→推理"五阶段范式
- 🤖 **多智能体协作**：专用智能体编排协作，支持链式/反应式/并行模式
- 📊 **告警压缩与根因推荐**：语义化告警聚合，根因推荐准确率≥80%
- 🔄 **持续演化**：WeRCA式记忆学习 + 专家反馈 + 历史轨迹优化
- 👁️ **全链路可观测**：输入/输出/思维链/性能/资源 端到端可观测
- 🛠️ **自动修复**：安全的自愈操作 + ActionStack回滚机制

### 系统架构

```
                    ┌──────────────────────────────────────┐
                    │         AgenticSRE Web Dashboard      │
                    │     (FastAPI + SSE Real-time Push)     │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────┐
                    │         Orchestrator Layer             │
                    │  ┌─────────┐ ┌──────────┐ ┌────────┐│
                    │  │ Pipeline│ │  Daemon   │ │  RCA   ││
                    │  │ Manager │ │ (7×24)    │ │ Engine ││
                    │  └─────────┘ └──────────┘ └────────┘│
                    └──────────────┬───────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
    ┌────▼─────┐            ┌─────▼──────┐           ┌─────▼──────┐
    │ Detection │            │  Planning   │           │  Recovery   │
    │  Agents   │            │  & Reasoning│           │   Agent     │
    │           │            │  Agents     │           │             │
    │• Alert    │            │• Hypothesis │           │• Remediation│
    │• Metric   │            │• Correlation│           │• ActionStack│
    │• Log      │            │• RCA Judge  │           │• Rollback   │
    │• Event    │            │             │           │             │
    └────┬─────┘            └─────┬──────┘           └─────┬──────┘
         │                         │                         │
    ┌────▼─────────────────────────▼─────────────────────────▼──────┐
    │                        Tool Layer                              │
    │  K8s Ops │ Prometheus │ Elasticsearch │ Jaeger │ Anomaly Det  │
    └────┬─────────────────────────┬─────────────────────────┬──────┘
         │                         │                         │
    ┌────▼─────────────────────────▼─────────────────────────▼──────┐
    │                   Memory & Evolution Layer                     │
    │  FaultContextStore │ ContextLearner │ RCAJudge │ TraceStore   │
    └───────────────────────────────────────────────────────────────┘
```

## 五阶段 Pipeline

| 阶段 | 名称 | 描述 |
|------|------|------|
| Phase 1 | **DETECTION** | 持续轮询Prometheus告警、K8s事件、ES错误日志、指标异常 |
| Phase 2 | **HYPOTHESIS** | 生成初始根因假设，注入历史知识 |
| Phase 3 | **INVESTIGATION** | 多智能体并行证据收集 + 交叉信号关联 + 假设重排序 |
| Phase 4 | **REASONING** | 图推理RCA定位 + LLM综合报告 + 质量评估 |
| Phase 5 | **RECOVERY** | 条件触发自愈操作 + 回滚保护 |

## 快速开始

**Docker 一键部署**（推荐，详见 [USER_MANUAL.md](USER_MANUAL.md)）：

```bash
tar xzf agenticsre-release-YYYYMMDD.tar.gz
cd agenticsre-release-YYYYMMDD
cp .env.example .env        # 填 LLM_API_KEY
./deploy_docker.sh
# 浏览器打开 http://<部署机IP>:8080
```

发布包默认加载随包镜像并以 `--no-build` 启动，不会在部署机重新下载 apt/pip 依赖。
只有显式运行 `./deploy_docker.sh --build` 才会在线重建镜像。

**从源码跑**（开发）：

```bash
pip install -r requirements.txt
cp .env.example .env        # 填 LLM_API_KEY
# 配置在 configs/config.yaml & configs/clusters.yaml (字段说明见 configs/README.md)

# Web UI
cd web_app && ./start.sh

# CLI
python main.py --mode daemon     # 7×24 持续监控
python main.py --mode pipeline   # 单次 Pipeline
python main.py --mode rca --query "pod CrashLoopBackOff in namespace default"
```

## 文档地图

| 文档 | 给谁看 |
|---|---|
| [USER_MANUAL.md](USER_MANUAL.md) | **使用者** — 部署、UI 走查、典型工作流、FAQ |
| [configs/README.md](configs/README.md) | **使用者** — 配置字段、LLM provider 切换、多集群 |
| [doc/technical_manual.md](doc/technical_manual.md) | **开发者** — 系统架构、模块、API、二次开发 |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | 发布包内 — 本次变更 |

## 验证环境

3节点K8S集群:
```bash
ssh -J openstack@222.200.180.102 ubuntu@10.10.3.110
```

## 项目结构

```
AgenticSRE/
├── main.py                  # 主入口
├── mcp_server.py            # MCP Server (Claude/Copilot集成)
├── requirements.txt         # Python依赖
├── configs/                 # 配置文件
├── agents/                  # 智能体模块
│   ├── alert_agent.py       # 告警压缩与根因推荐
│   ├── metric_agent.py      # 指标分析
│   ├── log_agent.py         # 日志分析
│   ├── trace_agent.py       # 调用链分析
│   ├── event_agent.py       # K8s事件分析
│   ├── hypothesis_agent.py  # 假设生成与重排序
│   ├── correlation_agent.py # 交叉信号关联
│   ├── detection_agent.py   # 持续异常检测
│   ├── planning_agent.py    # 规划智能体
│   ├── remediation_agent.py # 自愈智能体
│   └── profiling_agent.py   # Profiling分析
├── tools/                   # 工具层
│   ├── base_tool.py         # 工具基类 + 注册器
│   ├── k8s_tools.py         # K8s操作工具
│   ├── k8s_ops.py           # K8s SDK原生操作
│   ├── observability.py     # Prometheus/ES/Jaeger
│   ├── anomaly_detection.py # 异常检测算法
│   ├── hero_analysis.py     # Hero分析引擎
│   ├── rca_localization.py  # 图推理RCA
│   ├── action_stack.py      # 操作回滚栈
│   └── llm_client.py        # LLM客户端
├── memory/                  # 记忆与演化
│   ├── fault_context_store.py  # 故障上下文存储
│   ├── context_learner.py      # 自动规则学习
│   ├── rca_judge.py            # RCA质量评估
│   └── trace_store.py          # 执行轨迹存储
├── orchestrator/            # 编排层
│   ├── rca_engine.py        # 核心RCA引擎
│   ├── pipeline.py          # 五阶段Pipeline
│   ├── daemon.py            # 7×24守护进程
│   └── session.py           # 会话状态管理
├── observability/           # 智能体可观测性
│   ├── tracer.py            # 执行追踪器
│   ├── metrics_collector.py # 性能指标收集
│   └── validator.py         # 行为验证器
├── web_app/                 # Web Dashboard
│   ├── app.py               # FastAPI后端
│   ├── templates/           # Jinja2模板
│   └── static/              # 前端资源
└── eval/                    # 评估模块
    ├── benchmark_runner.py  # 基准测试运行器
    └── eval_tasks.yaml      # 测试任务定义
```

## SOW 交付对照

| SOW要求 | AgenticSRE对应模块 | 状态 |
|---------|-------------------|------|
| 面向智算/通算的专用智能体 | agents/ (8个专用Agent) | ✅ |
| 告警压缩与根因推荐 | agents/alert_agent.py | ✅ |
| 多智能体协作范式 | orchestrator/ (Pipeline/Daemon) | ✅ |
| 多智能体行为可观测性与验证 | observability/ (Tracer/Validator) | ✅ |
| 假设推理的持续演化 | memory/ + orchestrator/rca_engine.py | ✅ |
| 根因推荐准确率≥80% | eval/benchmark_runner.py | ✅ |
| 根因定位准确率提升10% | memory/context_learner.py (持续演化) | ✅ |

## License

Research Project — Huawei 2012 Lab
