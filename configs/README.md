# `configs/` 配置目录指南

部署后用户需要关心的文件只有 3 个：

| 文件 | 作用 | 何时改 |
|---|---|---|
| `../.env` | 凭据（API Key、SSH 跳板等） | **必改** — 部署前先 `cp .env.example .env` |
| `config.yaml` | 主配置（LLM / K8s / 观测后端 / Pipeline 行为） | 接入新观测后端或调参时改 |
| `clusters.yaml` | 故障注入目标（K8s 集群 + GPU 主机档案） | 多集群 / 切换 GPU 主机时改；**也可在 UI"管理目标"按钮里在线改** |

其余文件作用说明：

| 文件 | 作用 |
|---|---|
| `config_cluster.yaml` | 备用：跑在 K8s 节点上时的配置模板（默认不用） |
| `config_loader.py` | Python 加载器（dataclass schema），不要手改 |
| `domains/` | 故障域知识库（generic_linux / kubernetes）— 由系统读取，不需手改 |

---

## 加载优先级

```
.env (环境变量)
  ↓ 替换 ${VAR_NAME}
configs/config.yaml   ←  主配置
                          └─ 若 fault_targets: 段缺失
configs/clusters.yaml  ←  fault_targets 段的默认值
                          └─ 若 data/cluster_profiles.json 存在
data/cluster_profiles.json  ←  UI 在线编辑写入的覆盖值（最高优先级）
```

修改 `clusters.yaml` 后**首次重启**才生效。如果 `data/cluster_profiles.json` 已存在，会优先使用 JSON 里的内容；想让 YAML 重新生效需要先删除该 JSON。

---

## 常见配置场景

### 1. 只跑单集群 + 单 GPU 主机

把 `clusters.yaml` 里 `k8s_clusters` 留默认，`llm_hosts` 删到只剩一条即可。

### 2. 多 K8s 集群（dev / staging / prod）

为每个集群挂一个 kubeconfig 到容器，编辑 `docker-compose.yaml`：
```yaml
volumes:
  - ~/.kube/config:/root/.kube/config:ro
  - ~/.kube/staging-config:/root/.kube/staging-config:ro   # 新增
```
然后在 `clusters.yaml` 加一条：
```yaml
- id: staging
  name: "Staging 集群"
  kubeconfig: "/root/.kube/staging-config"
  context: ""              # 或填该 kubeconfig 里的 context 名
  default_namespace: "default"
  description: "staging 环境"
```

### 3. GPU 主机直连（无跳板）

`clusters.yaml > llm_hosts` 把 `jump_host` 留空即可：
```yaml
- id: local-a100
  name: "本地 A100"
  host: "10.0.0.50"
  ssh_user: "ubuntu"
  ssh_key_path: "/root/.ssh/id_ed25519"
  jump_host: ""            # 空 = 直连
  gpu: "NVIDIA A100 80G"
  role: "本地推理"
```

### 4. 换 LLM Provider（OpenAI / Qwen / Claude / 自建 vLLM）

系统调用模型走 **OpenAI 兼容协议**，任何兼容此协议的服务都能直接接。
`configs/config.yaml > llm:` 段头部有详细字段说明，紧随其后是 **4 套
provider 预设**（注释状态），按需取消注释、替换原 `llm:` 段即可。

**操作 3 步**：
1. 打开 `configs/config.yaml`，找到 `# ─── LLM Provider 预设 ───` 注释块
2. 选一个预设（OpenAI / 通义千问 / OpenRouter+Claude / 本地 vLLM），把
   它的 `llm:` 块解开注释并**删掉**默认的 DeepSeek `llm:` 块
3. 打开 `.env`，按预设里 `${XXX_API_KEY}` 占位的变量名填实际 key

示例：切到通义千问
```yaml
# configs/config.yaml
llm:
  api_key: "${DASHSCOPE_API_KEY}"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model: "qwen3-max"
  temperature: 0.1
  max_tokens: 32768
  timeout: 300
```
```bash
# .env
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx
```

重启容器：`./deploy_docker.sh` 或 `docker compose restart`。

> ⚠️ **`base_url` 末尾要带 `/v1`**（DeepSeek 除外，它的网关同时接受
> 带与不带）。代码用 OpenAI SDK 原样拼接 `/chat/completions`，不会
> 自动加 `/v1`。如果 401/404 多半就是这个原因。

---

## 通过 UI 配置（推荐）

部署后打开 `http://<host>:8080`，**通算 / 智算故障实验** 页面顶部都有
"管理目标"按钮 — 增删改集群 / GPU 主机不需要重启容器，编辑保存后立即
写入 `data/cluster_profiles.json` 并热加载到内存。

适合：临时加一个测试目标 / 切换默认集群 / 一次性试验。

YAML 适合：版本化的基线默认值（部署到新环境时直接生效）。
