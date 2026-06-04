# AgenticSRE 用户手册

面向 **部署者 / 运维使用者** 的端到端操作指南。
开发/二次开发请看 `doc/technical_manual.md`，配置字段细节请看 `configs/README.md`。

---

## 目录

1. [系统能做什么](#1-系统能做什么)
2. [5 分钟部署](#2-5-分钟部署)
3. [首次配置](#3-首次配置)
4. [Dashboard 走查](#4-dashboard-走查)
5. [典型工作流](#5-典型工作流)
6. [运行模式](#6-运行模式)
7. [常见问题](#7-常见问题)
8. [进阶链接](#8-进阶链接)

---

## 1. 系统能做什么

AgenticSRE 是一个面向 **K8s 微服务** 与 **GPU 推理集群** 的多智能体运维平台。

| 你的角色 | 它能帮你 |
|---|---|
| **SRE / 值班** | 7×24 检测告警 → 自动 RCA → 出具诊断报告（无需手动跑 kubectl） |
| **架构师** | 跨指标/日志/链路/事件四路证据关联，避免单维度误判 |
| **研究人员** | 在 6 种多智能体协作范式（Chain / ReAct / Reflection / Plan-and-Execute / Debate / Voting）之间对比，跑基准评估 |
| **混沌工程** | 内置"通算 / 智算"两套故障注入实验室，一键打 CPU/内存/网络/GPU 故障 |

部署后只有 **一个 Web Dashboard**（默认 `http://<部署机>:8080`）。命令行模式仅给后台自动化用。

---

## 2. 5 分钟部署

### 2.1 准备一台机器

- Linux x86_64（推荐 Ubuntu 22.04 / CentOS 8+）
- Docker ≥ 20.10、docker-compose plugin
- 4 vCPU / 8 GB RAM 起步
- 能访问 K8s API（`~/.kube/config` 可用）
- 能访问 LLM API（DeepSeek / OpenAI / 通义千问 / 本地 vLLM 任选）

> macOS 也能跑，但 `docker-compose.yaml` 用了 `network_mode: host`，
> macOS 上访问 `http://localhost:8080` 不通 — 这是 Docker Desktop 限制，
> 仅影响访问，容器内部正常。生产请用 Linux。

### 2.2 一键部署

```bash
# 1. 解包
tar xzf agenticsre-release-YYYYMMDD.tar.gz
cd agenticsre-release-YYYYMMDD

# 2. 准备凭据
cp .env.example .env
vim .env                  # 至少填 LLM_API_KEY

# 3. 起服务
./deploy_docker.sh
```

`deploy_docker.sh` 会：
1. 加载随包镜像 `agenticsre:latest`（无需联网构建）
2. 自动展开容器内挂载需要的源码目录
3. 缺 `~/.kube/config` / `~/.ssh` 时建占位避免起不来
4. `docker compose up -d --no-build`

默认部署不会执行 `docker build`，也不会重新下载 apt/pip 依赖。只有明确执行
`./deploy_docker.sh --build` 时才会联网重建镜像。

完成后浏览器打开：`http://<部署机IP>:8080`

### 2.3 验证

```bash
docker compose ps                   # 容器是 Up 状态
docker compose logs -f --tail=50    # 看启动日志，无 ERROR
curl -sf http://localhost:8080/api/health || echo "FAIL"
```

UI 上 "集群概览" 页能看到节点和 Pod 列表 = 一切就绪。

---

## 3. 首次配置

需要关心的文件只有 3 个，**都在解包后的目录里**：

| 文件 | 改什么 | 何时改 |
|---|---|---|
| `.env` | LLM API Key、SSH 跳板凭据 | **必改** |
| `configs/config.yaml` | LLM provider、Prometheus/ES/Jaeger 地址、Pipeline 行为 | 接入新观测后端 / 切 LLM 时改 |
| `configs/clusters.yaml` | 故障实验的 K8s 集群清单 + GPU 主机清单 | 多集群 / 切 GPU 主机时改（也能在 UI 上"管理目标"按钮里在线改）|

字段说明详见 `configs/README.md`。三个最常见的场景：

### 场景 A：换 LLM Provider

打开 `configs/config.yaml`，找到 `# ─── LLM Provider 预设 ───` 注释块，
里面有 OpenAI / 通义千问 / OpenRouter+Claude / 本地 vLLM 四套预设。
取消注释你要的那段，删掉默认 DeepSeek 段，再去 `.env` 填对应 KEY。

> ⚠️ **`base_url` 末尾要带 `/v1`**（DeepSeek 网关例外）。
> 代码用 OpenAI SDK 原样拼 `/chat/completions`，不会自动加。
> 401/404 错误大概率是这个原因。

### 场景 B：接入自己的 K8s 集群

直接把宿主机 `~/.kube/config` 挂进容器即可（`docker-compose.yaml` 已挂）。
多集群时编辑 `configs/clusters.yaml > k8s_clusters` 添加条目，
每条挂一份 kubeconfig 到容器内对应路径。

### 场景 C：接入自己的可观测后端

`configs/config.yaml > observability:` 段改 URL：

```yaml
observability:
  prometheus_url: "http://prometheus.your-domain:9090"
  elasticsearch_url: "http://es.your-domain:9200"
  jaeger_url: "http://jaeger.your-domain:16686"
  grafana_url: "http://grafana.your-domain:3000"
```

改后 `docker compose restart` 生效。

---

## 4. Dashboard 走查

左侧导航有 14 个页面，分 4 大类：

### 4.1 监控类（看现状）

| 页面 | 用途 |
|---|---|
| **集群概览** | 顶部 6 个统计卡 + 工作负载拓扑图（按节点/命名空间分组，异常高亮红色） |
| **指标监控** | Prometheus 自定义查询；右上"刷新"拉最新。配合 `config.yaml > detection.metric_checks` 看阈值 |
| **日志查询** | Elasticsearch 关键字 + 时间窗 + 服务过滤，搜近期日志 |
| **告警中心** | Prometheus AlertManager + K8s 事件合并视图。告警旁"→ RCA"按钮一键转诊 |
| **链路追踪** | Jaeger 链路浏览，按服务/操作/时长筛选 |
| **事件追踪** | K8s `kubectl get events`，按严重程度排序 |

### 4.2 智能分析类（让 Agent 干活）

| 页面 | 用途 |
|---|---|
| **根因分析** | 手动触发 RCA：填一段自然语言"问题描述"（如 *"test-social-network 命名空间 nginx-thrift Pod CrashLoopBackOff"*）→ 选范式 → 跑。出具 5 阶段诊断 + 报告 |
| **知识库** | 系统沉淀的诊断规则、历史故障案例、领域知识。可手动新增规则 |
| **故障报告** | 历史 RCA 的可视化报告，可导出 |
| **演化闭环** | 专家反馈入口：标"对/错"，系统下次同类故障会自学习 |

### 4.3 自动化类

| 页面 | 用途 |
|---|---|
| **守护进程** | 7×24 自动检测开关。开启后无需手动触发 RCA，达到阈值的告警自动跑 Pipeline |
| **Hermes 助手** | 对话式运维助手，自然语言查询集群状态 / 跑诊断 |

### 4.4 故障注入实验室

| 页面 | 用途 |
|---|---|
| **通算故障实验** | 对 K8s 容器/Pod/节点注入故障（CPU 满载、内存挤压、Pod kill、网络丢包等） |
| **智算故障实验** | 对 vLLM/GPU 推理服务注入故障（GPU 显存爆、推理排队、模型权重损坏等） |

**两个实验室通用流程**：
1. 顶部选目标（通算选"集群"+namespace 覆盖；智算选 K8s 模式或裸机 GPU 模式）
2. 选场景（下拉列表，每个场景旁有描述卡片）
3. ☑ Dry-run（建议先勾上看命令文本，确认无误再实跑）
4. 点 **注入** → 看下方日志窗
5. 完事点 **清理/恢复** 撤销

**目标管理**：右上角"管理目标"按钮 = 增删改集群和 GPU 主机，写入 `data/cluster_profiles.json`，**热加载无需重启**。

---

## 5. 典型工作流

### 工作流 1：值班发现告警 → 自动 RCA

1. 进 **告警中心**，看到 nginx-thrift CPU > 95% 告警
2. 点告警行右侧 **"→ RCA"** 按钮（或复制告警文本到 **根因分析** 页）
3. 范式选 **Plan-and-Execute**（默认，平衡），点 **开始**
4. 等 30s ~ 2min，Pipeline 自动跑完 5 阶段
5. 进 **故障报告** 页，导出报告给团队

### 工作流 2：跑一次通算混沌实验

1. 进 **通算故障实验**
2. 目标集群选 "默认集群"，namespace 填 `test-social-network`
3. 场景选 `sn-cpu-stress`（社交网络 CPU 压测）
4. ☑ Dry-run → 点注入 → 看日志确认命令对
5. ☐ 取消 Dry-run → 注入 → 在另一窗口看监控指标飙起来
6. 实验完点 **清理**

### 工作流 3：评估多个范式哪个更好

```bash
# 在宿主机跑评估脚本（不走 UI）
docker exec -it agenticsre bash
cd /app && python -m eval.benchmark_runner --paradigms all --suite social-network
# 输出 eval/results/comparison_report.md
```

---

## 6. 运行模式

通常只用 **Web UI** 模式（默认起）。命令行模式给 CI / 后台任务用：

```bash
# 进容器
docker exec -it agenticsre bash

# 1. 守护模式：7×24 后台轮询，发现就自动 RCA
python main.py --mode daemon

# 2. 单次 Pipeline：跑一次 5 阶段
python main.py --mode pipeline

# 3. 单次 RCA：基于自然语言查询
python main.py --mode rca --query "pod CrashLoopBackOff in namespace default"

# 4. 看守护进程状态
python main.py --mode status
```

行为参数全在 `configs/config.yaml > runtime / daemon / pipeline` 段调。

---

## 7. 常见问题

### Q1: UI 打不开 / 502

```bash
docker compose ps                       # 容器是否 Up
docker compose logs --tail=100          # 看最后报错
docker exec agenticsre curl -sf localhost:8080/api/health   # 容器内自测
```
- macOS 上访问 host 端口不通是 Docker Desktop 限制，换 Linux。
- 容器内自测通但宿主访问不通 = 端口被占用或防火墙。

### Q2: 跑 RCA 报 `401 Unauthorized` 或 `404 Not Found`（LLM 调用失败）

- 检查 `.env` 里 `LLM_API_KEY` 是否填对
- 检查 `configs/config.yaml > llm.base_url` 是否带 `/v1`（DeepSeek 除外）
- 检查 `llm.model` 是否是该 provider 支持的模型 ID

### Q3: 集群查不到 / Pod 列表为空

- 检查 `~/.kube/config` 是否挂进了容器（`docker exec agenticsre kubectl get nodes` 自测）
- 跨集群时确认 `configs/clusters.yaml` 的 kubeconfig 路径在容器内能访问到

### Q4: 故障实验注入失败 `permission denied (publickey)`

- 智算"裸机 GPU 主机"模式需要容器能 SSH 到目标机
- 确认 `docker-compose.yaml` 已挂载 `~/.ssh:/root/.ssh:ro`
- 确认目标机 `~/.ssh/authorized_keys` 含部署机的公钥
- 跳板机模式需保证跳板机也信任部署机

### Q5: 配置改了不生效

| 改了什么 | 怎么生效 |
|---|---|
| `.env` | `docker compose restart` |
| `configs/config.yaml` | `docker compose restart` |
| `configs/clusters.yaml` | `docker compose restart`（且 `data/cluster_profiles.json` 不存在；若已存在以 json 优先，需先删它） |
| UI"管理目标"按钮改的 | 立即生效（写入 `data/cluster_profiles.json` 热加载） |

### Q6: 想完全重置

```bash
docker compose down -v
rm -rf data/cluster_profiles.json data/memory data/evolution
./deploy_docker.sh
```

⚠️ 这会清掉所有学习到的规则和历史报告，谨慎。

---

## 8. 进阶链接

| 你想 | 看哪里 |
|---|---|
| 看每个配置字段什么意思 | `configs/README.md` |
| 改 Pipeline 行为 / 加新观测后端 | `configs/config.yaml` 内注释 |
| 了解系统架构 / 二次开发 | `doc/technical_manual.md` |
| 接入 Claude/Copilot 当 MCP 工具 | `mcp_server.py` 头部 + 技术手册 §11 |
| 看本次发布有啥变化 | `RELEASE_NOTES.md` |

---

> 反馈 / 问题：联系研发团队，或在 `data/expert_feedback/` 留底，
> 演化闭环页可见。
