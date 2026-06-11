"""
AgenticSRE Web Dashboard
FastAPI-based single-page application with SSE streaming.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ── Setup Paths ──
APP_DIR = Path(__file__).parent
ROOT_DIR = APP_DIR.parent
sys.path.insert(0, str(ROOT_DIR))
SCENARIOS_FILE = ROOT_DIR / "eval" / "fault_scenarios.yaml"
LLM_FAULT_SCENARIOS_OVERLAY_FILE = ROOT_DIR / "eval" / "llm_fault_scenarios.yaml"
HEAL_RECIPES_FILE = ROOT_DIR / "configs" / "heal_recipes.yaml"


def _default_llm_fault_injector_dir() -> Path:
    configured = os.environ.get("VLLM_FAULT_INJECTOR_DIR")
    if configured:
        return Path(configured)

    candidates = [
        ROOT_DIR / "vllm_fault_injector",
        ROOT_DIR.parent / "vllm_fault_injector",
        ROOT_DIR.parent.parent / "vllm_fault_injector",
        Path.home() / "vllm_fault_injector",
    ]
    for candidate in candidates:
        if (candidate / "scenarios.yaml").exists():
            return candidate
    return candidates[-1]


LLM_FAULT_INJECTOR_DIR = _default_llm_fault_injector_dir()
LLM_FAULT_SCENARIOS_FILE = LLM_FAULT_INJECTOR_DIR / "scenarios.yaml"
LLM_FAULT_ENVIRONMENT = {
    "jump_host": {
        "host": os.environ.get("LLM_FAULT_JUMP_HOST", "222.200.180.22"),
        "user": os.environ.get("LLM_FAULT_JUMP_USER", "guest"),
        "network": "校园网直连",
    },
    "targets": [
        {
            "name": "T4-127",
            "host": os.environ.get("LLM_FAULT_T4_127_HOST", "33.33.33.127"),
            "user": os.environ.get("LLM_FAULT_T4_127_USER", "root"),
            "gpu": "NVIDIA T4 x3",
            "role": "vLLM 推理服务 / 负载节点",
        },
        {
            "name": "T4-128",
            "host": os.environ.get("LLM_FAULT_T4_128_HOST", "33.33.33.128"),
            "user": os.environ.get("LLM_FAULT_T4_128_USER", "root"),
            "gpu": "NVIDIA T4 x3",
            "role": "vLLM 推理服务 / 对照节点",
        },
    ],
    "credential_mode": "passwords are runtime-only; do not persist in AgenticSRE",
}

from configs.config_loader import get_config, reload_config
from tools import build_tool_registry, LLMClient
from agents import DetectionAgent, AlertAgent
from orchestrator.pipeline import Pipeline
from orchestrator.daemon import Daemon

logger = logging.getLogger(__name__)

# ── FastAPI App ──
app = FastAPI(title="AgenticSRE Dashboard", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# ── Shared State ──
_state = {
    "config": None,
    "pipeline": None,
    "daemon": None,
    "daemon_thread": None,
    "detection_signals": deque(maxlen=200),
    "rca_runs": {},  # run_id → dict
    "pipeline_logs": deque(maxlen=500),
    "daemon_logs": deque(maxlen=500),
    "sse_subscribers": [],
    "prometheus_url": None,
    "jaeger_url": None,
    "fault_experiment_tasks": {},
    "fault_experiment_cancel": {},
}

_RCA_HISTORY_FILE = ROOT_DIR / "data" / "rca_history.json"
_PLATFORM_CONFIG_FILE = ROOT_DIR / "data" / "platform_config.json"
_DETECTION_CONFIG_FILE = ROOT_DIR / "data" / "detection_config.json"
_FAULT_TARGETS_FILE = ROOT_DIR / "data" / "cluster_profiles.json"
_FAULT_RUNS_FILE = ROOT_DIR / "data" / "fault_runs.json"
_FAULT_EXPERIMENTS_FILE = ROOT_DIR / "data" / "fault_experiments.json"
_FAULT_EXPERIMENT_ARTIFACT_DIR = ROOT_DIR / "data" / "fault_experiments"
_HEAL_RUNS_FILE = ROOT_DIR / "data" / "heal_runs.json"
_HEAL_ARTIFACT_DIR = ROOT_DIR / "data" / "heal_runs"
_HIGH_RISK_LLM_FAULT_TYPES = {
    "weight_corruption",
    "tokenizer_error",
    "process_hang",
    "model_permission_error",
    "http_503",
    "gpu_clock_throttle",
    "rdma_failure",
    "cuda_ctx_corruption",
    "disk_pressure",
    "fd_exhaustion",
}


def _load_rca_history():
    """Load persisted RCA runs from disk on startup."""
    if _RCA_HISTORY_FILE.exists():
        try:
            data = json.loads(_RCA_HISTORY_FILE.read_text(encoding="utf-8"))
            for run in data:
                _state["rca_runs"][run["id"]] = run
            logger.info(f"Loaded {len(data)} RCA runs from disk")
        except Exception as e:
            logger.warning(f"Failed to load RCA history: {e}")


def _save_rca_history():
    """Persist all completed/failed RCA runs to disk."""
    try:
        _RCA_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        runs = [
            r for r in _state["rca_runs"].values()
            if r.get("status") in ("completed", "failed")
        ]
        # Keep last 200 runs
        runs.sort(key=lambda r: r.get("started_at", 0), reverse=True)
        runs = runs[:200]
        _RCA_HISTORY_FILE.write_text(
            json.dumps(runs, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save RCA history: {e}")


def _save_detection_config():
    """Persist detection config overrides to disk."""
    try:
        cfg = _get_config()
        det = cfg.detection
        data = {
            "sources_enabled": det.sources_enabled,
            "metric_checks": det.metric_checks,
            "critical_event_reasons": det.critical_event_reasons,
            "critical_pod_reasons": det.critical_pod_reasons,
            "default_detect_methods": det.default_detect_methods,
            "default_lookback_m": det.default_lookback_m,
            "default_z_threshold": det.default_z_threshold,
            "default_ewma_span": det.default_ewma_span,
            "categories_enabled": det.categories_enabled,
            "business_services": det.business_services,
            "db_services": det.db_services,
            "thresholds": det.thresholds,
        }
        _DETECTION_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DETECTION_CONFIG_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info("Detection config saved to disk")
    except Exception as e:
        logger.warning(f"Failed to save detection config: {e}")


def _load_platform_config_file() -> Dict[str, Any]:
    """Load persisted platform configuration-center overrides."""
    if not _PLATFORM_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(_PLATFORM_CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to load platform config: {e}")
        return {}


def _save_platform_config_file(data: Dict[str, Any]) -> None:
    """Persist platform configuration-center overrides."""
    try:
        _PLATFORM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PLATFORM_CONFIG_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save platform config: {e}")
        raise


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}***{text[-4:]}"


def _apply_platform_config() -> None:
    """Apply persisted config-center overrides to the in-memory AppConfig."""
    data = _load_platform_config_file()
    if not data:
        return
    cfg = _get_config()

    llm = data.get("llm")
    if isinstance(llm, dict):
        for field_name in ("base_url", "model", "temperature", "max_tokens", "timeout"):
            if field_name in llm and hasattr(cfg.llm, field_name):
                current = getattr(cfg.llm, field_name)
                value = llm[field_name]
                try:
                    if isinstance(current, bool):
                        value = bool(value)
                    elif isinstance(current, int):
                        value = int(value)
                    elif isinstance(current, float):
                        value = float(value)
                except Exception:
                    pass
                setattr(cfg.llm, field_name, value)
        if llm.get("api_key"):
            cfg.llm.api_key = str(llm["api_key"])

    detection = data.get("detection")
    if isinstance(detection, dict):
        det = cfg.detection
        if isinstance(detection.get("sources_enabled"), dict):
            det.sources_enabled.update(detection["sources_enabled"])
        if isinstance(detection.get("metric_checks"), list):
            det.metric_checks = detection["metric_checks"]
        if isinstance(detection.get("default_detect_methods"), list):
            det.default_detect_methods = detection["default_detect_methods"]
        for name, caster in {
            "default_algorithm": str,
            "default_lookback_m": int,
            "default_z_threshold": float,
            "default_ewma_span": int,
            "min_samples": int,
            "confirmation_points": int,
            "relative_change_threshold": float,
            "spectral_residual_threshold": float,
        }.items():
            if name in detection and hasattr(det, name):
                try:
                    setattr(det, name, caster(detection[name]))
                except Exception:
                    setattr(det, name, detection[name])
        if isinstance(detection.get("thresholds"), dict):
            det.thresholds.update(detection["thresholds"])

    remediation = data.get("remediation")
    if isinstance(remediation, dict):
        rem = cfg.remediation
        for name, value in remediation.items():
            if not hasattr(rem, name):
                continue
            current = getattr(rem, name)
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, list) and not isinstance(value, list):
                    value = [value]
            except Exception:
                pass
            setattr(rem, name, value)


def _reload_runtime_config() -> None:
    """Reload YAML/env config then re-apply persisted platform overrides."""
    _state["config"] = reload_config()
    if _PLATFORM_CONFIG_FILE.exists():
        _apply_platform_config()
    else:
        _load_detection_config()
    _load_fault_targets()
    _state["pipeline"] = None
    _state["daemon"] = None


def _platform_config_schema() -> Dict[str, Any]:
    """Describe configuration-center parameters for operators and API clients."""
    return {
        "llm": {
            "label": "大模型",
            "description": "RCA、多智能体协作和自愈建议生成使用的大模型连接参数。",
            "fields": {
                "model": {"label": "模型名称", "type": "string", "default": "deepseek-v4-pro", "description": "调用的大模型标识，必须与推理服务实际部署的模型名称一致。"},
                "base_url": {"label": "服务地址", "type": "string", "default": "https://api.deepseek.com", "description": "OpenAI 兼容接口的基础 URL，例如 https://host/v1。"},
                "api_key": {"label": "API Key", "type": "secret", "default": "", "description": "访问大模型服务的凭据。留空保存时保持原值，不会通过接口返回明文。"},
                "temperature": {"label": "采样温度", "type": "number", "default": 0.1, "min": 0, "max": 2, "description": "控制输出随机性。故障诊断建议使用较低值，以提高结果稳定性。"},
                "max_tokens": {"label": "最大输出 Token", "type": "integer", "default": 65536, "min": 1, "description": "单次模型调用允许生成的最大 Token 数，过大会增加延迟和资源消耗。"},
                "timeout": {"label": "请求超时", "type": "integer", "default": 300, "min": 1, "unit": "秒", "description": "单次大模型请求的超时时间。复杂诊断可适当增大。"},
            },
        },
        "detection": {
            "label": "指标异常检测",
            "description": "告警中心指标异常检测的算法和判定参数。",
            "fields": {
                "default_algorithm": {"label": "默认算法", "type": "enum", "default": "zscore", "options": ["zscore", "iqr", "threshold", "spectral_residual"], "description": "未对指标单独指定算法时使用的异常检测算法。"},
                "default_z_threshold": {"label": "Z-Score 阈值", "type": "number", "default": 3.0, "min": 0, "description": "偏离均值超过该标准差倍数时标记异常。值越小越敏感。"},
                "default_lookback_m": {"label": "回看窗口", "type": "integer", "default": 30, "min": 1, "unit": "分钟", "description": "异常检测读取的历史指标窗口长度。"},
                "default_ewma_span": {"label": "EWMA 跨度", "type": "integer", "default": 10, "min": 1, "description": "EWMA 平滑跨度，用于降低短时噪声影响。"},
                "min_samples": {"label": "最少样本数", "type": "integer", "default": 12, "min": 3, "description": "执行统计检测前要求的最少数据点数量。样本不足时不会判定异常。"},
                "confirmation_points": {"label": "连续确认点", "type": "integer", "default": 1, "min": 1, "description": "连续出现多少个异常点后生成告警。增大可减少瞬态抖动告警。"},
                "relative_change_threshold": {"label": "相对变化阈值", "type": "number", "default": 0.0, "min": 0, "description": "相对基线的最小变化比例。0 表示不额外限制。"},
                "spectral_residual_threshold": {"label": "频谱残差阈值", "type": "number", "default": 3.0, "min": 0, "description": "spectral_residual 算法的异常分数阈值。"},
                "sources_enabled": {"label": "检测源开关", "type": "object", "description": "启用或停用 Prometheus、K8s Event、Pod、Node 等检测来源。"},
                "metric_checks": {"label": "指标检查项", "type": "array", "description": "自定义 PromQL、单位、标签和告警阈值列表。"},
                "default_detect_methods": {"label": "默认检测方法", "type": "array", "description": "默认组合执行的检测方法列表。"},
                "thresholds": {"label": "指标阈值覆盖", "type": "object", "description": "按指标名称覆盖默认阈值。"},
            },
        },
        "remediation": {
            "label": "自愈策略",
            "description": "自愈动作生成、审批和自动执行的安全控制参数。",
            "fields": {
                "enabled": {"label": "启用真实自愈", "type": "boolean", "default": False, "risk": "high", "description": "允许执行真实变更命令。关闭时只能生成建议和预演。"},
                "dry_run": {"label": "默认 Dry-run", "type": "boolean", "default": True, "description": "默认仅预演命令，不修改集群。建议保持开启。"},
                "require_approval": {"label": "需要审批", "type": "boolean", "default": True, "description": "真实执行前要求人工审批。生产环境建议保持开启。"},
                "confidence_threshold": {"label": "置信度阈值", "type": "number", "default": 0.85, "min": 0, "max": 1, "description": "诊断置信度达到该值后才允许进入自愈执行流程。"},
                "max_steps": {"label": "最大动作数", "type": "integer", "default": 5, "min": 1, "description": "单次自愈允许执行的最大命令数量。"},
                "max_rollback_depth": {"label": "最大回滚深度", "type": "integer", "default": 5, "min": 1, "description": "单次自愈保存和执行回滚动作的最大层数。"},
                "max_auto_risk_level": {"label": "自动执行最高风险", "type": "enum", "default": "medium", "options": ["low", "medium", "high"], "risk": "high", "description": "无需升级审批时允许自动执行的最高风险等级。"},
                "schedule": {"label": "执行时间窗", "type": "string", "default": "", "description": "允许自动执行的计划时间表达式。留空表示不限制。"},
                "blackout_windows": {"label": "禁止执行时间窗", "type": "array", "description": "禁止自动执行的时间范围列表，例如业务高峰期。"},
                "canary_enabled": {"label": "启用金丝雀验证", "type": "boolean", "default": False, "description": "先在受控范围验证动作，再决定是否扩大执行范围。"},
                "canary_namespace": {"label": "金丝雀命名空间", "type": "string", "default": "test", "description": "金丝雀验证使用的 Kubernetes 命名空间。"},
                "require_confirmation_for_high_risk": {"label": "高风险二次确认", "type": "boolean", "default": True, "description": "高风险动作执行前强制要求二次确认。"},
                "recommend_only": {"label": "仅生成建议", "type": "boolean", "default": True, "description": "只提供处置建议，不自动执行命令。生产初期建议保持开启。"},
            },
        },
    }


def _platform_config_public() -> Dict[str, Any]:
    cfg = _get_config()
    raw = _load_platform_config_file()
    det = cfg.detection
    rem = cfg.remediation
    return {
        "stored": raw,
        "schema": _platform_config_schema(),
        "effective": {
            "llm": {
                "base_url": cfg.llm.base_url,
                "model": cfg.llm.model,
                "temperature": cfg.llm.temperature,
                "max_tokens": cfg.llm.max_tokens,
                "timeout": cfg.llm.timeout,
                "api_key_configured": bool(cfg.llm.api_key),
                "api_key_masked": _mask_secret(cfg.llm.api_key),
            },
            "detection": {
                "sources_enabled": det.sources_enabled,
                "metric_checks": det.metric_checks,
                "default_detect_methods": det.default_detect_methods,
                "default_algorithm": getattr(det, "default_algorithm", "zscore"),
                "default_lookback_m": det.default_lookback_m,
                "default_z_threshold": det.default_z_threshold,
                "default_ewma_span": det.default_ewma_span,
                "min_samples": getattr(det, "min_samples", 12),
                "confirmation_points": getattr(det, "confirmation_points", 1),
                "relative_change_threshold": getattr(det, "relative_change_threshold", 0.0),
                "spectral_residual_threshold": getattr(det, "spectral_residual_threshold", 3.0),
                "thresholds": det.thresholds,
            },
            "remediation": {
                "enabled": rem.enabled,
                "dry_run": getattr(rem, "dry_run", True),
                "max_steps": getattr(rem, "max_steps", 5),
                "confidence_threshold": rem.confidence_threshold,
                "max_rollback_depth": rem.max_rollback_depth,
                "require_approval": rem.require_approval,
                "schedule": getattr(rem, "schedule", ""),
                "blackout_windows": getattr(rem, "blackout_windows", []),
                "max_auto_risk_level": getattr(rem, "max_auto_risk_level", "medium"),
                "canary_enabled": getattr(rem, "canary_enabled", False),
                "canary_namespace": getattr(rem, "canary_namespace", "test"),
                "require_confirmation_for_high_risk": getattr(rem, "require_confirmation_for_high_risk", True),
                "recommend_only": getattr(rem, "recommend_only", True),
            },
        },
    }


def _load_detection_config():
    """Load persisted detection config overrides on startup."""
    if not _DETECTION_CONFIG_FILE.exists():
        return
    try:
        data = json.loads(_DETECTION_CONFIG_FILE.read_text(encoding="utf-8"))
        cfg = _get_config()
        det = cfg.detection
        if "sources_enabled" in data:
            det.sources_enabled.update(data["sources_enabled"])
        if "metric_checks" in data:
            det.metric_checks = data["metric_checks"]
        if "critical_event_reasons" in data:
            det.critical_event_reasons = data["critical_event_reasons"]
        if "critical_pod_reasons" in data:
            det.critical_pod_reasons = data["critical_pod_reasons"]
        if "default_detect_methods" in data:
            det.default_detect_methods = data["default_detect_methods"]
        if "default_lookback_m" in data:
            det.default_lookback_m = int(data["default_lookback_m"])
        if "default_z_threshold" in data:
            det.default_z_threshold = float(data["default_z_threshold"])
        if "default_ewma_span" in data:
            det.default_ewma_span = int(data["default_ewma_span"])
        if "categories_enabled" in data:
            det.categories_enabled.update(data["categories_enabled"])
        if "business_services" in data:
            det.business_services = data["business_services"]
        if "db_services" in data:
            det.db_services = data["db_services"]
        if "thresholds" in data:
            det.thresholds.update(data["thresholds"])
        logger.info("Loaded detection config from disk")
    except Exception as e:
        logger.warning(f"Failed to load detection config: {e}")


def _save_fault_targets():
    """Persist fault-target profiles (k8s_clusters + llm_hosts) to disk."""
    try:
        cfg = _get_config()
        data = {
            "k8s_clusters": list(cfg.fault_targets.k8s_clusters),
            "llm_hosts": list(cfg.fault_targets.llm_hosts),
        }
        _FAULT_TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FAULT_TARGETS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Fault targets saved to disk")
    except Exception as e:
        logger.warning(f"Failed to save fault targets: {e}")


def _load_fault_runs() -> List[Dict[str, Any]]:
    """Load persisted fault injection run records."""
    if not _FAULT_RUNS_FILE.exists():
        return []
    try:
        data = json.loads(_FAULT_RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load fault runs: {e}")
        return []


def _append_fault_run(record: Dict[str, Any]) -> None:
    """Persist one fault injection/cleanup record, keeping the latest 500."""
    try:
        _FAULT_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        runs = _load_fault_runs()
        runs.insert(0, record)
        runs = runs[:500]
        _FAULT_RUNS_FILE.write_text(
            json.dumps(runs, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save fault run: {e}")


def _merge_platform_section(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a section into platform_config.json and return the full document."""
    data = _load_platform_config_file()
    current = data.get(section)
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    data[section] = current
    data["updated_at"] = time.time()
    _save_platform_config_file(data)
    return data


def _load_fault_experiments() -> List[Dict[str, Any]]:
    """Load persisted live benchmark experiment records."""
    if not _FAULT_EXPERIMENTS_FILE.exists():
        return []
    try:
        data = json.loads(_FAULT_EXPERIMENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load fault experiments: {e}")
        return []


def _save_fault_experiment(record: Dict[str, Any]) -> None:
    """Upsert one experiment record and keep recent history bounded."""
    try:
        _FAULT_EXPERIMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        records = _load_fault_experiments()
        records = [r for r in records if r.get("id") != record.get("id")]
        records.insert(0, record)
        records = records[:200]
        _FAULT_EXPERIMENTS_FILE.write_text(
            json.dumps(records, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save fault experiment: {e}")


def _write_fault_experiment_artifact(run: Dict[str, Any]) -> Optional[str]:
    """Persist the full benchmark artifact for one run."""
    try:
        run_id = run.get("id") or f"experiment-{uuid.uuid4().hex[:10]}"
        _FAULT_EXPERIMENT_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = _FAULT_EXPERIMENT_ARTIFACT_DIR / f"{run_id}.json"
        path.write_text(
            json.dumps(run, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return str(path)
    except Exception as e:
        logger.warning(f"Failed to save fault experiment artifact: {e}")
        return None


HEAL_RECIPES: List[Dict[str, Any]] = [
    {
        "fault_type": "OOMKilled",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "容器被 OOMKilled，优先滚动重启或提高 Deployment 内存上限。",
        "signals": ["OOMKilled", "exit code 137", "memory limit"],
        "actions": [
            {
                "step": "滚动重启受影响 Deployment",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
            {
                "step": "查看 Pod 资源使用",
                "command": "kubectl top pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "PodCrashLoop",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Pod 反复崩溃，先采集日志，再重启或回滚 Deployment。",
        "signals": ["CrashLoopBackOff", "Back-off restarting failed container"],
        "actions": [
            {
                "step": "采集最近容器日志",
                "command": "kubectl logs <pod> -n <namespace> --tail=120 --previous",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "回滚 Deployment 到上一版本",
                "command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "rollback_command": "",
                "risk": "medium",
            },
        ],
    },
    {
        "fault_type": "ImagePullError",
        "category": "镜像",
        "risk": "medium",
        "requires_approval": True,
        "description": "镜像拉取失败，建议修正镜像 tag 或 imagePullSecret 后触发 rollout。",
        "signals": ["ImagePullBackOff", "ErrImagePull"],
        "actions": [
            {
                "step": "查看镜像拉取事件",
                "command": "kubectl describe pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "重启 Deployment 重新拉取镜像",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
        ],
    },
    {
        "fault_type": "NodeNotReady",
        "category": "节点",
        "risk": "high",
        "requires_approval": True,
        "description": "节点不可用，先 cordon 隔离并检查节点事件，必要时驱逐业务。",
        "signals": ["NodeNotReady", "node is not ready"],
        "actions": [
            {
                "step": "隔离不可用节点",
                "command": "kubectl cordon <node>",
                "rollback_command": "kubectl uncordon <node>",
                "risk": "high",
            },
            {
                "step": "查看节点详情",
                "command": "kubectl describe node <node>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "MetricAnomaly",
        "category": "指标",
        "risk": "low",
        "requires_approval": False,
        "description": "指标异常先做只读确认，避免误修复。",
        "signals": ["metric", "prometheus", "anomaly", "指标"],
        "actions": [
            {
                "step": "查看相关 Pod 资源使用",
                "command": "kubectl top pod -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "ProbeFailure",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Readiness/Liveness 探针失败，先查看事件和 rollout 状态，再执行滚动重启。",
        "signals": ["Unhealthy", "readiness probe failed", "liveness probe failed", "probe"],
        "actions": [
            {
                "step": "查看 Pod 事件与探针失败原因",
                "command": "kubectl describe pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "滚动重启受影响 Deployment",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
        ],
    },
    {
        "fault_type": "PendingScheduling",
        "category": "调度",
        "risk": "low",
        "requires_approval": False,
        "description": "Pod Pending 或 FailedScheduling，优先只读确认资源、污点和调度失败事件。",
        "signals": ["FailedScheduling", "Pending", "Insufficient cpu", "Insufficient memory"],
        "actions": [
            {
                "step": "查看 Pod 调度失败详情",
                "command": "kubectl describe pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "查看节点资源水位",
                "command": "kubectl top node",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "HighCpu",
        "category": "资源",
        "risk": "low",
        "requires_approval": False,
        "description": "CPU 使用率异常，默认只读确认热点 Pod，避免误扩缩容。",
        "signals": ["cpu", "CPU", "node_cpu_usage", "container_cpu"],
        "actions": [
            {
                "step": "查看命名空间内 Pod CPU 使用",
                "command": "kubectl top pod -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "查看 Deployment rollout 状态",
                "command": "kubectl rollout status deployment/<deployment> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "HighMemory",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "内存使用率异常或接近 OOM，先确认资源水位，再选择滚动重启或扩容。",
        "signals": ["memory", "内存", "node_memory_usage", "container_memory"],
        "actions": [
            {
                "step": "查看命名空间内 Pod 内存使用",
                "command": "kubectl top pod -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "滚动重启疑似内存泄漏 Deployment",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
        ],
    },
]

HEAL_RECIPES.extend([
    {
        "fault_type": "HighCPUUsage",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "CPU 使用率持续高位，参考 agenticSnail 的热点定位、资源调优、滚动恢复三段式策略。",
        "signals": ["HighCPU", "cpu usage", "container_cpu", "node_cpu_usage"],
        "actions": [
            {"step": "按 CPU 排序定位热点 Pod", "command": "kubectl top pod -n <namespace> --sort-by=cpu", "rollback_command": "", "risk": "low"},
            {"step": "提高 Deployment CPU limit", "command": "kubectl set resources deployment/<deployment> -n <namespace> --requests=cpu=500m --limits=1500m", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "滚动重启使 CPU 配置生效", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "HighMemoryUsage",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "内存热点或泄漏风险，先定位内存最高 Pod，再提升 limit 或重建异常副本。",
        "signals": ["HighMemory", "memory usage", "container_memory", "node_memory_usage"],
        "actions": [
            {"step": "按内存排序定位热点 Pod", "command": "kubectl top pod -n <namespace> --sort-by=memory", "rollback_command": "", "risk": "low"},
            {"step": "提高 Deployment memory limit", "command": "kubectl set resources deployment/<deployment> -n <namespace> --requests=memory=512Mi --limits=2Gi", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "滚动重启疑似内存泄漏 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "DiskPressure",
        "category": "节点",
        "risk": "high",
        "requires_approval": True,
        "description": "节点 DiskPressure 时先收集节点 Conditions，必要时隔离并迁移工作负载。",
        "signals": ["DiskPressure", "disk pressure", "filesystem pressure", "disk full"],
        "actions": [
            {"step": "查看节点磁盘压力和驱逐事件", "command": "kubectl describe node <node>", "rollback_command": "", "risk": "low"},
            {"step": "隔离磁盘压力节点", "command": "kubectl cordon <node>", "rollback_command": "kubectl uncordon <node>", "risk": "medium"},
            {"step": "驱逐节点工作负载释放调度压力", "command": "kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force --timeout=120s", "rollback_command": "kubectl uncordon <node>", "risk": "high"},
        ],
    },
    {
        "fault_type": "DNSResolution",
        "category": "网络",
        "risk": "medium",
        "requires_approval": True,
        "description": "DNS 解析异常时检查 kube-dns 端点，必要时滚动重启 CoreDNS。",
        "signals": ["dns", "coredns", "kube-dns", "lookup timeout"],
        "actions": [
            {"step": "检查 kube-dns Endpoints", "command": "kubectl get endpoints kube-dns -n kube-system -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 CoreDNS", "command": "kubectl rollout restart deployment/coredns -n kube-system", "rollback_command": "", "risk": "medium"},
            {"step": "验证 CoreDNS Pod 状态", "command": "kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "KubeProxyUnhealthy",
        "category": "网络",
        "risk": "medium",
        "requires_approval": True,
        "description": "kube-proxy 不健康时检查 DaemonSet 与 Pod，必要时触发 DaemonSet 滚动重启。",
        "signals": ["kube-proxy", "KubeProxyDown", "proxy rules"],
        "actions": [
            {"step": "查看 kube-proxy Pod 分布", "command": "kubectl get pods -n kube-system -l k8s-app=kube-proxy -o wide", "rollback_command": "", "risk": "low"},
            {"step": "重启 kube-proxy DaemonSet", "command": "kubectl rollout restart ds/kube-proxy -n kube-system", "rollback_command": "", "risk": "medium"},
            {"step": "等待 kube-proxy 发布完成", "command": "kubectl rollout status ds/kube-proxy -n kube-system", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "ApiServerUnhealthy",
        "category": "控制面",
        "risk": "high",
        "requires_approval": True,
        "description": "API Server 异常属于控制面高风险故障，只允许在审批后重建异常静态 Pod。",
        "signals": ["apiserver", "kube-apiserver", "TargetDown"],
        "actions": [
            {"step": "查看 API Server 静态 Pod", "command": "kubectl get pods -n kube-system -l component=kube-apiserver -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 API Server 事件详情", "command": "kubectl describe pod -n kube-system -l component=kube-apiserver", "rollback_command": "", "risk": "low"},
            {"step": "删除异常 API Server Pod 触发 kubelet 重建", "command": "kubectl delete pod -n kube-system -l component=kube-apiserver", "rollback_command": "", "risk": "high"},
        ],
    },
    {
        "fault_type": "EtcdUnhealthy",
        "category": "控制面",
        "risk": "high",
        "requires_approval": True,
        "description": "etcd 异常属于控制面存储高风险故障，默认只诊断，重建动作需审批和风险确认。",
        "signals": ["etcd", "etcdserver", "leader changed"],
        "actions": [
            {"step": "查看 etcd 静态 Pod", "command": "kubectl get pods -n kube-system -l component=etcd -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 etcd Pod 事件", "command": "kubectl describe pod -n kube-system -l component=etcd", "rollback_command": "", "risk": "low"},
            {"step": "重建异常 etcd Pod", "command": "kubectl delete pod -n kube-system -l component=etcd", "rollback_command": "", "risk": "high"},
        ],
    },
    {
        "fault_type": "KubeletUnhealthy",
        "category": "节点",
        "risk": "high",
        "requires_approval": True,
        "description": "kubelet 不健康时先隔离节点，必要时迁移工作负载，主机级 kubelet 重启留给人工执行。",
        "signals": ["kubelet", "KubeletDown", "node agent"],
        "actions": [
            {"step": "查看节点 Conditions", "command": "kubectl describe node <node>", "rollback_command": "", "risk": "low"},
            {"step": "隔离 kubelet 异常节点", "command": "kubectl cordon <node>", "rollback_command": "kubectl uncordon <node>", "risk": "medium"},
            {"step": "驱逐节点工作负载", "command": "kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force --timeout=120s", "rollback_command": "kubectl uncordon <node>", "risk": "high"},
        ],
    },
    {
        "fault_type": "VolumeMountFailed",
        "category": "存储",
        "risk": "medium",
        "requires_approval": True,
        "description": "挂载失败时检查 PV/PVC/Pod 事件，必要时重建 Pod 触发 kubelet 重新挂载。",
        "signals": ["FailedMount", "MountVolume", "FailedAttachVolume"],
        "actions": [
            {"step": "查看 Pod 挂载失败事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间 PVC 状态", "command": "kubectl get pvc -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "删除挂载异常 Pod 触发重新挂载", "command": "kubectl delete pod <pod> -n <namespace> --grace-period=30", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "ConfigMissing",
        "category": "配置",
        "risk": "medium",
        "requires_approval": True,
        "description": "ConfigMap/Secret 缺失时先定位引用关系，不自动生成未知业务配置，仅允许重建 Pod 重新挂载已补齐配置。",
        "signals": ["configmap not found", "secret not found", "not registered"],
        "actions": [
            {"step": "查看 Pod 配置引用和事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "列出命名空间 ConfigMap/Secret", "command": "kubectl get configmap,secret -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "配置补齐后重建 Pod", "command": "kubectl delete pod <pod> -n <namespace> --grace-period=30", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "ConfigKeyMissing",
        "category": "配置",
        "risk": "low",
        "requires_approval": True,
        "description": "ConfigMap 已存在但 key 缺失，默认只诊断和触发配置补齐后的重建，避免写入错误默认值。",
        "signals": ["non-existent config key", "references non-existent key"],
        "actions": [
            {"step": "查看 Pod 中缺失 key 的引用", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "导出命名空间 ConfigMap 供人工核对", "command": "kubectl get configmap -n <namespace> -o yaml", "rollback_command": "", "risk": "low"},
            {"step": "配置补齐后滚动重启 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "SchedulingFailed",
        "category": "调度",
        "risk": "medium",
        "requires_approval": True,
        "description": "调度失败时检查资源请求、污点、配额，必要时调整副本或资源以恢复可调度性。",
        "signals": ["FailedScheduling", "Unschedulable", "Insufficient cpu", "Insufficient memory"],
        "actions": [
            {"step": "查看 Pod 调度失败事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看节点资源水位", "command": "kubectl top node", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间 ResourceQuota", "command": "kubectl describe resourcequota -n <namespace>", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "ResourceQuotaExceeded",
        "category": "容量",
        "risk": "medium",
        "requires_approval": True,
        "description": "命名空间配额不足时先输出配额和使用量，配额调整需人工确认后执行。",
        "signals": ["exceeded quota", "ResourceQuota", "forbidden: quota"],
        "actions": [
            {"step": "查看 ResourceQuota 摘要", "command": "kubectl get resourcequota -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看 ResourceQuota 明细", "command": "kubectl describe resourcequota -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间资源使用", "command": "kubectl top pod -n <namespace>", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "HPAScalingFailed",
        "category": "弹性伸缩",
        "risk": "medium",
        "requires_approval": True,
        "description": "HPA 扩缩容失败时检查 scaleTargetRef、metrics-server 和目标 Deployment，必要时临时固定副本数。",
        "signals": ["FailedGetScale", "FailedGetResourceMetric", "FailedComputeMetricsReplicas"],
        "actions": [
            {"step": "查看 HPA 状态", "command": "kubectl get hpa -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 metrics APIService", "command": "kubectl get apiservice v1beta1.metrics.k8s.io", "rollback_command": "", "risk": "low"},
            {"step": "临时固定 Deployment 副本数", "command": "kubectl scale deployment/<deployment> -n <namespace> --replicas=1", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "DeploymentUnhealthy",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Deployment 副本不健康或探针失败，先确认 rollout 和 endpoints，再重启或回滚。",
        "signals": ["readiness probe failed", "liveness probe failed", "replicas unavailable"],
        "actions": [
            {"step": "检查 Deployment 发布状态", "command": "kubectl rollout status deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "检查服务端点", "command": "kubectl get endpoints <service> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "回滚到上一稳定版本", "command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "NetworkPolicyDeny",
        "category": "网络",
        "risk": "low",
        "requires_approval": True,
        "description": "NetworkPolicy 疑似阻断合法流量时默认只审计策略，不自动删除网络策略。",
        "signals": ["NetworkPolicy", "egress denied", "ingress denied", "network deny"],
        "actions": [
            {"step": "查看命名空间 NetworkPolicy", "command": "kubectl get networkpolicy -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看 NetworkPolicy 明细", "command": "kubectl describe networkpolicy -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看服务 Endpoints", "command": "kubectl get endpoints -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "HighLatency",
        "category": "性能",
        "risk": "medium",
        "requires_approval": True,
        "description": "P99 延迟或超时升高时先检查 endpoints 和资源热点，必要时扩容或滚动重启。",
        "signals": ["latency", "timeout", "p99", "slow"],
        "actions": [
            {"step": "检查服务 Endpoints", "command": "kubectl get endpoints <service> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看资源热点", "command": "kubectl top pod -n <namespace> --sort-by=cpu", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启高延迟 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "临时扩容分摊流量", "command": "kubectl scale deployment/<deployment> -n <namespace> --replicas=2", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "ServiceUnavailable",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "服务 5xx 或不可用时检查 Service/Endpoints/Deployment，优先恢复副本可用性。",
        "signals": ["ServiceUnavailable", "5xx", "no endpoints", "connection refused"],
        "actions": [
            {"step": "检查 Service Endpoints", "command": "kubectl get endpoints <service> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "检查 Deployment 状态", "command": "kubectl get deployment <deployment> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "TargetDown",
        "category": "监控",
        "risk": "low",
        "requires_approval": False,
        "description": "Prometheus TargetDown 先做目标发现和 endpoints 只读确认，避免把监控丢失误判为业务故障。",
        "signals": ["TargetDown", "scrape failed", "target disappeared"],
        "actions": [
            {"step": "查看命名空间 Service 和 Endpoints", "command": "kubectl get svc,endpoints -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看相关 Pod 状态", "command": "kubectl get pods -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "LogErrorSpike",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "错误日志突增时先采样最近日志，若与发布相关再滚动重启或回滚。",
        "signals": ["error spike", "log error", "ERROR rate"],
        "actions": [
            {"step": "采集最近错误日志样本", "command": "kubectl logs <pod> -n <namespace> --tail=500", "rollback_command": "", "risk": "low"},
            {"step": "查看 Deployment 发布历史", "command": "kubectl rollout history deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "回滚疑似问题发布", "command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "KubeDeploymentReplicasMismatch",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Deployment 期望副本和可用副本不一致，检查 ReplicaSet/Events 后滚动恢复。",
        "signals": ["KubeDeploymentReplicasMismatch", "replicas mismatch"],
        "actions": [
            {"step": "查看 Deployment 副本状态", "command": "kubectl get deployment <deployment> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 ReplicaSet 状态", "command": "kubectl get rs -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 Deployment 恢复副本", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "NodeClockNotSynchronising",
        "category": "节点",
        "risk": "low",
        "requires_approval": False,
        "description": "节点时钟不同步会影响证书、日志和控制面选主，当前系统只做 K8s 侧诊断并升级人工处理。",
        "signals": ["NodeClockNotSynchronising", "clock skew", "time sync"],
        "actions": [
            {"step": "查看节点 Conditions", "command": "kubectl describe node <node>", "rollback_command": "", "risk": "low"},
            {"step": "查看节点列表和版本", "command": "kubectl get nodes -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "AppArmorProfileUnsupported",
        "category": "安全",
        "risk": "low",
        "requires_approval": True,
        "description": "AppArmor/vArmor profile 不兼容导致容器创建失败时，默认只定位安全策略和 Pod 事件，不自动放宽安全边界。",
        "signals": ["AppArmor", "vArmor", "CreateContainerError", "profile unsupported"],
        "actions": [
            {"step": "查看 Pod 创建失败事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间 Pod 状态", "command": "kubectl get pods -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
])

_HEAL_FORBIDDEN = re.compile(
    r"\b(rm\s+-rf|mkfs|fdisk|dd\s+if|shutdown|reboot|halt|kill\s+-9|pkill|wipefs|format)\b",
    re.IGNORECASE,
)


def _load_heal_recipe_library() -> Dict[str, Any]:
    """Load external self-healing recipes, falling back to built-in recipes."""
    if HEAL_RECIPES_FILE.exists():
        try:
            data = yaml.safe_load(HEAL_RECIPES_FILE.read_text(encoding="utf-8")) or {}
            recipes = data.get("recipes") if isinstance(data, dict) else None
            if isinstance(recipes, list):
                return {
                    "version": data.get("version") or "external",
                    "description": data.get("description") or "",
                    "source": str(HEAL_RECIPES_FILE),
                    "recipes": recipes,
                }
        except Exception as e:
            logger.warning("Failed to load heal recipe library: %s", e)
    return {
        "version": "built-in",
        "description": "Built-in fallback self-healing recipes.",
        "source": "built-in",
        "recipes": HEAL_RECIPES,
    }


def _load_heal_recipes() -> List[Dict[str, Any]]:
    return list(_load_heal_recipe_library().get("recipes") or [])


def _load_heal_runs() -> List[Dict[str, Any]]:
    if not _HEAL_RUNS_FILE.exists():
        return []
    try:
        data = json.loads(_HEAL_RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load heal runs: {e}")
        return []


def _save_heal_run(record: Dict[str, Any]) -> None:
    try:
        _HEAL_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        runs = [r for r in _load_heal_runs() if r.get("id") != record.get("id")]
        runs.insert(0, record)
        runs = runs[:300]
        _HEAL_RUNS_FILE.write_text(
            json.dumps(runs, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        _HEAL_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        (_HEAL_ARTIFACT_DIR / f"{record['id']}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save heal run: {e}")


def _normalize_heal_fault_type(text: str) -> str:
    raw = str(text or "")
    lower = raw.lower()
    bracket_alert = re.search(r"\[([A-Za-z0-9_.:-]+)\]", raw)
    if bracket_alert:
        alert_name = bracket_alert.group(1).lower()
        if "kubeproxydown" in alert_name or "kube-proxy" in alert_name or "kubeproxy" in alert_name:
            return "KubeProxyUnhealthy"
        if "targetdown" in alert_name:
            if "kube-proxy" in lower or "kubeproxy" in lower:
                return "KubeProxyUnhealthy"
            if "apiserver" in lower or "api server" in lower or "kube-apiserver" in lower:
                return "ApiServerUnhealthy"
            if "kubelet" in lower:
                return "KubeletUnhealthy"
            if "etcd" in lower:
                return "EtcdUnhealthy"
            return "TargetDown"
        if "kubeapiserver" in alert_name or "apiserver" in alert_name:
            return "ApiServerUnhealthy"
        if "etcd" in alert_name:
            return "EtcdUnhealthy"
        if "kubelet" in alert_name:
            return "KubeletUnhealthy"
        if "nodeclocknotsynchronising" in alert_name or "nodeclocknotsynchronizing" in alert_name:
            return "NodeClockNotSynchronising"
    if any(x in lower for x in ("non-existent config key", "non-existent configmap key", "references non-existent key")):
        return "ConfigKeyMissing"
    if any(x in lower for x in ("oomkilled", "out of memory", "exit code 137", "oom")):
        return "OOMKilled"
    if any(x in lower for x in ("apparmor", "varmor", "sandbox.varmor.org")) and any(x in lower for x in ("createcontainererror", "blocked", "denied", "forbidden")):
        return "AppArmorProfileUnsupported"
    if any(x in lower for x in ("diskpressure", "disk pressure", "filesystem pressure", "disk full")):
        return "DiskPressure"
    if any(x in lower for x in ("dns", "coredns", "kube-dns")):
        return "DNSResolution"
    if any(x in lower for x in ("apiserver", "api server", "kube-apiserver")):
        return "ApiServerUnhealthy"
    if "etcd" in lower:
        return "EtcdUnhealthy"
    if "kubelet" in lower:
        return "KubeletUnhealthy"
    if any(x in lower for x in ("kube-proxy", "kubeproxy")):
        return "KubeProxyUnhealthy"
    if any(x in lower for x in ("exceeded quota", "forbidden: quota", "resourcequota", "limitrange", "denied the request")):
        return "ResourceQuotaExceeded"
    if any(x in lower for x in ("failedgetscale", "failedgetresourcemetric", "failedcomputemetricsreplicas", "unable to get target")):
        return "HPAScalingFailed"
    if any(x in lower for x in ("crashloopbackoff", "crash loop", "back-off")):
        return "PodCrashLoop"
    if any(x in lower for x in ("kubepodnotready", "pod not ready", "non-ready state")):
        return "KubePodNotReady"
    if any(x in lower for x in ("kubedeploymentreplicasmismatch", "replicas mismatch", "expected number of replicas")):
        return "KubeDeploymentReplicasMismatch"
    if any(x in lower for x in ("kubepodcrashlooping", "container waiting", "kubecontainerwaiting")):
        return "KubeContainerWaiting"
    if any(x in lower for x in ("targetdown", "target disappeared from prometheus target discovery")):
        return "TargetDown"
    if any(x in lower for x in ("nodeclocknotsynchronising", "clock not synchronising", "time sync")):
        return "NodeClockNotSynchronising"
    if any(x in lower for x in ("imagepullbackoff", "errimagepull", "image pull")):
        return "ImagePullError"
    if any(x in lower for x in ("nodenotready", "node not ready", "node is not ready", "not ready")):
        return "NodeNotReady"
    if any(x in lower for x in ("failedmount", "mountvolume", "failedattachvolume", "volumefailed")):
        return "VolumeMountFailed"
    if any(x in lower for x in ("configmap", "secret \"", "not registered", "non-existent config")):
        return "ConfigMissing"
    if any(x in lower for x in ("failedscheduling", "failed scheduling", "pending", "unschedulable", "insufficient cpu", "insufficient memory", "failedcreate")):
        return "SchedulingFailed"
    if any(x in lower for x in ("highcpu", "high cpu", "cpu throttle", "cpu throttl", "cpu usage", "container_cpu", "node_cpu", "cpu使用", " cpu ")):
        return "HighCPUUsage"
    if any(x in lower for x in ("highmemory", "high memory", "memory usage", "memory pressure", "container_memory", "node_memory", "内存")):
        return "HighMemoryUsage"
    if any(x in lower for x in ("readiness probe", "liveness probe", "probe failed", "unhealthy")):
        return "DeploymentUnhealthy"
    if any(x in lower for x in ("networkpolicy", "network policy", "network_deny")):
        return "NetworkPolicyDeny"
    if any(x in lower for x in ("resource_exhausted", "memory_pressure", "cpu_pressure")):
        return "ResourceExhausted"
    if any(x in lower for x in ("log error spike", "error spike", "log_spike")):
        return "LogErrorSpike"
    if any(x in lower for x in ("metric", "prometheus", "anomaly", "指标")):
        return "MetricAnomaly"
    if any(x in lower for x in ("serviceunavailable", "service unavailable", "5xx")):
        return "ServiceUnavailable"
    if any(x in lower for x in ("latency", "slow", "timeout", "connection refused")):
        return "HighLatency"
    return raw or "UnknownFault"


def _is_safe_k8s_name(value: Any) -> bool:
    return isinstance(value, str) and bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}$", value.strip()))


def _extract_k8s_name(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("pod", "node", "deployment", "object", "root_cause_component", "name"):
            found = _extract_k8s_name(value.get(key))
            if found:
                return found
        return ""
    text = str(value).strip()
    typed = re.search(r"\b(?:pod|deployment|deploy|node|service|svc)[/:=\s]+([a-zA-Z0-9][a-zA-Z0-9_.-]{0,252})\b", text, re.I)
    if typed and _is_safe_k8s_name(typed.group(1)):
        return typed.group(1)
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = re.sub(r"^(pod|deployment|deploy|node|service|svc)[/:]\s*", "", text, flags=re.I)
    return text if _is_safe_k8s_name(text) else ""


def _guess_deployment_from_pod(pod: str) -> str:
    parts = str(pod or "").split("-")
    if len(parts) >= 3 and re.match(r"^[a-z0-9]{5,12}$", parts[-2]) and re.match(r"^[a-z0-9]{4,10}$", parts[-1]):
        return "-".join(parts[:-2])
    return ""


def _materialize_heal_command(
    command: str,
    *,
    namespace: str,
    pod: str = "",
    deployment: str = "",
    node: str = "",
    service: str = "",
) -> str:
    cmd = str(command or "").strip()
    replacements = {
        "<namespace>": namespace,
        "<ns>": namespace,
        "<dst-ns>": namespace,
        "<pod>": pod,
        "<deployment>": deployment,
        "<name>": deployment or pod,
        "<node>": node,
        "<svc>": service or deployment,
        "<service>": service or deployment,
    }
    for key, value in replacements.items():
        if value:
            cmd = cmd.replace(key, value)
    return cmd


def _has_heal_placeholder(command: Any) -> bool:
    return bool(re.search(r"<[^>\s]+>", str(command or "")))


def _validate_heal_command(command: str) -> Optional[str]:
    cmd = str(command or "").strip()
    if not cmd:
        return "empty command"
    if _has_heal_placeholder(cmd):
        return "command still contains unresolved placeholder"
    if _HEAL_FORBIDDEN.search(cmd):
        return "command contains forbidden destructive pattern"
    if not cmd.startswith("kubectl "):
        return "only kubectl commands are allowed"
    return None


def _sample_materialize_heal_command(command: str) -> str:
    return _materialize_heal_command(
        command,
        namespace="default",
        pod="sample-pod-7d8c9f6b5d-x9q2p",
        deployment="sample-deploy",
        node="sample-node",
        service="sample-service",
    )


def _validate_heal_recipe_library(library: Dict[str, Any]) -> Dict[str, Any]:
    recipes = library.get("recipes") or []
    issues: List[Dict[str, Any]] = []
    seen_fault_types: set[str] = set()
    required = ("fault_type", "category", "risk", "description", "actions")

    for idx, recipe in enumerate(recipes):
        fault_type = str(recipe.get("fault_type") or "").strip()
        for field in required:
            if not recipe.get(field):
                issues.append({"severity": "error", "recipe": fault_type or f"index:{idx}", "field": field, "message": "missing required field"})
        if fault_type in seen_fault_types:
            issues.append({"severity": "error", "recipe": fault_type, "field": "fault_type", "message": "duplicate fault_type"})
        if fault_type:
            seen_fault_types.add(fault_type)

        actions = recipe.get("actions")
        if not isinstance(actions, list) or not actions:
            issues.append({"severity": "error", "recipe": fault_type or f"index:{idx}", "field": "actions", "message": "actions must be a non-empty list"})
            continue
        for action_idx, action in enumerate(actions):
            if not isinstance(action, dict):
                issues.append({"severity": "error", "recipe": fault_type, "field": f"actions[{action_idx}]", "message": "action must be an object"})
                continue
            command = str(action.get("command") or "").strip()
            if not action.get("step"):
                issues.append({"severity": "warning", "recipe": fault_type, "field": f"actions[{action_idx}].step", "message": "missing action step"})
            reason = _validate_heal_command(_sample_materialize_heal_command(command))
            if reason:
                issues.append({"severity": "error", "recipe": fault_type, "field": f"actions[{action_idx}].command", "message": reason, "command": command})
            rollback = str(action.get("rollback_command") or "").strip()
            if rollback:
                rollback_reason = _validate_heal_command(_sample_materialize_heal_command(rollback))
                if rollback_reason:
                    issues.append({"severity": "error", "recipe": fault_type, "field": f"actions[{action_idx}].rollback_command", "message": rollback_reason, "command": rollback})

    error_count = sum(1 for item in issues if item.get("severity") == "error")
    warning_count = sum(1 for item in issues if item.get("severity") == "warning")
    return {
        "ok": error_count == 0,
        "source": library.get("source"),
        "version": library.get("version"),
        "total": len(recipes),
        "validated_fault_types": len(seen_fault_types),
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }


def _build_heal_verification_plan(namespace: str, target: Dict[str, Any], commands: List[str]) -> Dict[str, Any]:
    target = target if isinstance(target, dict) else {}
    deployment = _extract_k8s_name(target.get("deployment"))
    pod = _extract_k8s_name(target.get("pod"))
    service = _extract_k8s_name(target.get("service"))
    node = _extract_k8s_name(target.get("node"))
    commands_text = "\n".join(commands or [])
    checks: List[Dict[str, str]] = []

    if deployment or re.search(r"\bdeployment/", commands_text):
        if not deployment:
            match = re.search(r"\bdeployment/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,252})", commands_text)
            deployment = _extract_k8s_name(match.group(1) if match else "")
        checks.extend([
            {"type": "deployment_rollout", "command": f"kubectl rollout status deployment/{deployment or '<deployment>'} -n {namespace} --timeout=60s"},
            {"type": "deployment_status", "command": f"kubectl get deployment {deployment or '<deployment>'} -n {namespace} -o json"},
        ])
        return {"mode": "deployment", "target": {"namespace": namespace, "deployment": deployment}, "checks": checks}
    if pod:
        checks.append({"type": "pod_status", "command": f"kubectl get pod {pod} -n {namespace} -o json"})
        if any(" delete pod " in f" {cmd} " for cmd in commands or []):
            checks.append({"type": "namespace_pods_after_delete", "command": f"kubectl get pods -n {namespace} --no-headers"})
        return {"mode": "pod", "target": {"namespace": namespace, "pod": pod}, "checks": checks}
    if service:
        checks.append({"type": "service_endpoints", "command": f"kubectl get endpoints {service} -n {namespace} -o json"})
        return {"mode": "service", "target": {"namespace": namespace, "service": service}, "checks": checks}
    if node:
        checks.append({"type": "node_status", "command": f"kubectl get node {node} -o json"})
        return {"mode": "node", "target": {"node": node}, "checks": checks}
    checks.append({"type": "namespace_pods", "command": f"kubectl get pods -n {namespace} --no-headers"})
    return {"mode": "namespace", "target": {"namespace": namespace}, "checks": checks}


def _compact_heal_text(value: Any, limit: int = 1200) -> str:
    """Convert alert/RCA fragments to bounded text for deterministic matching."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:limit]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return str(value)[:limit]


def _extract_alert_object(alert: Dict[str, Any]) -> Dict[str, str]:
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    involved = raw.get("involvedObject") if isinstance(raw.get("involvedObject"), dict) else {}
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    kind = str(involved.get("kind") or labels.get("kind") or "").lower()
    name = involved.get("name") or alert.get("service") or labels.get("pod") or labels.get("deployment") or labels.get("node") or ""
    obj = {"pod": "", "deployment": "", "node": ""}
    if kind == "pod":
        obj["pod"] = _extract_k8s_name(name)
    elif kind in ("deployment", "replicaset", "statefulset", "daemonset"):
        obj["deployment"] = _extract_k8s_name(name)
    elif kind == "node":
        obj["node"] = _extract_k8s_name(name)
    service = _extract_k8s_name(alert.get("service") or "")
    title_name = _extract_k8s_name(alert.get("title") or "")
    if not obj["pod"] and service:
        obj["pod"] = service
    if not obj["pod"] and title_name and any(x in str(alert.get("title", "")).lower() for x in ("pod", "unhealthy pod")):
        obj["pod"] = title_name
    if not obj["node"] and (labels.get("node") or labels.get("instance")):
        obj["node"] = _extract_k8s_name(labels.get("node") or labels.get("instance"))
    return obj


def _heal_capability(
    *,
    recipe: Optional[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    blocked: List[Dict[str, Any]],
) -> Dict[str, Any]:
    cfg = _get_config()
    risks = [str(s.get("risk", "low")).lower() for s in suggestions + blocked]
    high_risk = "high" in risks
    return {
        "available": bool(suggestions),
        "mode": "dry_run" if getattr(cfg.remediation, "dry_run", True) else "execution_ready",
        "execution_enabled": bool(cfg.remediation.enabled),
        "requires_approval": bool((recipe or {}).get("requires_approval", True) or cfg.remediation.require_approval),
        "requires_risk_ack": bool(high_risk and getattr(cfg.remediation, "require_confirmation_for_high_risk", True)),
        "supports_rollback": any(bool(s.get("rollback_command")) for s in suggestions),
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "policy": "recommend_only" if getattr(cfg.remediation, "recommend_only", False) else "guarded_execute",
    }


def _heal_risk_impact(risk: str, step: str) -> str:
    if risk == "high":
        return "高风险动作，可能导致服务短暂中断或节点工作负载迁移，需审批和风险确认。"
    if risk == "medium":
        return "中风险动作，可能触发 Pod 重建、滚动发布或副本调整。"
    return "低风险动作，主要用于只读确认或轻量恢复。"


def _public_heal_recipe(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """Expose recipes in an agenticSnail-compatible shape while keeping actions."""
    actions = list(recipe.get("actions") or recipe.get("example_commands") or [])
    tier = recipe.get("tier")
    if not tier:
        tier = "diagnostic" if all(str(a.get("risk", "low")).lower() == "low" for a in actions) else "kubectl"
    return {
        **recipe,
        "tier": tier,
        "coverage": recipe.get("coverage") or "tested",
        "how_fixed": recipe.get("how_fixed") or recipe.get("description") or "",
        "actions": actions,
        "example_commands": actions,
    }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _diagnosis_evidence_count(diagnosis: Dict[str, Any]) -> int:
    evidence = diagnosis.get("evidence_summary")
    if isinstance(evidence, dict):
        return sum(1 for v in evidence.values() if v)
    if isinstance(evidence, list):
        return sum(1 for v in evidence if v)
    return 0


def _build_diagnosis_gate(diagnosis: Dict[str, Any], cfg: Any) -> Dict[str, Any]:
    threshold = _to_float(getattr(cfg.remediation, "confidence_threshold", 0.85), 0.85)
    confidence = _to_float(diagnosis.get("confidence"), 0.0) if diagnosis else 0.0
    root_cause = str((diagnosis or {}).get("root_cause") or "").strip()
    fault_type = str((diagnosis or {}).get("fault_type") or "").strip()
    evidence_count = _diagnosis_evidence_count(diagnosis or {})
    blockers = []
    if not diagnosis:
        blockers.append("missing_diagnosis")
    if diagnosis and confidence < threshold:
        blockers.append("low_confidence")
    if diagnosis and not root_cause:
        blockers.append("missing_root_cause")
    if diagnosis and not fault_type:
        blockers.append("missing_fault_type")
    return {
        "required_for_real_execution": True,
        "ready": not blockers,
        "blockers": blockers,
        "confidence": confidence,
        "confidence_threshold": threshold,
        "root_cause": root_cause,
        "fault_type": fault_type,
        "evidence_count": evidence_count,
    }


def _score_heal_action(action: Dict[str, Any], context_text: str) -> Dict[str, Any]:
    text = str(context_text or "").lower()
    step = str(action.get("step") or "").lower()
    command = str(action.get("command") or "").lower()
    joined = f"{step} {command}"
    score = 0
    reasons: List[str] = []
    if action.get("source") == "kb":
        score += 20
        reasons.append("matched external heal recipe")
    elif action.get("source") == "diagnosis_dynamic":
        score += 16
        reasons.append("generated from diagnosis context")
    else:
        score += 10
        reasons.append("matched deterministic guardrail")
    if any(x in text for x in ("scaled to zero", "scale to zero", "replicas=0", "0 replicas", "no ready endpoints", "no endpoints")) and "scale deployment" in joined and "--replicas=1" in joined:
        score += 40
        reasons.append("RCA indicates scale-to-zero/no-endpoints recovery")
    if "no endpoints" in text and "endpoints" in joined:
        score += 12
        reasons.append("checks service endpoints mentioned by diagnosis")
    if "deployment" in text and "deployment" in joined:
        score += 8
        reasons.append("targets deployment identified by diagnosis")
    if "rollback" in step or "rollout undo" in command:
        score -= 3
        reasons.append("rollback action kept behind direct recovery checks")
    ranked = dict(action)
    ranked["selection_score"] = score
    ranked["selection_reason"] = "; ".join(reasons)
    return ranked


def _append_heal_action(
    suggestions: List[Dict[str, Any]],
    *,
    step: str,
    command: str,
    risk: str = "medium",
    rollback_command: str = "",
    source: str = "deterministic",
    impact: str = "",
    selection_reason: str = "",
) -> None:
    if not command:
        return
    suggestions.append({
        "step": step,
        "command": command,
        "rollback_command": rollback_command,
        "risk": risk,
        "impact": impact or _heal_risk_impact(risk, step),
        "source": source,
        "selection_reason": selection_reason or f"{source} action matched {step}",
    })


def _build_dynamic_heal_actions(
    fault_type: str,
    *,
    namespace: str,
    pod: str = "",
    deployment: str = "",
    node: str = "",
    service: str = "",
    diagnosis_text: str = "",
) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    deploy = deployment or _guess_deployment_from_pod(pod)
    svc = service or deploy
    diag_lower = str(diagnosis_text or "").lower()

    if pod:
        _append_heal_action(
            suggestions,
            step=f"查看 Pod {pod} 的事件和状态",
            command=f"kubectl describe pod {pod} -n {namespace}",
            risk="low",
        )

    if fault_type in {"PodCrashLoop", "KubeContainerWaiting"}:
        if pod:
            _append_heal_action(suggestions, step=f"采集 {pod} 上一次容器日志", command=f"kubectl logs {pod} -n {namespace} --tail=200 --previous", risk="low")
            _append_heal_action(suggestions, step=f"删除异常 Pod {pod} 触发控制器重建", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"回滚 Deployment {deploy} 到上一版本", command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type == "OOMKilled":
        if pod:
            _append_heal_action(suggestions, step=f"确认 {pod} OOMKilled 证据", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"删除 OOM Pod {pod} 触发重建", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            _append_heal_action(suggestions, step=f"提高 Deployment {deploy} 内存限制", command=f"kubectl set resources deployment/{deploy} -n {namespace} --requests=memory=512Mi --limits=1Gi", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy} 使资源配置生效", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type in {"HighCPUUsage", "HighMemoryUsage", "MetricAnomaly"}:
        sort_key = "memory" if fault_type == "HighMemoryUsage" else "cpu"
        _append_heal_action(suggestions, step=f"定位 {namespace} 中资源热点 Pod", command=f"kubectl top pod -n {namespace} --sort-by={sort_key}", risk="low")
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 资源使用", command=f"kubectl top pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"重建资源异常 Pod {pod}", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            if fault_type == "HighCPUUsage":
                _append_heal_action(suggestions, step=f"提高 Deployment {deploy} CPU limit", command=f"kubectl set resources deployment/{deploy} -n {namespace} --requests=cpu=500m --limits=1500m", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            if fault_type == "HighMemoryUsage":
                _append_heal_action(suggestions, step=f"提高 Deployment {deploy} memory limit", command=f"kubectl set resources deployment/{deploy} -n {namespace} --requests=memory=512Mi --limits=2Gi", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"临时扩容 Deployment {deploy}", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=2", risk="medium")

    elif fault_type in {"DeploymentUnhealthy", "KubePodNotReady", "ServiceUnavailable", "HighLatency"}:
        if svc:
            _append_heal_action(suggestions, step=f"检查 Service/Endpoints {svc}", command=f"kubectl get endpoints {svc} -n {namespace} -o wide", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"检查 Deployment {deploy} 发布状态", command=f"kubectl rollout status deployment/{deploy} -n {namespace}", risk="low")
            if any(x in diag_lower for x in ("scaled to zero", "scale to zero", "replicas=0", "0 replicas", "no ready endpoints", "no endpoints")):
                _append_heal_action(suggestions, step=f"恢复 Deployment {deploy} 至 1 个副本", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=1", rollback_command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=0", risk="medium")
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"回滚 Deployment {deploy}", command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
        elif pod:
            _append_heal_action(suggestions, step=f"删除异常 Pod {pod} 触发重建", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        else:
            _append_heal_action(suggestions, step="检查命名空间服务端点和最近事件", command=f"kubectl get endpoints -n {namespace} -o wide", risk="low")
            _append_heal_action(suggestions, step="检查命名空间 Deployment 状态", command=f"kubectl get deploy -n {namespace} -o wide", risk="low")

    elif fault_type == "ImagePullError":
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 镜像拉取事件", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"删除镜像拉取失败 Pod {pod} 重新拉取", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=0 --force", risk="high")
        if deploy:
            _append_heal_action(suggestions, step=f"回滚 Deployment {deploy} 到上一镜像版本", command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type in {"SchedulingFailed", "PendingScheduling", "ResourceQuotaExceeded", "ResourceExhausted"}:
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 调度失败原因", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
        _append_heal_action(suggestions, step="查看节点资源水位", command="kubectl top node", risk="low")
        _append_heal_action(suggestions, step=f"查看 {namespace} 配额状态", command=f"kubectl describe resourcequota -n {namespace}", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"临时扩容 Deployment {deploy} 保持可用副本", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=2", risk="medium")

    elif fault_type in {"ConfigMissing", "ConfigKeyMissing", "VolumeMountFailed"}:
        if pod:
            _append_heal_action(suggestions, step=f"定位 {pod} 配置/挂载失败原因", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"删除挂载异常 Pod {pod} 触发重新挂载", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy} 重新加载配置", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type in {"NodeNotReady", "KubeletUnhealthy", "DiskPressure", "NodeUnschedulable"}:
        if node:
            _append_heal_action(suggestions, step=f"查看节点 {node} Conditions 与事件", command=f"kubectl describe node {node}", risk="low")
            if fault_type != "NodeUnschedulable":
                _append_heal_action(suggestions, step=f"隔离节点 {node}", command=f"kubectl cordon {node}", rollback_command=f"kubectl uncordon {node}", risk="medium")
                _append_heal_action(suggestions, step=f"驱逐节点 {node} 上工作负载", command=f"kubectl drain {node} --ignore-daemonsets --delete-emptydir-data --force --timeout=120s", rollback_command=f"kubectl uncordon {node}", risk="high")
            _append_heal_action(suggestions, step=f"恢复节点 {node} 调度", command=f"kubectl uncordon {node}", risk="medium")
        else:
            _append_heal_action(suggestions, step="查看所有节点状态", command="kubectl get nodes -o wide", risk="low")

    elif fault_type == "DNSResolution":
        _append_heal_action(suggestions, step="检查 kube-dns/CoreDNS Endpoints", command="kubectl get endpoints kube-dns -n kube-system -o wide", risk="low")
        _append_heal_action(suggestions, step="滚动重启 CoreDNS", command="kubectl rollout restart deployment/coredns -n kube-system", risk="medium")
        _append_heal_action(suggestions, step="查看 CoreDNS Pod 状态", command="kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide", risk="low")

    elif fault_type == "KubeProxyUnhealthy":
        _append_heal_action(suggestions, step="查看 kube-proxy Pod 状态", command="kubectl get pods -n kube-system -l k8s-app=kube-proxy -o wide", risk="low")
        _append_heal_action(suggestions, step="重启 kube-proxy DaemonSet", command="kubectl rollout restart ds/kube-proxy -n kube-system", risk="medium")

    elif fault_type == "ApiServerUnhealthy":
        _append_heal_action(suggestions, step="查看 API Server 静态 Pod 状态", command="kubectl get pods -n kube-system -l component=kube-apiserver -o wide", risk="low")
        _append_heal_action(suggestions, step="删除异常 API Server Pod 触发 kubelet 重建", command="kubectl delete pod -n kube-system -l component=kube-apiserver", risk="high")

    elif fault_type == "EtcdUnhealthy":
        _append_heal_action(suggestions, step="查看 etcd 静态 Pod 状态", command="kubectl get pods -n kube-system -l component=etcd -o wide", risk="low")
        _append_heal_action(suggestions, step="重建异常 etcd Pod", command="kubectl delete pod -n kube-system -l component=etcd", risk="high")

    elif fault_type == "NetworkPolicyDeny":
        _append_heal_action(suggestions, step=f"查看 {namespace} NetworkPolicy", command=f"kubectl get networkpolicy -n {namespace}", risk="low")
        _append_heal_action(suggestions, step=f"查看 {namespace} NetworkPolicy 详情", command=f"kubectl describe networkpolicy -n {namespace}", risk="low")

    elif fault_type == "HPAScalingFailed":
        _append_heal_action(suggestions, step=f"查看 {namespace} HPA 状态", command=f"kubectl get hpa -n {namespace} -o wide", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"检查 HPA {deploy}", command=f"kubectl describe hpa {deploy} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"临时固定 Deployment {deploy} 副本", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=1", risk="medium")

    elif fault_type in {"TargetDown", "LogErrorSpike"}:
        _append_heal_action(suggestions, step=f"检查 {namespace} Endpoints", command=f"kubectl get endpoints -n {namespace} -o wide", risk="low")
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 错误日志", command=f"kubectl logs {pod} -n {namespace} --tail=500", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    return suggestions


def _build_heal_suggestions(body: Dict[str, Any]) -> Dict[str, Any]:
    alert = body.get("alert") if isinstance(body.get("alert"), dict) else {}
    diagnosis = body.get("diagnosis") if isinstance(body.get("diagnosis"), dict) else {}
    cfg = _get_config()
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    involved = raw.get("involvedObject") if isinstance(raw.get("involvedObject"), dict) else {}
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    namespace = body.get("namespace") or alert.get("namespace") or raw.get("metadata", {}).get("namespace") or cfg.kubernetes.namespace or "default"
    recipes = _load_heal_recipes()
    known_fault_types = {str(r.get("fault_type")) for r in recipes if r.get("fault_type")}
    explicit_fault_type = str(body.get("fault_type") or diagnosis.get("fault_type") or "").strip()
    diagnosis_evidence = diagnosis.get("evidence_summary") if isinstance(diagnosis.get("evidence_summary"), dict) else {}
    diagnosis_services = diagnosis.get("affected_services") if isinstance(diagnosis.get("affected_services"), list) else []
    text = " ".join(filter(None, [
        str(body.get("fault_type") or diagnosis.get("fault_type") or ""),
        str(body.get("root_cause") or diagnosis.get("root_cause") or ""),
        str(body.get("root_cause_component") or diagnosis.get("root_cause_component") or ""),
        str(body.get("remediation_hint") or diagnosis.get("remediation_suggestion") or ""),
        str(body.get("message", "")),
        str(body.get("object", "")),
        " ".join(map(str, diagnosis_services)),
        " ".join(map(str, diagnosis_evidence.values())),
        " ".join(map(str, body.get("evidence", []) or [])),
        str(alert.get("source", "")),
        str(alert.get("severity", "")),
        str(alert.get("title", "")),
        str(alert.get("description", "")),
        str(alert.get("service", "")),
        str(raw.get("reason", "")),
        str(raw.get("message", "")),
        str(involved.get("kind", "")),
        str(involved.get("name", "")),
        " ".join(str(labels.get(k, "")) for k in ("alertname", "reason", "pod", "deployment", "node", "instance")),
    ]))
    fault_type = explicit_fault_type if explicit_fault_type in known_fault_types else _normalize_heal_fault_type(text)
    alert_object = _extract_alert_object(alert)
    primary_diagnosis_service = next((str(s) for s in diagnosis_services if s), "")
    involved_kind = str(involved.get("kind") or "").lower()
    alert_pod = alert_object.get("pod") if involved_kind == "pod" or str(alert.get("source") or "") == "pod_health" else ""
    pod = _extract_k8s_name(body.get("pod") or alert_pod or "")
    component = _extract_k8s_name(diagnosis.get("root_cause_component") or body.get("root_cause_component") or "")
    deployment = _extract_k8s_name(body.get("deployment") or alert_object.get("deployment") or component or primary_diagnosis_service or "") or _guess_deployment_from_pod(pod)
    node = _extract_k8s_name(body.get("node") or alert_object.get("node") or body.get("instance") or diagnosis.get("node") or "")
    service = _extract_k8s_name(body.get("service") or body.get("svc") or alert.get("service") or body.get("object") or primary_diagnosis_service or component or "")
    recipe = next((r for r in recipes if r.get("fault_type") == fault_type), None)
    suggestions: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    seen_commands: set[str] = set()
    candidate_actions: List[Dict[str, Any]] = []
    plan_sources: set[str] = set()

    for action in _build_dynamic_heal_actions(
        fault_type,
        namespace=namespace,
        pod=pod,
        deployment=deployment,
        node=node,
        service=service,
        diagnosis_text=text,
    ):
        action["source"] = action.get("source") or "diagnosis_dynamic"
        action["selection_reason"] = action.get("selection_reason") or "Generated from RCA target and fault type"
        candidate_actions.append(action)

    if recipe:
        for action in recipe.get("actions", []):
            cmd = _materialize_heal_command(
                action.get("command", ""),
                namespace=namespace,
                pod=pod,
                deployment=deployment,
                node=node,
                service=service,
            )
            rollback = _materialize_heal_command(
                action.get("rollback_command", ""),
                namespace=namespace,
                pod=pod,
                deployment=deployment,
                node=node,
                service=service,
            )
            item = {
                "step": action.get("step"),
                "command": cmd,
                "rollback_command": rollback,
                "risk": action.get("risk") or recipe.get("risk", "medium"),
                "source": "kb",
                "selection_reason": f"Retrieved from heal recipe {fault_type}",
            }
            candidate_actions.append(item)

    for action in sorted(
        (_score_heal_action(item, text) for item in candidate_actions),
        key=lambda x: x.get("selection_score", 0),
        reverse=True,
    ):
        cmd_key = action.get("command", "")
        if cmd_key and cmd_key not in seen_commands:
            seen_commands.add(cmd_key)
            plan_sources.add(str(action.get("source") or "unknown"))
            suggestions.append(action)

    executable: List[Dict[str, Any]] = []
    for item in suggestions:
        reason = _validate_heal_command(item.get("command", ""))
        if reason:
            item["executable"] = False
            item["blocked_reason"] = reason
            blocked.append(item)
        else:
            item["executable"] = True
            executable.append(item)
    suggestions = executable

    recipe_public = None
    if recipe:
        recipe_public = {
            "fault_type": recipe.get("fault_type"),
            "category": recipe.get("category"),
            "risk": recipe.get("risk"),
            "requires_approval": recipe.get("requires_approval"),
            "description": recipe.get("description"),
            "signals": recipe.get("signals", []),
        }
    diagnosis_gate = _build_diagnosis_gate(diagnosis, cfg)
    capability = _heal_capability(recipe=recipe, suggestions=suggestions, blocked=blocked)
    capability["diagnosis_ready"] = bool(diagnosis_gate.get("ready"))
    capability["diagnosis_required_for_real_execution"] = True

    return {
        "fault_type": fault_type,
        "namespace": namespace,
        "target": {"pod": pod, "deployment": deployment, "node": node, "service": service},
        "recipe": recipe_public,
        "suggestions": suggestions,
        "blocked_templates": blocked,
        "capability": capability,
        "diagnosis_gate": diagnosis_gate,
        "verification_plan": _build_heal_verification_plan(
            namespace,
            {"pod": pod, "deployment": deployment, "node": node, "service": service},
            [s.get("command", "") for s in suggestions],
        ),
        "recommended_strategy": "kb" if suggestions else "none",
        "plan_source": "+".join(sorted(plan_sources)) if plan_sources else "none",
        "strategy_trace": {
            "recipe_matched": bool(recipe),
            "candidate_count": len(candidate_actions),
            "selected_count": len(suggestions),
            "sources": sorted(plan_sources),
            "ranking": [
                {
                    "source": item.get("source"),
                    "step": item.get("step"),
                    "command": item.get("command"),
                    "score": item.get("selection_score"),
                    "reason": item.get("selection_reason"),
                }
                for item in suggestions[:12]
            ],
        },
        "diagnosis_mode": "rca_result" if diagnosis else "alert_only",
        "diagnosis_used": {
            "fault_type": diagnosis.get("fault_type"),
            "root_cause": diagnosis.get("root_cause"),
            "root_cause_component": diagnosis.get("root_cause_component"),
            "remediation_suggestion": diagnosis.get("remediation_suggestion"),
            "affected_services": diagnosis_services,
            "confidence": diagnosis.get("confidence"),
            "evidence_count": diagnosis_gate.get("evidence_count", 0),
        } if diagnosis else {},
        "diagnosis_summary": f"故障类型: {fault_type} | 根因: {diagnosis.get('root_cause') or body.get('root_cause') or '-'} | namespace: {namespace} | pod: {pod or '-'} | deployment: {deployment or '-'} | node: {node or '-'}",
    }


def _require_fault_execution_approval(
    *,
    body: Dict[str, Any],
    target_mode: str,
    action: str,
    fault_type: str,
) -> None:
    """Enforce explicit confirmations before any real fault action."""
    if bool(body.get("dry_run", True)):
        return
    if not bool(body.get("confirm")):
        raise HTTPException(
            400,
            "Real fault execution requires confirm=true. Re-run as dry_run first and review the command.",
        )
    if target_mode == "host" and not bool(body.get("host_ack")):
        raise HTTPException(
            400,
            "Bare-metal host execution requires host_ack=true because it modifies the target GPU host.",
        )
    if fault_type in _HIGH_RISK_LLM_FAULT_TYPES and not bool(body.get("risk_ack")):
        raise HTTPException(
            400,
            f"Fault type '{fault_type}' is high risk and requires risk_ack=true.",
        )
    if action == "experiment" and not bool(body.get("experiment_ack")):
        raise HTTPException(
            400,
            "Real experiment execution requires experiment_ack=true because it injects and recovers faults.",
        )


def _load_fault_targets():
    """Load persisted fault-target overrides on startup (replaces yaml defaults)."""
    if not _FAULT_TARGETS_FILE.exists():
        return
    try:
        data = json.loads(_FAULT_TARGETS_FILE.read_text(encoding="utf-8"))
        cfg = _get_config()
        if isinstance(data.get("k8s_clusters"), list):
            cfg.fault_targets.k8s_clusters = list(data["k8s_clusters"])
        if isinstance(data.get("llm_hosts"), list):
            cfg.fault_targets.llm_hosts = list(data["llm_hosts"])
        logger.info("Loaded fault targets from disk")
    except Exception as e:
        logger.warning(f"Failed to load fault targets: {e}")


def _find_k8s_cluster(cluster_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up a K8s cluster profile by id. Returns None if not found."""
    if not cluster_id:
        return None
    cfg = _get_config()
    for c in cfg.fault_targets.k8s_clusters:
        if isinstance(c, dict) and c.get("id") == cluster_id:
            return c
    return None


def _find_llm_host(host_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up an LLM GPU host profile by id. Returns None if not found."""
    if not host_id:
        return None
    cfg = _get_config()
    for h in cfg.fault_targets.llm_hosts:
        if isinstance(h, dict) and h.get("id") == host_id:
            return h
    return None


def _get_config():
    if _state["config"] is None:
        _state["config"] = get_config()
    return _state["config"]


def _get_pipeline():
    if _state["pipeline"] is None:
        _state["pipeline"] = Pipeline(_get_config())
    return _state["pipeline"]


# ── Kubectl Helpers ──

def _kubectl_sync(cmd: str, namespace: str = "") -> str:
    """Execute kubectl command via SSH jump host (synchronous)."""
    import subprocess
    cfg = _get_config()
    ns_flag = f"-n {namespace}" if namespace else ""

    if cfg.kubernetes.use_ssh and cfg.kubernetes.ssh_jump_host:
        ssh_target = cfg.kubernetes.ssh_target or cfg.kubernetes.target_host
        ssh_cmd = f"ssh -J {cfg.kubernetes.ssh_jump_host} {ssh_target} 'kubectl {cmd} {ns_flag}'"
    else:
        ssh_cmd = f"kubectl {cmd} {ns_flag}"

    try:
        result = subprocess.run(
            ssh_cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "Error: command timed out"
    except Exception as e:
        return f"Error: {e}"


async def _kubectl(cmd: str, namespace: str = "") -> str:
    """Execute kubectl command without blocking the event loop."""
    return await asyncio.to_thread(_kubectl_sync, cmd, namespace)


async def _kubectl_json(cmd: str, namespace: str = "") -> Any:
    """Execute kubectl -o json and parse without blocking."""
    raw = await _kubectl(f"{cmd} -o json", namespace)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": raw}


def _apply_cluster_to_cmd(cmd: str, cluster_id: Optional[str]) -> str:
    """Inject KUBECONFIG env and --context flag into kubectl commands for a specific cluster."""
    if not cluster_id:
        return cmd
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        return cmd
    ctx = cluster.get("context") or ""
    kubeconfig = cluster.get("kubeconfig") or ""
    new_cmd = cmd
    if ctx:
        new_cmd = re.sub(
            r'\bkubectl\b(?!\s+--context)',
            f'kubectl --context={shlex.quote(ctx)}',
            new_cmd,
        )
    if kubeconfig:
        new_cmd = f"KUBECONFIG={shlex.quote(kubeconfig)} {new_cmd}"
    return new_cmd


def _shell_sync(cmd: str, timeout: int = 90, cluster_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute a bounded shell command locally or through the configured jump host.

    When cluster_id is provided, kubectl invocations are rewritten to target that
    cluster profile (KUBECONFIG + --context flags).
    """
    cfg = _get_config()
    effective_cmd = _apply_cluster_to_cmd(cmd, cluster_id)
    shell_cmd = effective_cmd
    if cfg.kubernetes.use_ssh and cfg.kubernetes.ssh_jump_host:
        ssh_target = cfg.kubernetes.ssh_target or cfg.kubernetes.target_host
        escaped = effective_cmd.replace("'", "'\"'\"'")
        shell_cmd = f"ssh -J {cfg.kubernetes.ssh_jump_host} {ssh_target} '{escaped}'"

    try:
        result = subprocess.run(
            shell_cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return {
            "command": effective_cmd,
            "returncode": result.returncode,
            "stdout": result.stdout.strip()[-4000:],
            "stderr": result.stderr.strip()[-4000:],
            "ok": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"command": effective_cmd, "returncode": 124, "stdout": "", "stderr": "timeout", "ok": False}
    except Exception as e:
        return {"command": effective_cmd, "returncode": 1, "stdout": "", "stderr": str(e), "ok": False}


def _kubectl_heal_check(
    args: str,
    *,
    cluster_id: Optional[str],
    timeout: int = 45,
    parse_json: bool = False,
) -> Dict[str, Any]:
    cmd = f"kubectl {args}"
    result = _shell_sync(cmd, timeout, cluster_id)
    check = {
        "command": result.get("command") or cmd,
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
    }
    if parse_json and check["ok"]:
        try:
            check["json"] = json.loads(check["stdout"] or "{}")
        except Exception as e:
            check["ok"] = False
            check["parse_error"] = str(e)
    return check


def _deployment_available(obj: Dict[str, Any]) -> bool:
    spec = obj.get("spec") if isinstance(obj, dict) else {}
    status = obj.get("status") if isinstance(obj, dict) else {}
    desired = int(spec.get("replicas") or 1)
    available = int(status.get("availableReplicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    unavailable = int(status.get("unavailableReplicas") or 0)
    conditions = [c for c in status.get("conditions", []) or [] if isinstance(c, dict)]
    condition_ok = not conditions or any(c.get("type") == "Available" and c.get("status") == "True" for c in conditions)
    return available >= desired and ready >= desired and unavailable == 0 and condition_ok


def _pod_ready(obj: Dict[str, Any]) -> bool:
    status = obj.get("status") if isinstance(obj, dict) else {}
    phase = status.get("phase")
    if phase == "Succeeded":
        return True
    if phase != "Running":
        return False
    statuses = status.get("containerStatuses") or []
    return bool(statuses) and all(bool(c.get("ready")) for c in statuses if isinstance(c, dict))


def _node_ready(obj: Dict[str, Any]) -> bool:
    conditions = ((obj.get("status") or {}).get("conditions") or []) if isinstance(obj, dict) else []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions if isinstance(c, dict))


def _endpoints_ready(obj: Dict[str, Any]) -> bool:
    subsets = obj.get("subsets") if isinstance(obj, dict) else []
    return any((s.get("addresses") or []) for s in subsets or [] if isinstance(s, dict))


def _namespace_health_from_pods(stdout: str) -> Dict[str, Any]:
    bad_states = ("CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull", "CreateContainer", "Pending", "Failed")
    rows = [line for line in (stdout or "").splitlines() if line.strip()]
    bad_rows = [line for line in rows if any(state in line for state in bad_states)]
    return {"total_pods_seen": len(rows), "unhealthy_rows": bad_rows[:20], "healthy": not bad_rows}


def _verify_heal_recovery(
    *,
    namespace: str,
    suggestion: Dict[str, Any],
    commands: List[str],
    cluster_id: Optional[str],
    dry_run: bool,
    executed_success: bool,
) -> Dict[str, Any]:
    """Best-effort post-action recovery verification for self-healing runs."""
    if dry_run:
        return {"status": "skipped", "reason": "dry_run", "recovered": None, "checks": []}
    if not executed_success:
        return {"status": "skipped", "reason": "execution_failed", "recovered": False, "checks": []}
    if not _is_safe_k8s_name(namespace):
        return {"status": "unknown", "reason": "unsafe_namespace", "recovered": None, "checks": []}

    target = suggestion.get("target") if isinstance(suggestion.get("target"), dict) else {}
    deployment = _extract_k8s_name(target.get("deployment"))
    pod = _extract_k8s_name(target.get("pod"))
    service = _extract_k8s_name(target.get("service"))
    node = _extract_k8s_name(target.get("node"))
    commands_text = "\n".join(commands)
    checks: List[Dict[str, Any]] = []

    if deployment or re.search(r"\bdeployment/", commands_text):
        if not deployment:
            match = re.search(r"\bdeployment/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,252})", commands_text)
            deployment = _extract_k8s_name(match.group(1) if match else "")
        if deployment:
            rollout = _kubectl_heal_check(
                f"rollout status deployment/{shlex.quote(deployment)} -n {shlex.quote(namespace)} --timeout=60s",
                cluster_id=cluster_id,
                timeout=75,
            )
            checks.append({"type": "deployment_rollout", **rollout})
            dep = _kubectl_heal_check(
                f"get deployment {shlex.quote(deployment)} -n {shlex.quote(namespace)} -o json",
                cluster_id=cluster_id,
                parse_json=True,
            )
            checks.append({"type": "deployment_status", **dep})
            recovered = bool(rollout.get("ok")) and bool(dep.get("ok")) and _deployment_available(dep.get("json") or {})
            return {"status": "verified", "target": {"deployment": deployment, "namespace": namespace}, "recovered": recovered, "checks": checks}

    if pod:
        pod_check = _kubectl_heal_check(
            f"get pod {shlex.quote(pod)} -n {shlex.quote(namespace)} -o json",
            cluster_id=cluster_id,
            parse_json=True,
        )
        checks.append({"type": "pod_status", **pod_check})
        if pod_check.get("ok"):
            recovered = _pod_ready(pod_check.get("json") or {})
            return {"status": "verified", "target": {"pod": pod, "namespace": namespace}, "recovered": recovered, "checks": checks}
        if any(" delete pod " in f" {cmd} " for cmd in commands):
            pods = _kubectl_heal_check(
                f"get pods -n {shlex.quote(namespace)} --no-headers",
                cluster_id=cluster_id,
            )
            checks.append({"type": "namespace_pods_after_delete", **pods})
            health = _namespace_health_from_pods(pods.get("stdout", ""))
            return {
                "status": "verified" if pods.get("ok") else "unknown",
                "target": {"pod": pod, "namespace": namespace},
                "recovered": health["healthy"] if pods.get("ok") else None,
                "checks": checks,
                "summary": health,
            }

    if service:
        endpoints = _kubectl_heal_check(
            f"get endpoints {shlex.quote(service)} -n {shlex.quote(namespace)} -o json",
            cluster_id=cluster_id,
            parse_json=True,
        )
        checks.append({"type": "service_endpoints", **endpoints})
        recovered = bool(endpoints.get("ok")) and _endpoints_ready(endpoints.get("json") or {})
        return {"status": "verified", "target": {"service": service, "namespace": namespace}, "recovered": recovered, "checks": checks}

    if node:
        node_check = _kubectl_heal_check(
            f"get node {shlex.quote(node)} -o json",
            cluster_id=cluster_id,
            parse_json=True,
        )
        checks.append({"type": "node_status", **node_check})
        recovered = bool(node_check.get("ok")) and _node_ready(node_check.get("json") or {})
        return {"status": "verified", "target": {"node": node}, "recovered": recovered, "checks": checks}

    pods = _kubectl_heal_check(
        f"get pods -n {shlex.quote(namespace)} --no-headers",
        cluster_id=cluster_id,
    )
    checks.append({"type": "namespace_pods", **pods})
    health = _namespace_health_from_pods(pods.get("stdout", ""))
    return {
        "status": "verified" if pods.get("ok") else "unknown",
        "target": {"namespace": namespace},
        "recovered": health["healthy"] if pods.get("ok") else None,
        "checks": checks,
        "summary": health,
    }


def _prometheus_api_ok(base_url: str) -> bool:
    """Return True if the URL looks like a working Prometheus API endpoint."""
    if not base_url:
        return False
    import requests as req
    try:
        resp = req.get(
            f"{base_url.rstrip('/')}/api/v1/query",
            params={"query": "up"},
            timeout=4,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        return data.get("status") == "success" and "data" in data
    except Exception:
        return False


def _discover_prometheus_url() -> str:
    """
    Discover a reachable Prometheus URL.

    The validation host is not always a K8S node, so ClusterIP and localhost can
    fail. Prefer configured URL, then discover the Prometheus NodePort and test
    all node InternalIPs.
    """
    cached = _state.get("prometheus_url")
    if cached and _prometheus_api_ok(cached):
        return cached

    cfg = _get_config()
    candidates: List[str] = []
    if getattr(cfg.observability, "prometheus_url", None):
        candidates.append(getattr(cfg.observability, "prometheus_url", "").rstrip("/"))

    try:
        svc_data = json.loads(_kubectl_sync("get svc -A -o json"))
    except Exception:
        svc_data = {}
    try:
        node_data = json.loads(_kubectl_sync("get nodes -o json"))
    except Exception:
        node_data = {}

    nodes: List[str] = []
    for node in node_data.get("items", []):
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP" and addr.get("address"):
                nodes.append(addr["address"])

    for svc in svc_data.get("items", []):
        meta = svc.get("metadata", {})
        spec = svc.get("spec", {})
        ns_name = f"{meta.get('namespace', '')}/{meta.get('name', '')}".lower()
        if "prom" not in ns_name:
            continue
        for port in spec.get("ports", []):
            port_num = port.get("port")
            target_name = str(port.get("name", "")).lower()
            if port_num != 9090 and "web" not in target_name and "prom" not in target_name:
                continue

            cluster_ip = spec.get("clusterIP")
            if cluster_ip and cluster_ip != "None":
                candidates.append(f"http://{cluster_ip}:{port_num}")

            node_port = port.get("nodePort")
            if node_port:
                for node_ip in nodes:
                    candidates.append(f"http://{node_ip}:{node_port}")

    seen = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        if _prometheus_api_ok(url):
            _state["prometheus_url"] = url
            logger.info("Prometheus endpoint selected: %s", url)
            return url

    return ""  # native prometheus removed; MCP backend handles metrics


# ─────────────────────────────────────────
# Page Routes
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")

# ─── AliData / offline-mode admin section removed during MCP cutover ───

# ─────────────────────────────────────────
# Model Interaction APIs (OpsLLM-7B)
# ─────────────────────────────────────────

_state["chat_sessions"] = {}  # session_id → {"messages": [], "created_at": time}


@app.get("/api/model/info")
async def get_model_info():
    """Get current LLM model configuration."""
    cfg = _get_config()
    return {
        "model": cfg.llm.model,
        "base_url": cfg.llm.base_url,
        "configured": bool(cfg.llm.api_key),
        "max_tokens": cfg.llm.max_tokens,
        "temperature": cfg.llm.temperature,
    }


@app.post("/api/model/chat")
async def model_chat(request: Request):
    """Send a message to the LLM model and get a response."""
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")
    stream = body.get("stream", False)

    if not message:
        raise HTTPException(400, "Missing 'message' field")

    cfg = _get_config()
    if not cfg.llm.api_key:
        raise HTTPException(
            503,
            "LLM API Key 未配置。请在 .env 文件中设置 LLM_API_KEY。"
        )

    # Initialize or update session
    if session_id not in _state["chat_sessions"]:
        _state["chat_sessions"][session_id] = {
            "messages": [],
            "created_at": time.time(),
        }

    session = _state["chat_sessions"][session_id]
    session["messages"].append({"role": "user", "content": message})

    system_prompt = {
        "role": "system",
        "content": (
            "你是 AgenticSRE 的资深 SRE 智能运维助手，专注于 Kubernetes、可观测性、"
            "故障诊断、性能分析、告警处理等领域。\n\n"
            "回答规范（必须遵守）：\n"
            "1. 用中文回答，markdown 格式，结构化输出。\n"
            "2. 对每个故障问题，至少包含以下章节：\n"
            "   - ## 可能原因（列出 3-5 条具体原因，每条说明触发条件）\n"
            "   - ## 排查步骤（按优先级列出 5-8 个具体可执行步骤，附 kubectl/curl/相关命令）\n"
            "   - ## 处置建议（短期止血 + 长期优化各 2-3 条）\n"
            "3. 命令必须用 ``` 代码块包裹，关键概念用 ** 加粗。\n"
            "4. 回答要详尽、可执行，避免泛泛而谈。长度通常 500-1000 字。\n"
            "5. 如果用户问题简单（如名词解释），可以简短回答 3-5 句话。"
        )
    }
    llm_messages = [system_prompt] + session["messages"][-20:]

    try:
        llm = LLMClient(cfg.llm)

        if stream:
            async def generate():
                accumulated = []
                try:
                    from openai import OpenAI
                    client = OpenAI(api_key=cfg.llm.api_key or "EMPTY", base_url=cfg.llm.base_url)
                    response = await asyncio.to_thread(
                        lambda: client.chat.completions.create(
                            model=cfg.llm.model,
                            messages=llm_messages,
                            temperature=cfg.llm.temperature,
                            max_tokens=cfg.llm.max_tokens,
                            stream=True,
                        )
                    )
                    loop = asyncio.get_running_loop()
                    def _nxt(it):
                        try: return next(it)
                        except StopIteration: return None
                    it = iter(response)
                    while True:
                        chunk_obj = await loop.run_in_executor(None, _nxt, it)
                        if chunk_obj is None: break
                        if not chunk_obj.choices: continue
                        piece = getattr(chunk_obj.choices[0].delta, "content", None) or ""
                        if piece:
                            accumulated.append(piece)
                            yield f"data: {json.dumps({'type': 'chunk', 'content': piece}, ensure_ascii=False)}\n\n"
                    text = "".join(accumulated)
                    session["messages"].append({"role": "assistant", "content": text})
                    yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")
        else:
            response_text = await asyncio.to_thread(llm.chat, llm_messages)
            session["messages"].append({"role": "assistant", "content": response_text})
            return {
                "response": response_text,
                "session_id": session_id,
                "message_count": len(session["messages"]),
            }
    except Exception as e:
        logger.error(f"Model chat error: {e}", exc_info=True)
        raise HTTPException(500, f"模型调用失败: {str(e)}")


@app.get("/api/model/chat/history/{session_id}")
async def get_chat_history(session_id: str):
    """Get chat history for a session."""
    session = _state["chat_sessions"].get(session_id)
    if not session:
        return {"messages": [], "session_id": session_id}
    return {
        "messages": session["messages"],
        "session_id": session_id,
        "created_at": session["created_at"],
    }


@app.delete("/api/model/chat/history/{session_id}")
async def clear_chat_history(session_id: str):
    """Clear chat history for a session."""
    if session_id in _state["chat_sessions"]:
        _state["chat_sessions"][session_id]["messages"] = []
        return {"status": "cleared", "session_id": session_id}
    return {"status": "not_found", "session_id": session_id}


@app.get("/api/model/chat/sessions")
async def list_chat_sessions():
    """List all chat sessions."""
    sessions = []
    for sid, session in _state["chat_sessions"].items():
        sessions.append({
            "session_id": sid,
            "message_count": len(session["messages"]),
            "created_at": session["created_at"],
        })
    return {"sessions": sessions}


# ─────────────────────────────────────────
# Cluster Info APIs
# ─────────────────────────────────────────

_DASH_CACHE: Dict[str, tuple] = {}
_DASH_CACHE_TTL = 300  # seconds


def _cache_get(key: str):
    rec = _DASH_CACHE.get(key)
    if not rec: return None
    val, expires = rec
    if expires < time.time():
        _DASH_CACHE.pop(key, None)
        return None
    return val


def _cache_put(key: str, val):
    _DASH_CACHE[key] = (val, time.time() + _DASH_CACHE_TTL)
    return val


def _prewarm_caches_async():
    """Background MCP prewarm — entity browses only (cheap, ~3s total).
    Heavy per_entity metric sweeps run on first user request."""
    import threading
    def _warm():
        try:
            logger.info("MCP prewarm: starting")
            _mcp_browse_entities("k8s", "k8s.pod", 500)
            _mcp_browse_entities("k8s", "k8s.node", 100)
            _mcp_browse_entities("apm", "apm.service", 100)
            logger.info("MCP prewarm: complete (entity browses cached)")
        except Exception as exc:
            logger.warning("MCP prewarm failed: %s", exc)
    threading.Thread(target=_warm, daemon=True).start()


def _mcp_browse_entities(domain: str, entity_set: str, limit: int = 100):
    """Helper: list entities via MCP umodel_get_entities (cached 30s)."""
    key = f"entities:{domain}:{entity_set}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    from tools import build_tool_registry
    reg = build_tool_registry()
    prom = reg.get("prometheus")
    if prom is None:
        return _cache_put(key, [])
    try:
        raw = prom.client.call_tool("umodel_get_entities", {
            "regionId": prom.default_region,
            "workspace": prom.default_workspace,
            "domain": domain,
            "entity_set_name": entity_set,
            "limit": limit,
            "from_time": "now-6h",
            "to_time": "now",
        })
        return _cache_put(key, raw.get("data") or [])
    except Exception as exc:
        logger.debug("MCP entity browse %s/%s failed: %s", domain, entity_set, exc)
        return _cache_put(key, [])


@app.get("/api/cluster/overview")
async def cluster_overview():
    """Cluster health summary — sourced from MCP umodel entities."""
    nodes = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.node", 100)
    pods = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.pod", 500)
    phases = {}
    namespaces = set()
    for pod in pods:
        phase = pod.get("status") or pod.get("phase") or "Unknown"
        phases[phase] = phases.get(phase, 0) + 1
        ns = pod.get("namespace") or ""
        if ns:
            namespaces.add(ns)
    return {
        "nodes": len(nodes),
        "pods_total": len(pods),
        "pod_phases": phases,
        "total_restarts": 0,  # not exposed by MCP entity browse
        "namespaces": len(namespaces),
    }


@app.get("/api/cluster/nodes")
async def cluster_nodes():
    """Node list — sourced from MCP umodel entities."""
    raw = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.node", 100)
    nodes = []
    for n in raw:
        nodes.append({
            "name": n.get("name", ""),
            "roles": (n.get("roles") or "").split(",") if n.get("roles") else [],
            "ready": n.get("status", "Unknown"),
            "version": n.get("kubelet_version", ""),
            "os": n.get("os_image", ""),
            "cpu": n.get("cpu", ""),
            "memory": n.get("memory", ""),
        })
    return {"nodes": nodes}


@app.get("/api/cluster/namespaces")
async def cluster_namespaces():
    """Namespaces — derived from MCP pod entity namespaces (unique)."""
    pods = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.pod", 500)
    namespaces = sorted({p.get("namespace") for p in pods if p.get("namespace")})
    return {"namespaces": list(namespaces)}


@app.get("/api/cluster/pods")
async def cluster_pods(namespace: str = ""):
    """Pod list — sourced from MCP umodel entities."""
    raw = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.pod", 500)
    pods = []
    for p in raw:
        ns = p.get("namespace", "")
        if namespace and ns != namespace:
            continue
        pods.append({
            "name": p.get("name", ""),
            "namespace": ns,
            "phase": p.get("status", "Unknown"),
            "ready": p.get("ready", "?"),
            "restarts": int(p.get("restart_count", 0) or 0),
            "node": p.get("node_name", ""),
            "age": p.get("create_time", ""),
        })
    return {"pods": pods}


@app.get("/api/cluster/events")
async def cluster_events(namespace: str = "", limit: int = 50):
    """Recent events — synthesized from MCP signals.

    The MCP workspace doesn't publish a K8s event stream, so we surface
    the next-best thing: pods whose status is non-Running, plus any pods
    flagged by the alert centre threshold check. The dashboard event
    table renders these consistently with native K8s events.
    """
    events = []
    pods = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.pod", 500)
    for p in pods:
        ns = p.get("namespace", "")
        if namespace and ns != namespace:
            continue
        status = p.get("status") or "Unknown"
        if status != "Running":
            events.append({
                "type": "Warning",
                "reason": status,
                "message": f"Pod {p.get('name','')} is {status}",
                "source": "mcp:k8s.pod",
                "object": p.get("name", ""),
                "namespace": ns,
                "count": 1,
                "last_seen": p.get("__last_observed_time__", ""),
            })

    # Also surface the alert-centre threshold signals (high cpu/memory pods).
    try:
        from tools import build_tool_registry
        from agents.detection_agent import DetectionAgent
        from tools.llm_client import LLMClient
        cfg = _get_config()
        reg = build_tool_registry(cfg)
        agent = DetectionAgent(None, reg, cfg)
        signals = agent._check_prometheus_alerts()
        for sig in signals:
            events.append({
                "type": "Warning" if sig.severity == "warning" else "Critical",
                "reason": sig.severity,
                "message": sig.description,
                "source": "mcp:alert",
                "object": sig.service,
                "namespace": sig.namespace or "",
                "count": 1,
                "last_seen": "",
            })
    except Exception as exc:
        logger.debug("alert signal merge failed: %s", exc)

    return {"events": events[:limit]}


@app.get("/api/cluster/services")
async def cluster_services(namespace: str = ""):
    """Services list — from MCP k8s.service entities."""
    raw = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.service", 200)
    services = []
    for s in raw:
        ns = s.get("namespace", "")
        if namespace and ns != namespace:
            continue
        services.append({
            "name": s.get("name", ""),
            "namespace": ns,
            "type": s.get("type", ""),
            "cluster_ip": s.get("cluster_ip", ""),
            "ports": s.get("ports", ""),
        })
    return {"services": services}


@app.get("/api/cluster/topology")
async def cluster_topology():
    """Topology view sourced from MCP entities (k8s.pod + k8s.node + k8s.deployment + apm.service)."""
    nodes_raw = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.node", 100)
    pods_raw = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.pod", 500)
    deploys_raw = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.deployment", 200)
    svcs_raw = await asyncio.to_thread(_mcp_browse_entities, "k8s", "k8s.service", 200)

    import json as _json
    # Build node-IP → node-name index for pod→node mapping.
    nodes = []
    ip_to_node = {}
    for n in nodes_raw:
        # status is a JSON-encoded condition list: parse it for Ready.
        raw_st = n.get("status") or ""
        ready = False
        if isinstance(raw_st, str) and raw_st.startswith("["):
            try:
                for cond in _json.loads(raw_st):
                    if isinstance(cond, dict) and cond.get("type") == "Ready":
                        ready = cond.get("status") == "True"
                        break
            except (ValueError, TypeError):
                pass
        elif isinstance(raw_st, str):
            ready = raw_st.lower() == "ready"

        # capacity/allocatable hold {"cpu": "...", "memory": "..."} as JSON
        cpu = ""
        memory = ""
        cap_raw = n.get("capacity") or ""
        if isinstance(cap_raw, str) and cap_raw.startswith("{"):
            try:
                cap = _json.loads(cap_raw)
                cpu = str(cap.get("cpu", ""))
                memory = str(cap.get("memory", ""))
            except (ValueError, TypeError):
                pass

        node_name = n.get("name", "")
        ip = n.get("internal_ip") or ""
        if ip:
            ip_to_node[ip] = node_name

        nodes.append({
            "name": node_name,
            "roles": [],
            "ready": ready,
            "is_faulty": not ready,
            "cpu": cpu,
            "memory": memory,
        })

    pods = []
    namespaces = set()
    for p_ in pods_raw:
        ns = p_.get("namespace", "")
        if ns:
            namespaces.add(ns)
        phase = p_.get("status") or "Unknown"
        is_faulty = phase not in ("Running", "Succeeded")

        # MCP k8s.pod entity has no node_name. Best-effort: derive node
        # from the pod's instance_ip (which sits inside the node CIDR).
        # Since CIDR matching is messy, fall back to a /24-prefix match
        # against known node IPs.
        pod_ip = p_.get("instance_ip") or ""
        node_name = ""
        if pod_ip:
            # exact match (unlikely)
            node_name = ip_to_node.get(pod_ip, "")
            if not node_name:
                # try /24 prefix
                prefix = ".".join(pod_ip.split(".")[:3])
                for nip, nn in ip_to_node.items():
                    if nip.startswith(prefix):
                        node_name = nn
                        break

        pods.append({
            "name": p_.get("name", ""),
            "namespace": ns,
            "phase": phase,
            "node": node_name,
            "restarts": 0,
            "is_faulty": is_faulty,
            "fault_reason": phase if is_faulty else "",
            "owner": "",
            "owner_kind": "",
        })

    deployments = []
    for d in deploys_raw:
        ready_replicas = int(d.get("ready_replicas", 0) or 0)
        replicas = int(d.get("replicas", 0) or 0)
        is_faulty = replicas > 0 and ready_replicas < replicas
        deployments.append({
            "name": d.get("name", ""),
            "namespace": d.get("namespace", ""),
            "replicas": replicas,
            "ready_replicas": ready_replicas,
            "is_faulty": is_faulty,
        })

    services = []
    for sv in svcs_raw:
        services.append({
            "name": sv.get("name", ""),
            "namespace": sv.get("namespace", ""),
            "type": sv.get("type", ""),
            "cluster_ip": sv.get("cluster_ip", ""),
        })

    summary = {
        "total_nodes": len(nodes),
        "total_pods": len(pods),
        "total_deployments": len(deployments),
        "total_services": len(services),
        "faulty_nodes": sum(1 for n in nodes if n["is_faulty"]),
        "faulty_pods": sum(1 for p in pods if p["is_faulty"]),
        "faulty_deployments": sum(1 for d in deployments if d["is_faulty"]),
    }

    return {
        "nodes": nodes,
        "pods": pods,
        "deployments": deployments,
        "services": services,
        "namespaces": sorted(namespaces),
        "summary": summary,
    }

@app.get("/api/logs/{namespace}/{pod}")
async def pod_logs(namespace: str, pod: str, lines: int = 200, container: str = ""):
    c_flag = f"-c {container}" if container else ""
    raw = await _kubectl(f"logs {pod} {c_flag} --tail={lines}", namespace)
    return {"logs": raw}


# ─────────────────────────────────────────
# Prometheus Query APIs
# ─────────────────────────────────────────

_PROMQL_TOKEN_MAP = {
    # Map PromQL metric/keyword tokens to MCP metric-name substrings.
    "node_cpu_seconds": "cpu",
    "node_memory_memavailable": "memory",
    "node_memory_memtotal": "memory",
    "node_filesystem": "memory",  # closest MCP analog
    "container_cpu_usage": "cpu",
    "container_memory_working_set": "memory",
    "kube_pod_container_status_restarts": "restart",
    "kube_deployment_status_replicas": "memory",  # no direct analog
    "apiserver_request_total": "request",
    "node_netstat_tcp": "memory",
    "node_network_receive": "network",
    "node_network_transmit": "network",
    "coredns_dns_requests": "request",
}


def _extract_mcp_filter(query: str) -> str:
    """Translate a PromQL fragment to an MCP-friendly substring filter.

    Best-effort: scan well-known PromQL metric names and map them to a
    short substring the MCP `prometheus` adapter can match against
    `pod_cpu_usage_rate` / `pod_memory_working_set_bytes` / etc.
    Falls back to empty (return everything) if nothing recognized.
    """
    q = (query or "").lower()
    for token, sub in _PROMQL_TOKEN_MAP.items():
        if token in q:
            return sub
    # If query is already a bare metric name (no parentheses), pass through
    if query and "(" not in query and "[" not in query and " " not in query:
        return query
    return ""


_NODE_METRIC_RE = (
    "node_network_", "node_netstat_", "node_disk_",
    "node_memory_", "node_cpu_", "node_filesystem_",
    "node_load", "node_sockstat_", "node_filefd_",
    "process_cpu_", "process_resident_",
)


def _extract_node_metric(query: str) -> str:
    """If the PromQL fragment references a known node-exporter metric,
    return the bare metric name (case-preserved) so we can fetch it
    directly via umodel_get_metrics (k8s.node / node_exporter_node)."""
    if not query:
        return ""
    import re
    # Case-insensitive match against the prefix list, but preserve the
    # original case in the extracted metric name (server is case-sensitive).
    for pref in _NODE_METRIC_RE:
        m = re.search(rf"({re.escape(pref)}[A-Za-z0-9_]+)", query, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _fetch_node_metric(metric_name: str, start: str, end: str) -> dict:
    """Direct call to umodel_get_metrics for node-exporter metrics."""
    from tools import build_tool_registry
    reg = build_tool_registry()
    tool = reg.get("prometheus")
    if tool is None or tool.client is None:
        return {"results": [], "resultType": "matrix"}
    from_arg = start if start else "now-1h"
    to_arg = end if end else "now"
    try:
        raw = tool.client.call_tool("umodel_get_metrics", {
            "regionId": tool.default_region,
            "workspace": tool.default_workspace,
            "domain": "k8s",
            "entity_set_name": "k8s.node",
            "metric_domain_name": "k8s.metric.node_exporter_node",
            "metric": metric_name,
            "from_time": from_arg,
            "to_time": to_arg,
        })
    except Exception as exc:
        logger.debug("node metric fetch %s failed: %s", metric_name, exc)
        return {"results": [], "resultType": "matrix"}

    import json as _json
    data = raw.get("data", [])
    results = []
    for item in data:
        if not isinstance(item, dict): continue
        ts_raw = item.get("__ts__", "")
        val_raw = item.get("__value__", "")
        try:
            ts = _json.loads(ts_raw) if isinstance(ts_raw, str) else ts_raw
            val = _json.loads(val_raw) if isinstance(val_raw, str) else val_raw
        except (ValueError, TypeError):
            continue
        if not isinstance(ts, list) or not isinstance(val, list):
            continue
        values = []
        for i, t_ns in enumerate(ts):
            if i >= len(val): break
            try:
                ts_sec = int(float(t_ns) / 1e9)
            except (TypeError, ValueError):
                continue
            values.append([ts_sec, str(val[i])])
        labels_raw = item.get("__labels__") or "{}"
        try:
            labels = _json.loads(labels_raw) if isinstance(labels_raw, str) else {}
        except (ValueError, TypeError):
            labels = {}
        metric_block = {"__name__": metric_name, **(labels if isinstance(labels, dict) else {})}
        results.append({
            "metric": metric_block,
            "values": values,
            "value": values[-1] if values else [0, "0"],
        })
    return {"results": results, "resultType": "matrix"}


def _prom_query_sync(query: str, query_type: str = "instant",
                     start: str = "", end: str = "", step: str = "60s") -> dict:
    """Execute query against the MCP-backed `prometheus` SRETool. Cached 30s.

    Routes:
      1. If PromQL references a node-exporter metric (node_network_*,
         node_netstat_*, node_disk_*, etc.), call umodel_get_metrics on
         k8s.node directly — this is where node-level data lives.
      2. Otherwise, translate to a token and call golden_metrics on
         k8s.pod via the MCPMetricTool adapter (CPU/memory).
    """
    # Branch 1: node-exporter metric
    node_metric = _extract_node_metric(query)
    if node_metric:
        cache_key = f"nodemetric:{node_metric}:{start}:{end}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        out = _fetch_node_metric(node_metric, start, end)
        return _cache_put(cache_key, out)

    # Branch 2: golden metrics
    from tools import build_tool_registry
    reg = build_tool_registry()
    tool = reg.get("prometheus")
    if tool is None:
        return {"error": "prometheus tool not in registry"}
    mcp_query = _extract_mcp_filter(query)
    cache_key = f"prom2:{mcp_query}:{query_type}:{start}:{end}:{step}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        # Dashboard chart panels: aggregate mode = ONE MCP call returning
        # all metric series for the domain. Per-pod breakdown stays
        # available via per_entity but isn't worth the latency cost here.
        result = tool.execute(
            query=mcp_query,
            start=start,
            end=end,
            step=step,
            domain="k8s",
            entity_set_name="k8s.pod",
        )
        if not result.success:
            return {"error": result.error or "MCP metric tool failed"}
        data = result.data or {}
        out = {
            "results": data.get("results", []),
            "resultType": "matrix" if query_type == "range" else "vector",
        }
        return _cache_put(cache_key, out)
    except Exception as e:
        return {"error": f"MCP error: {e}"}


# ─── /api/alidata/* compatibility stubs ─────────────────────────────
# The original AliData admin section was removed during MCP cutover.
# These stubs return empty {} (200) so the frontend stops 404'ing
# until the JS is updated to use MCP-aware endpoints.

@app.get("/api/alidata/status")
async def _alidata_status_stub():
    return {"enabled": False, "backend": "mcp", "note": "AliData backend retired; using MCP"}


@app.get("/api/alidata/services")
async def _alidata_services_stub():
    return {"services": []}


@app.get("/api/alidata/metrics")
async def _alidata_metrics_stub(query: str = "", namespace: str = "",
                                 time_range: str = "1h"):
    return {"data": [], "note": "AliData metrics retired; use /api/prometheus/* (MCP-backed)"}


@app.get("/api/alidata/logs")
async def _alidata_logs_stub(query: str = "", time_range: str = "1h",
                              level: str = "", size: int = 100):
    return {"entries": [], "total_hits": 0, "returned": 0}


@app.get("/api/alidata/traces")
async def _alidata_traces_stub(service: str = "", operation: str = "",
                                lookback: str = "1h", limit: int = 20):
    return {"data": [], "service": service}


@app.get("/api/alidata/trace/{trace_id}")
async def _alidata_trace_detail_stub(trace_id: str):
    return {"data": [], "trace_id": trace_id}


@app.get("/api/prometheus/query")
async def prometheus_query(query: str = "", query_type: str = "instant",
                           start: str = "", end: str = "", step: str = "60s"):
    """Execute arbitrary PromQL queries."""
    if not query:
        raise HTTPException(400, "Missing 'query' parameter")
    result = await asyncio.to_thread(_prom_query_sync, query, query_type, start, end, step)
    return result


@app.get("/api/prometheus/metrics_summary")
async def prometheus_metrics_summary(namespace: str = ""):
    """Pre-built metrics summary for the dashboard: node CPU/mem/disk + container top."""
    ns_filter = f'namespace="{namespace}"' if namespace else ''

    queries = {
        "node_cpu": 'avg by(instance)(1 - rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
        "node_memory": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
        "node_disk": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
    }

    if ns_filter:
        queries["container_cpu_top"] = (
            f'topk(10, sum by(pod)(rate(container_cpu_usage_seconds_total{{{ns_filter}}}[5m])) * 100)'
        )
        queries["container_mem_top"] = (
            f'topk(10, sum by(pod)(container_memory_working_set_bytes{{{ns_filter}}}) / 1024 / 1024)'
        )
    else:
        queries["container_cpu_top"] = (
            'topk(10, sum by(pod, namespace)(rate(container_cpu_usage_seconds_total[5m])) * 100)'
        )
        queries["container_mem_top"] = (
            'topk(10, sum by(pod, namespace)(container_memory_working_set_bytes) / 1024 / 1024)'
        )

    import concurrent.futures
    results = {}

    def _query_one(key, q):
        return key, _prom_query_sync(q)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_query_one, k, q) for k, q in queries.items()]
        for f in concurrent.futures.as_completed(futures):
            k, v = f.result()
            results[k] = v

    return results


# ─────────────────────────────────────────
# Metric History APIs (time-series charts)
# ─────────────────────────────────────────

def _detect_anomalies_zscore(values: list, threshold: float = 3.0) -> list:
    """Z-score anomaly detection on a list of float values."""
    import math
    n = len(values)
    if n < 5:
        return []
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    if std == 0:
        return []
    anomalies = []
    for i, v in enumerate(values):
        z = abs(v - mean) / std
        if z > threshold:
            anomalies.append({
                "index": i, "value": round(v, 4),
                "zscore": round(z, 2),
                "severity": "critical" if z > threshold * 1.5 else "warning",
                "method": "zscore",
            })
    return anomalies


def _detect_anomalies_threshold(values: list, warn: float = None, crit: float = None) -> list:
    """Static threshold anomaly detection."""
    anomalies = []
    for i, v in enumerate(values):
        if crit is not None and v >= crit:
            anomalies.append({"index": i, "value": round(v, 4), "severity": "critical", "method": "threshold"})
        elif warn is not None and v >= warn:
            anomalies.append({"index": i, "value": round(v, 4), "severity": "warning", "method": "threshold"})
    return anomalies


def _detect_anomalies_ewma(values: list, span: int = 10, threshold: float = 3.0) -> list:
    """EWMA (Exponentially Weighted Moving Average) anomaly detection."""
    import math
    n = len(values)
    if n < span:
        return []
    alpha = 2.0 / (span + 1)
    ewma = values[0]
    ewma_var = 0.0
    anomalies = []
    for i, v in enumerate(values):
        if i == 0:
            continue
        diff = v - ewma
        ewma = alpha * v + (1 - alpha) * ewma
        ewma_var = alpha * diff * diff + (1 - alpha) * ewma_var
        std = math.sqrt(ewma_var) if ewma_var > 0 else 0
        if std > 0 and abs(diff) / std > threshold:
            anomalies.append({
                "index": i, "value": round(v, 4),
                "zscore": round(abs(diff) / std, 2),
                "severity": "critical" if abs(diff) / std > threshold * 1.5 else "warning",
                "method": "ewma",
            })
    return anomalies


def _run_anomaly_detection(values: list, methods: list, z_threshold: float,
                           ewma_span: int, warn: float = None, crit: float = None) -> list:
    """Run configured anomaly detection methods and merge results."""
    all_anomalies = []
    seen_indices = set()
    if "zscore" in methods:
        for a in _detect_anomalies_zscore(values, z_threshold):
            if a["index"] not in seen_indices:
                seen_indices.add(a["index"])
                all_anomalies.append(a)
    if "threshold" in methods:
        for a in _detect_anomalies_threshold(values, warn, crit):
            if a["index"] not in seen_indices:
                seen_indices.add(a["index"])
                all_anomalies.append(a)
    if "ewma" in methods:
        for a in _detect_anomalies_ewma(values, ewma_span, z_threshold):
            if a["index"] not in seen_indices:
                seen_indices.add(a["index"])
                all_anomalies.append(a)
    return all_anomalies


@app.get("/api/prometheus/metric_history")
async def prometheus_metric_history(
    metric_name: str = "",
    duration: str = "1h",
    step: str = "",
    custom_query: str = "",
    max_series: int = 10,
    detect: bool = False,
):
    """Range query for a configured metric, with anomaly detection."""
    cfg = _get_config()

    # Resolve query + thresholds + detection methods from config metric_checks
    query = custom_query
    warn_threshold = None
    crit_threshold = None
    unit = "%"
    label_key = "instance"
    detect_methods = cfg.detection.default_detect_methods or ["zscore"]
    z_threshold = cfg.detection.default_z_threshold or 3.0
    ewma_span = cfg.detection.default_ewma_span or 10

    if metric_name and not custom_query:
        for mc in cfg.detection.metric_checks:
            if mc.get("name") == metric_name:
                query = mc.get("query", "")
                warn_threshold = mc.get("warn")
                crit_threshold = mc.get("crit")
                unit = mc.get("unit", "%")
                label_key = mc.get("label_key", "instance")
                detect_methods = mc.get("detect_methods") or detect_methods
                break

    if not query:
        raise HTTPException(400, "No query found for this metric")

    # Calculate time range
    dur_map = {"1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200, "24h": 86400}
    dur_seconds = dur_map.get(duration, 3600)
    now = int(time.time())
    start_ts = str(now - dur_seconds)
    end_ts = str(now)

    if not step:
        if dur_seconds <= 3600:
            step = "30s"
        elif dur_seconds <= 21600:
            step = "120s"
        else:
            step = "300s"

    result = await asyncio.to_thread(
        _prom_query_sync, query, "range", start_ts, end_ts, step
    )

    if result.get("error"):
        return result

    # Build series + detect anomalies
    series = []
    all_anomalies = []

    for r in result.get("results", []):
        metric_labels = r.get("metric", {})
        label = (metric_labels.get(label_key)
                 or metric_labels.get("pod")
                 or metric_labels.get("instance")
                 or metric_labels.get("__name__")
                 or "metric")
        # Shorten label
        label = label.replace(":9100", "").replace(":10250", "")
        # Append namespace for container-level if multiple namespaces
        ns_val = metric_labels.get("namespace", "")
        if ns_val and label_key in ("pod",) and not custom_query:
            label = f"{label} ({ns_val})"

        values_raw = r.get("values", [])
        timestamps = [v[0] for v in values_raw]
        values = [float(v[1]) if v[1] != "NaN" else 0.0 for v in values_raw]

        series.append({
            "label": label,
            "timestamps": timestamps,
            "values": [round(v, 4) for v in values],
        })

        # Anomaly detection per series (disabled — was producing 1MB+ JSON
        # with thousands of markers per chart, slowing the metrics tab to 18s).
        # Re-enable by setting detect=true query param.
        if detect:
            anoms = _run_anomaly_detection(
                values, detect_methods, z_threshold, ewma_span,
                warn_threshold, crit_threshold
            )
            for a in anoms:
                a["series_label"] = label
                if a["index"] < len(timestamps):
                    a["timestamp"] = timestamps[a["index"]]
                all_anomalies.append(a)

    # Limit series count: sort by average value descending, keep top max_series
    if len(series) > max_series:
        series.sort(key=lambda s: sum(s["values"]) / max(len(s["values"]), 1), reverse=True)
        kept_labels = {s["label"] for s in series[:max_series]}
        series = series[:max_series]
        all_anomalies = [a for a in all_anomalies if a.get("series_label") in kept_labels]

    return {
        "metric_name": metric_name,
        "unit": unit,
        "duration": duration,
        "series": series,
        "anomalies": all_anomalies,
        "thresholds": {"warn": warn_threshold, "crit": crit_threshold},
        "detection": {
            "methods": detect_methods,
            "z_threshold": z_threshold,
            "ewma_span": ewma_span,
            "anomaly_count": len(all_anomalies),
        },
    }


@app.get("/api/prometheus/business_metrics")
async def prometheus_business_metrics(duration: str = "1h", step: str = "",
                                      namespace: str = ""):
    """Business-level metrics: service request rate, latency P99, error rate."""
    cfg = _get_config()
    services = cfg.detection.business_services or []

    dur_map = {"1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200, "24h": 86400}
    dur_seconds = dur_map.get(duration, 3600)
    now = int(time.time())
    start_ts = str(now - dur_seconds)
    end_ts = str(now)

    if not step:
        step = "30s" if dur_seconds <= 3600 else "120s" if dur_seconds <= 21600 else "300s"

    svc_filter = "|".join(services) if services else ".*"
    ns_filter = f',namespace="{namespace}"' if namespace else ''

    queries = {
        "request_rate": f'sum by(service)(rate(istio_requests_total{{reporter="source",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m])) or sum by(service)(rate(http_server_requests_seconds_count{{service=~"{svc_filter}"{ns_filter}}}[5m]))',
        "latency_p99": f'histogram_quantile(0.99, sum by(le, service)(rate(istio_request_duration_milliseconds_bucket{{reporter="source",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m]))) or histogram_quantile(0.99, sum by(le, service)(rate(http_server_requests_seconds_bucket{{service=~"{svc_filter}"{ns_filter}}}[5m]))) * 1000',
        "error_rate": f'sum by(service)(rate(istio_requests_total{{reporter="source",response_code=~"5.*",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m])) / sum by(service)(rate(istio_requests_total{{reporter="source",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m])) * 100',
    }

    import concurrent.futures
    results = {}

    def _query_one(key, q):
        return key, _prom_query_sync(q, "range", start_ts, end_ts, step)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_query_one, k, q) for k, q in queries.items()]
        for f in concurrent.futures.as_completed(futures):
            k, v = f.result()
            results[k] = v

    return {"duration": duration, "metrics": results}


# ─────────────────────────────────────────
# Jaeger Trace APIs
# ─────────────────────────────────────────

def _jaeger_api_ok(base_url: str) -> tuple[bool, List[str]]:
    import requests as req
    if not base_url:
        return False, []
    try:
        resp = req.get(f"{base_url.rstrip('/')}/api/services", timeout=5)
        if resp.status_code != 200:
            return False, []
        data = resp.json()
        services = data.get("data", [])
        return isinstance(services, list), services if isinstance(services, list) else []
    except Exception:
        return False, []


def _discover_jaeger_url() -> str:
    """Discover a reachable Jaeger query API endpoint."""
    cached = _state.get("jaeger_url")
    ok, services = _jaeger_api_ok(cached or "")
    if ok and services:
        return cached

    cfg = _get_config()
    candidates: List[str] = []
    if getattr(cfg.observability, "jaeger_url", None):
        candidates.append(getattr(cfg.observability, "jaeger_url", "").rstrip("/"))

    try:
        svc_data = json.loads(_kubectl_sync("get svc -A -o json"))
    except Exception:
        svc_data = {}
    try:
        node_data = json.loads(_kubectl_sync("get nodes -o json"))
    except Exception:
        node_data = {}

    nodes: List[str] = []
    for node in node_data.get("items", []):
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP" and addr.get("address"):
                nodes.append(addr["address"])

    for svc in svc_data.get("items", []):
        meta = svc.get("metadata", {})
        spec = svc.get("spec", {})
        ns_name = f"{meta.get('namespace', '')}/{meta.get('name', '')}".lower()
        if not any(token in ns_name for token in ("jaeger", "trace")):
            continue
        for port in spec.get("ports", []):
            port_num = port.get("port")
            target_name = str(port.get("name", "")).lower()
            if port_num != 16686 and "query" not in target_name and "ui" not in target_name:
                continue

            cluster_ip = spec.get("clusterIP")
            if cluster_ip and cluster_ip != "None":
                candidates.append(f"http://{cluster_ip}:{port_num}")

            node_port = port.get("nodePort")
            if node_port:
                for node_ip in nodes:
                    candidates.append(f"http://{node_ip}:{node_port}")

    preferred_markers = {
        "nginx-web-server", "compose-post-service", "home-timeline-service",
        "user-timeline-service", "post-storage-service",
    }
    first_reachable = ""
    first_with_services = ""
    seen = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        ok, services = _jaeger_api_ok(url)
        if ok and services:
            if preferred_markers.intersection(set(services)):
                _state["jaeger_url"] = url
                logger.info("Jaeger endpoint selected: %s (%s services, preferred workload)", url, len(services))
                return url
            if not first_with_services:
                first_with_services = url
        if ok and not first_reachable:
            first_reachable = url

    if first_with_services:
        _state["jaeger_url"] = first_with_services
        logger.info("Jaeger endpoint selected: %s (first endpoint with services)", first_with_services)
        return first_with_services
    if first_reachable:
        _state["jaeger_url"] = first_reachable
        return first_reachable
    return ""  # native jaeger removed; MCP backend handles traces


def _jaeger_request(path: str, params: dict = None) -> dict:
    """Route Jaeger API paths to the MCP-backed `jaeger` SRETool.

    The frontend uses Jaeger's REST API contract; we translate four shapes:
      /api/services                       -> list services from search results
      /api/traces                         -> tool.execute(service=..., lookback=...)
      /api/traces/{trace_id}              -> tool.execute(trace_id=...)
      /api/services/{svc}/operations      -> derived from search results
    """
    params = params or {}
    try:
        from tools import build_tool_registry
        reg = build_tool_registry()
        tool = reg.get("jaeger")
        if tool is None:
            return {"error": "jaeger tool not in registry"}

        # /api/services -> list service entities (umodel_search_traces summaries
        # don't carry service names; entity browse gives the canonical list).
        if path == "/api/services":
            try:
                ents = tool.client.call_tool("umodel_get_entities", {
                    "regionId": tool.default_region,
                    "workspace": tool.default_workspace,
                    "domain": "apm",
                    "entity_set_name": "apm.service",
                    "limit": 100,
                    "from_time": "now-6h",
                    "to_time": "now",
                })
            except Exception as e:
                return {"error": f"entity browse failed: {e}"}
            services = sorted({
                e.get("service") or e.get("name")
                for e in (ents.get("data") or [])
                if e.get("service") or e.get("name")
            })
            return {"data": list(services)}

        # /api/traces?service=&lookback=&limit=
        if path == "/api/traces":
            service = params.get("service", "")
            lookback = params.get("lookback", "1h")
            limit = int(params.get("limit", 20))
            result = tool.execute(service=service, lookback=lookback, limit=limit)
            if not result.success:
                return {"error": result.error or "jaeger tool failed"}
            # Map MCP-lean shape to Jaeger v1 shape the dashboard expects
            jv1 = []
            for tr in result.data.get("traces", [])[:limit]:
                tid = tr.get("trace_id") or tr.get("traceID") or ""
                dur_us = int(float(tr.get("duration_ms") or 0) * 1000)
                jv1.append({
                    "traceID": tid,
                    "spans": [{
                        "processID": "p1",
                        "operationName": service or "<root>",
                        "duration": dur_us,
                        "startTime": 0,
                        "references": [],
                    }] * max(int(tr.get("span_count") or 1), 1),
                    "processes": {"p1": {"serviceName": service or "<root>"}},
                })
            return {"data": jv1}

        # /api/traces/{trace_id}
        if path.startswith("/api/traces/"):
            tid = path[len("/api/traces/"):]
            result = tool.execute(trace_id=tid, lookback="6h")
            if not result.success:
                return {"error": result.error or "jaeger tool failed"}
            jv1 = []
            for tr in result.data.get("traces", []):
                # Build processes map from span service names
                services = sorted({sp.get("serviceName", "") for sp in tr.get("spans", []) if sp.get("serviceName")})
                proc_map = {f"p{i+1}": {"serviceName": svc} for i, svc in enumerate(services)}
                svc_to_pid = {svc: pid for pid, info in proc_map.items() for svc in [info["serviceName"]]}
                spans_v1 = []
                for sp in tr.get("spans", []):
                    sn = sp.get("serviceName", "")
                    spans_v1.append({
                        "traceID": tid,
                        "spanID": sp.get("span_id") or sp.get("spanId") or "",
                        "operationName": sp.get("spanName") or sp.get("operation_name") or "",
                        "processID": svc_to_pid.get(sn, "p1"),
                        "duration": int(float(sp.get("duration_ms") or 0) * 1000),
                        "startTime": int(float(sp.get("startTime") or 0) / 1000),
                        "references": (
                            [{"refType": "CHILD_OF", "traceID": tid, "spanID": sp["parentSpanId"]}]
                            if sp.get("parentSpanId") else []
                        ),
                        "tags": [],
                    })
                jv1.append({
                    "traceID": tid,
                    "spans": spans_v1,
                    "processes": proc_map or {"p1": {"serviceName": "<unknown>"}},
                })
            return {"data": jv1}

        # /api/services/{svc}/operations -> operation names found in that service's spans
        if path.startswith("/api/services/") and path.endswith("/operations"):
            svc = path[len("/api/services/"):-len("/operations")]
            result = tool.execute(service=svc, lookback="6h", limit=50)
            if not result.success:
                return {"error": result.error or "jaeger tool failed"}
            ops = sorted({
                sp.get("spanName") or sp.get("operation_name")
                for tr in (result.data.get("traces") or [])
                for sp in (tr.get("spans") or [])
                if sp.get("spanName") or sp.get("operation_name")
            })
            return {"data": list(ops)}

        return {"error": f"unsupported jaeger path: {path}"}
    except Exception as e:
        return {"error": f"MCP error: {e}"}


@app.get("/api/jaeger/services")
async def jaeger_services():
    """List available services in Jaeger (MCP-backed)."""
    data = await asyncio.to_thread(_jaeger_request, "/api/services")
    services = data.get("data", []) if not data.get("error") else []
    return {
        "services": services,
        "error": data.get("error"),
        "selected_url": "mcp://aliyun-observability",
    }


@app.get("/api/jaeger/traces")
async def jaeger_traces(service: str = "", operation: str = "",
                        min_duration: str = "", max_duration: str = "",
                        limit: int = 20, lookback: str = "1h"):
    """Search traces by service and filters."""
    if not service:
        raise HTTPException(400, "Missing 'service' parameter")

    params = {"service": service, "limit": limit, "lookback": lookback}
    if operation:
        params["operation"] = operation
    if min_duration:
        params["minDuration"] = min_duration
    if max_duration:
        params["maxDuration"] = max_duration

    data = await asyncio.to_thread(_jaeger_request, "/api/traces", params)
    if data.get("error"):
        return {"traces": [], "error": data["error"]}

    traces = data.get("data", [])
    summaries = []
    for trace in traces[:limit]:
        spans = trace.get("spans", [])
        processes = trace.get("processes", {})
        services_in_trace = sorted(set(
            processes.get(s.get("processID", ""), {}).get("serviceName", "")
            for s in spans
            if processes.get(s.get("processID", ""), {}).get("serviceName", "")
        ))
        durations = [s.get("duration", 0) for s in spans]
        root_span = next((s for s in spans if not s.get("references")), spans[0] if spans else {})
        root_proc = processes.get(root_span.get("processID", ""), {})
        summaries.append({
            "traceID": trace.get("traceID", ""),
            "root_service": root_proc.get("serviceName", ""),
            "root_operation": root_span.get("operationName", ""),
            "span_count": len(spans),
            "services": services_in_trace,
            "total_duration_us": max(durations) if durations else 0,
            "avg_duration_us": sum(durations) // max(len(durations), 1),
            "start_time": root_span.get("startTime", 0),
        })

    return {
        "traces": summaries,
        "total": len(summaries),
        "selected_url": data.get("_selected_url") or _state.get("jaeger_url"),
    }


@app.get("/api/jaeger/trace/{trace_id}")
async def jaeger_trace_detail(trace_id: str):
    """Get full trace detail by trace ID."""
    data = await asyncio.to_thread(_jaeger_request, f"/api/traces/{trace_id}")
    if data.get("error"):
        return {"error": data["error"]}

    traces = data.get("data", [])
    if not traces:
        raise HTTPException(404, "Trace not found")

    trace = traces[0]
    spans = trace.get("spans", [])
    processes = trace.get("processes", {})

    span_list = []
    for s in spans:
        pid = s.get("processID", "")
        proc = processes.get(pid, {})
        span_list.append({
            "spanID": s.get("spanID", ""),
            "operationName": s.get("operationName", ""),
            "serviceName": proc.get("serviceName", ""),
            "duration_us": s.get("duration", 0),
            "startTime": s.get("startTime", 0),
            "tags": {t["key"]: t["value"] for t in s.get("tags", [])},
            "logs": [{"ts": l.get("timestamp"), "fields": l.get("fields")} for l in s.get("logs", [])],
            "references": s.get("references", []),
        })

    return {
        "traceID": trace.get("traceID"),
        "spans": span_list,
        "span_count": len(span_list),
        "processes": processes,
    }


@app.get("/api/jaeger/operations")
async def jaeger_operations(service: str = ""):
    """List operations for a Jaeger service."""
    if not service:
        return {"operations": []}
    data = await asyncio.to_thread(_jaeger_request, f"/api/services/{service}/operations")
    operations = data.get("data", []) if not data.get("error") else []
    return {
        "operations": operations,
        "error": data.get("error"),
        "selected_url": data.get("_selected_url") or data.get("selected_url") or _state.get("jaeger_url"),
    }


# ─────────────────────────────────────────
# Detection & Alert APIs
# ─────────────────────────────────────────

@app.get("/api/detection/signals")
async def get_detection_signals():
    return {"signals": list(_state["detection_signals"])}


@app.delete("/api/detection/signals")
async def clear_detection_signals():
    _state["detection_signals"].clear()
    return {"status": "cleared"}


@app.get("/api/detection/stream")
async def detection_stream():
    """SSE stream for detection signals."""
    async def event_gen():
        last_count = 0
        while True:
            current = len(_state["detection_signals"])
            if current > last_count:
                new = list(_state["detection_signals"])[last_count:]
                for s in new:
                    yield f"data: {json.dumps(s)}\n\n"
                last_count = current
            else:
                yield f": heartbeat {int(time.time())}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/detection/config")
async def get_detection_config():
    """返回当前检测配置"""
    cfg = _get_config()
    det = cfg.detection
    return {
        "sources_enabled": det.sources_enabled,
        "metric_checks": det.metric_checks,
        "critical_event_reasons": det.critical_event_reasons,
        "critical_pod_reasons": det.critical_pod_reasons,
        "default_detect_methods": det.default_detect_methods,
        "default_lookback_m": det.default_lookback_m,
        "default_z_threshold": det.default_z_threshold,
        "default_ewma_span": det.default_ewma_span,
        "categories_enabled": det.categories_enabled,
        "business_services": det.business_services,
        "db_services": det.db_services,
        "thresholds": det.thresholds,
    }


@app.put("/api/detection/config")
async def update_detection_config(request: Request):
    """运行时更新检测配置（内存生效，不写 YAML）"""
    body = await request.json()
    cfg = _get_config()
    det = cfg.detection
    if "sources_enabled" in body:
        det.sources_enabled.update(body["sources_enabled"])
    if "metric_checks" in body:
        det.metric_checks = body["metric_checks"]
    if "critical_event_reasons" in body:
        det.critical_event_reasons = body["critical_event_reasons"]
    if "critical_pod_reasons" in body:
        det.critical_pod_reasons = body["critical_pod_reasons"]
    if "default_detect_methods" in body:
        det.default_detect_methods = body["default_detect_methods"]
    if "default_lookback_m" in body:
        det.default_lookback_m = int(body["default_lookback_m"])
    if "default_z_threshold" in body:
        det.default_z_threshold = float(body["default_z_threshold"])
    if "default_ewma_span" in body:
        det.default_ewma_span = int(body["default_ewma_span"])
    if "categories_enabled" in body and isinstance(body["categories_enabled"], dict):
        det.categories_enabled.update(body["categories_enabled"])
    if "business_services" in body and isinstance(body["business_services"], list):
        det.business_services = body["business_services"]
    if "db_services" in body and isinstance(body["db_services"], list):
        det.db_services = body["db_services"]
    if "thresholds" in body and isinstance(body["thresholds"], dict):
        det.thresholds.update(body["thresholds"])
    # 重建 pipeline 使配置生效
    _state["pipeline"] = None
    # 持久化到磁盘
    _save_detection_config()
    _merge_platform_section("detection", {
        "sources_enabled": det.sources_enabled,
        "metric_checks": det.metric_checks,
        "critical_event_reasons": det.critical_event_reasons,
        "critical_pod_reasons": det.critical_pod_reasons,
        "default_detect_methods": det.default_detect_methods,
        "default_algorithm": getattr(det, "default_algorithm", "zscore"),
        "default_lookback_m": det.default_lookback_m,
        "default_z_threshold": det.default_z_threshold,
        "default_ewma_span": det.default_ewma_span,
        "min_samples": getattr(det, "min_samples", 12),
        "confirmation_points": getattr(det, "confirmation_points", 1),
        "relative_change_threshold": getattr(det, "relative_change_threshold", 0.0),
        "spectral_residual_threshold": getattr(det, "spectral_residual_threshold", 3.0),
        "categories_enabled": det.categories_enabled,
        "business_services": det.business_services,
        "db_services": det.db_services,
        "thresholds": det.thresholds,
    })
    return {
        "status": "ok",
        "detection": {
            "sources_enabled": det.sources_enabled,
            "metric_checks": det.metric_checks,
            "critical_event_reasons": det.critical_event_reasons,
            "critical_pod_reasons": det.critical_pod_reasons,
            "default_detect_methods": det.default_detect_methods,
            "default_lookback_m": det.default_lookback_m,
            "default_z_threshold": det.default_z_threshold,
            "default_ewma_span": det.default_ewma_span,
            "categories_enabled": det.categories_enabled,
            "business_services": det.business_services,
            "db_services": det.db_services,
            "thresholds": det.thresholds,
        },
    }


@app.get("/api/platform/config")
async def platform_config_get():
    """Return configuration-center stored overrides and effective runtime values."""
    return _platform_config_public()


@app.put("/api/platform/config")
async def platform_config_update(request: Request):
    """Update configuration-center overrides for llm/detection/remediation."""
    body = await request.json()
    allowed = {"llm", "detection", "remediation"}
    data = _load_platform_config_file()
    for section in allowed:
        if section not in body:
            continue
        value = body.get(section)
        if value is None:
            data.pop(section, None)
        elif isinstance(value, dict):
            current = data.get(section)
            if not isinstance(current, dict):
                current = {}
            if section == "llm" and value.get("api_key") in ("", None, "********"):
                value = {k: v for k, v in value.items() if k != "api_key"}
            current.update(value)
            data[section] = current
        else:
            raise HTTPException(400, f"{section} must be an object")
    data["updated_at"] = time.time()
    _save_platform_config_file(data)
    _reload_runtime_config()
    return {"status": "ok", **_platform_config_public()}


@app.post("/api/platform/config/reload")
async def platform_config_reload():
    """Reload YAML/env config and re-apply config-center overrides."""
    _reload_runtime_config()
    return {"status": "ok", **_platform_config_public()}


@app.delete("/api/platform/config")
async def platform_config_reset():
    """Clear configuration-center overrides and fall back to YAML/env config."""
    if _PLATFORM_CONFIG_FILE.exists():
        _PLATFORM_CONFIG_FILE.unlink()
    _reload_runtime_config()
    return {"status": "ok", **_platform_config_public()}


@app.get("/api/alerts/list")
async def alert_list(namespace: str = ""):
    """Fetch all current alerts from all sources with details."""
    cfg = _get_config()
    registry = build_tool_registry(cfg)
    llm = LLMClient(cfg.llm) if cfg.llm.api_key else None
    agent = DetectionAgent(llm, registry, cfg)
    signals = await asyncio.to_thread(agent.detect, namespace)
    return {
        "alerts": [s.to_dict() for s in signals],
        "total": len(signals),
        "sources": list(set(s.source for s in signals)),
    }


@app.get("/api/alerts/scan")
async def alert_scan(namespace: str = ""):
    """Run alert compression scan (SOW core)."""
    cfg = _get_config()
    if not cfg.llm.api_key:
        raise HTTPException(
            503,
            "LLM API Key 未配置。请在 .env 文件中设置 LLM_API_KEY，然后重启服务。"
        )
    llm = LLMClient(cfg.llm)
    registry = build_tool_registry(cfg)
    agent = AlertAgent(llm, registry)

    # Collect raw alerts first, then compress
    raw_alerts = await agent._collect_alerts(namespace)
    result = await agent.compress_and_recommend(alerts=raw_alerts, namespace=namespace)

    # Attach raw alert details for frontend display
    result["raw_alerts"] = [
        {"name": a.name, "severity": a.severity, "source": a.source,
         "timestamp": a.timestamp, "labels": a.labels, "message": a.message}
        for a in raw_alerts
    ]
    return result


# ─────────────────────────────────────────
# RCA APIs
# ─────────────────────────────────────────

@app.post("/api/rca/run")
async def rca_run(request: Request):
    """Trigger an RCA pipeline."""
    body = await request.json()
    query = body.get("query", "")
    namespace = body.get("namespace", "")

    if not query:
        raise HTTPException(400, "Missing 'query' field")

    cfg = _get_config()
    if not cfg.llm.api_key:
        raise HTTPException(
            503,
            "LLM API Key 未配置。请在 .env 文件中设置 LLM_API_KEY 或在 config_cluster.yaml 中直接配置 llm.api_key，然后重启服务。"
        )

    run_id = f"rca-{uuid.uuid4().hex[:8]}"
    _state["rca_runs"][run_id] = {
        "id": run_id,
        "query": query,
        "namespace": namespace,
        "status": "running",
        "logs": [],
        "events": [],
        "result": None,
        "started_at": time.time(),
    }

    def log_cb(msg):
        if isinstance(msg, dict):
            _state["rca_runs"][run_id]["events"].append(msg)
        else:
            _state["rca_runs"][run_id]["logs"].append(msg)

    def _run_sync():
        """Run pipeline in a thread to avoid blocking the event loop."""
        try:
            pipeline = _get_pipeline()
            result = asyncio.run(pipeline.run(query, namespace, log_cb))
            _state["rca_runs"][run_id]["result"] = result.to_dict()
            _state["rca_runs"][run_id]["status"] = result.status
        except Exception as e:
            logger.error(f"RCA pipeline error: {e}", exc_info=True)
            _state["rca_runs"][run_id]["status"] = "failed"
            _state["rca_runs"][run_id]["result"] = {"error": str(e)}
        finally:
            _save_rca_history()

    # Run in background thread (pipeline does sync LLM calls that block the loop)
    import threading
    t = threading.Thread(target=_run_sync, daemon=True, name=f"rca-{run_id}")
    t.start()
    return {"run_id": run_id}


@app.get("/api/rca/history")
async def rca_history(limit: int = 20):
    runs = sorted(_state["rca_runs"].values(), key=lambda r: r.get("started_at", 0), reverse=True)
    return {"runs": [
        {
            "id": r["id"],
            "query": r["query"],
            "status": r["status"],
            "started_at": r.get("started_at"),
            "duration_s": (r.get("result", {}) or {}).get("duration_s", 0),
            # session_id is the trace key — expert feedback uses it to look up real RCA output
            "session_id": (r.get("result", {}) or {}).get("session_id", ""),
        }
        for r in runs[:limit]
    ]}


@app.get("/api/rca/{run_id}")
async def rca_status(run_id: str):
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/rca/{run_id}/stream")
async def rca_stream(run_id: str):
    """SSE stream for RCA execution logs."""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    async def event_gen():
        log_idx = 0
        evt_idx = 0
        while True:
            # Send new logs
            logs = run["logs"]
            if log_idx < len(logs):
                for msg in logs[log_idx:]:
                    yield f"data: {json.dumps({'type': 'log', 'msg': msg})}\n\n"
                log_idx = len(logs)

            # Send new structured events
            events = run.get("events", [])
            if evt_idx < len(events):
                for evt in events[evt_idx:]:
                    yield f"data: {json.dumps({'type': 'event', 'data': evt})}\n\n"
                evt_idx = len(events)

            if run["status"] in ("completed", "failed"):
                yield f"data: {json.dumps({'type': 'done', 'status': run['status'], 'result': run.get('result')})}\n\n"
                break

            yield f": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ─────────────────────────────────────────
# Remediation APIs
# ─────────────────────────────────────────

@app.post("/api/rca/{run_id}/remediation/approve")
async def rca_remediation_approve(run_id: str):
    """Approve and execute the pending remediation plan."""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    result = run.get("result")
    if not result:
        raise HTTPException(400, "RCA not completed yet")

    # Find remediation data — could be nested in pipeline result
    rca_inner = result.get("result", result)
    rem_data = None
    if isinstance(rca_inner, dict):
        # Check evidence.remediation in the inner RCA result
        evidence = rca_inner.get("evidence", {})
        if isinstance(evidence, dict):
            rem_data = evidence.get("remediation")
    # Also check events for remediation plan
    if not rem_data:
        for evt in reversed(run.get("events", [])):
            if evt.get("event") == "remediation":
                rem_data = evt.get("data")
                break

    if not rem_data or rem_data.get("status") != "pending_approval":
        raise HTTPException(400, "No pending remediation plan found")

    plan = rem_data.get("plan", {})
    if not plan.get("actions"):
        raise HTTPException(400, "Remediation plan has no actions")

    # Execute the plan
    cfg = _get_config()
    from tools import build_tool_registry, LLMClient
    from agents import RemediationAgent
    registry = build_tool_registry(cfg, allow_write=True)
    llm = LLMClient(cfg.llm)
    agent = RemediationAgent(llm, registry, cfg)

    # Override to skip approval check this time
    original_require = agent.require_approval
    agent.require_approval = False
    agent.enabled = True

    rca_result = rca_inner if isinstance(rca_inner, dict) else result
    exec_result = await agent.remediate(rca_result, confidence=1.0, approved=True)

    agent.require_approval = original_require

    # Store the execution result
    run["remediation_result"] = exec_result
    run.setdefault("events", []).append({"event": "remediation_executed", "data": exec_result})

    return exec_result


@app.post("/api/rca/{run_id}/remediation/rollback")
async def rca_remediation_rollback(run_id: str):
    """Roll back the last remediation execution."""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    cfg = _get_config()
    from tools import build_tool_registry, LLMClient
    from agents import RemediationAgent
    registry = build_tool_registry(cfg, allow_write=True)
    llm = LLMClient(cfg.llm)
    agent = RemediationAgent(llm, registry, cfg)

    rollback_result = agent.rollback()
    run["remediation_rollback"] = rollback_result
    run.setdefault("events", []).append({"event": "remediation_rollback", "data": rollback_result})

    return rollback_result


@app.get("/api/rca/{run_id}/remediation")
async def rca_remediation_status(run_id: str):
    """Get remediation status for a run."""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    # Find remediation event
    rem_data = None
    for evt in reversed(run.get("events", [])):
        if evt.get("event") in ("remediation", "remediation_executed", "remediation_rollback"):
            rem_data = evt.get("data")
            break

    return {
        "run_id": run_id,
        "remediation": rem_data,
        "execution": run.get("remediation_result"),
        "rollback": run.get("remediation_rollback"),
    }


@app.get("/api/heal/recipes")
async def heal_recipes():
    """Return deterministic self-healing knowledge-base recipes."""
    library = _load_heal_recipe_library()
    recipes = [_public_heal_recipe(r) for r in library.get("recipes", [])]
    validation = _validate_heal_recipe_library(library)
    categories = sorted({r.get("category", "unknown") for r in recipes})
    fault_types = sorted({r.get("fault_type", "unknown") for r in recipes})
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for recipe in recipes:
        grouped.setdefault(str(recipe.get("category") or "unknown"), []).append(recipe)
    return {
        "recipes": recipes,
        "grouped": grouped,
        "version": library.get("version"),
        "source": library.get("source"),
        "description": library.get("description"),
        "validation": {
            "ok": validation.get("ok"),
            "error_count": validation.get("error_count"),
            "warning_count": validation.get("warning_count"),
            "issues": validation.get("issues", [])[:20],
        },
        "total": len(recipes),
        "categories": categories,
        "fault_types": fault_types,
    }


@app.get("/api/heal/recipes/validate")
async def heal_recipes_validate():
    """Validate the external self-healing recipe library."""
    return _validate_heal_recipe_library(_load_heal_recipe_library())


@app.post("/api/heal/suggest")
async def heal_suggest(request: Request):
    """Generate self-healing suggestions without executing commands."""
    body = await request.json()
    result = _build_heal_suggestions(body)
    cfg = _get_config()
    result["policy"] = {
        "enabled": cfg.remediation.enabled,
        "dry_run": getattr(cfg.remediation, "dry_run", True),
        "require_approval": cfg.remediation.require_approval,
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "recommend_only": getattr(cfg.remediation, "recommend_only", True),
    }
    return result


@app.post("/api/heal/capability")
async def heal_capability(request: Request):
    """Analyze an alert/RCA payload and return available self-healing capability."""
    body = await request.json()
    result = _build_heal_suggestions(body)
    cfg = _get_config()
    result["source"] = body.get("source") or ("alert" if body.get("alert") else "diagnosis")
    result["policy"] = {
        "enabled": cfg.remediation.enabled,
        "dry_run": getattr(cfg.remediation, "dry_run", True),
        "require_approval": cfg.remediation.require_approval,
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "recommend_only": getattr(cfg.remediation, "recommend_only", True),
    }
    result["summary"] = {
        "fault_type": result.get("fault_type"),
        "namespace": result.get("namespace"),
        "target": result.get("target"),
        "suggestion_count": len(result.get("suggestions") or []),
        "blocked_count": len(result.get("blocked_templates") or []),
        "available": bool(result.get("suggestions")),
    }
    return result


@app.get("/api/heal/runs")
async def heal_runs(limit: int = 50):
    runs = _load_heal_runs()
    selected = runs[: max(1, min(limit, 200))]
    summary = {
        "total": len(runs),
        "returned": len(selected),
        "dry_run": sum(1 for r in runs if r.get("dry_run") is True or r.get("status") == "dry_run"),
        "real": sum(1 for r in runs if r.get("dry_run") is False),
        "succeeded": sum(1 for r in runs if r.get("success") is True),
        "failed": sum(1 for r in runs if r.get("success") is False or r.get("status") == "failed"),
        "verified_recovered": sum(1 for r in runs if (r.get("verification") or {}).get("recovered") is True),
        "verified_unrecovered": sum(1 for r in runs if (r.get("verification") or {}).get("recovered") is False),
    }
    return {"runs": selected, "total": len(runs), "summary": summary}


@app.get("/api/heal/runs/{run_id}")
async def heal_run_detail(run_id: str):
    for run in _load_heal_runs():
        if run.get("id") == run_id:
            return run
    path = _HEAL_ARTIFACT_DIR / f"{run_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(500, f"Failed to load heal run: {e}")
    raise HTTPException(404, "Heal run not found")


@app.post("/api/heal/execute")
async def heal_execute(request: Request):
    """Execute user-confirmed self-healing commands, defaulting to dry-run."""
    body = await request.json()
    cfg = _get_config()
    suggestion = _build_heal_suggestions(body)
    namespace = body.get("namespace") or suggestion.get("namespace") or cfg.kubernetes.namespace or "default"
    dry_run = bool(body.get("dry_run", getattr(cfg.remediation, "dry_run", True)))
    cluster_id = body.get("cluster_id") or None
    commands = [str(c).strip() for c in (body.get("commands") or []) if str(c).strip()]
    rollback_commands = [str(c).strip() for c in (body.get("rollback_commands") or []) if str(c).strip()]
    if not commands:
        commands = [s["command"] for s in suggestion.get("suggestions", []) if s.get("executable")]
        rollback_commands = [s.get("rollback_command", "") for s in suggestion.get("suggestions", []) if s.get("rollback_command")]
    if not commands:
        raise HTTPException(400, "No executable healing commands")
    max_steps = max(1, int(getattr(cfg.remediation, "max_steps", 5) or 5))
    if not dry_run and len(commands) > max_steps:
        raise HTTPException(400, f"Self-healing command count {len(commands)} exceeds max_steps={max_steps}")

    blocked = [{"command": cmd, "reason": _validate_heal_command(cmd)} for cmd in commands]
    blocked = [b for b in blocked if b["reason"]]
    if blocked:
        raise HTTPException(400, {"message": "Healing commands failed safety validation", "blocked": blocked})
    if not dry_run:
        diagnosis_gate = suggestion.get("diagnosis_gate") or {}
        if not diagnosis_gate.get("ready"):
            raise HTTPException(400, {
                "message": "Self-healing real execution requires a high-confidence RCA diagnosis",
                "diagnosis_gate": diagnosis_gate,
            })
        if getattr(cfg.remediation, "recommend_only", False):
            raise HTTPException(400, "Self-healing is in recommend_only mode; disable recommend_only in configuration center to execute real actions")
        if not cfg.remediation.enabled:
            raise HTTPException(400, "Self-healing execution is disabled in configuration center")
        if cfg.remediation.require_approval and not bool(body.get("approved")):
            raise HTTPException(400, "Self-healing execution requires approved=true")
        high_risk = [cmd for cmd in commands if any(x in cmd for x in (" delete ", " drain ", " cordon ", " taint "))]
        if high_risk and getattr(cfg.remediation, "require_confirmation_for_high_risk", True) and not bool(body.get("risk_ack")):
            raise HTTPException(400, "High-risk healing commands require risk_ack=true")

    run_id = f"heal-{uuid.uuid4().hex[:10]}"
    started_at = time.time()
    results: List[Dict[str, Any]] = []
    for idx, cmd in enumerate(commands):
        if dry_run:
            result = {"command": cmd, "ok": True, "dry_run": True, "stdout": "", "stderr": ""}
        else:
            result = await asyncio.to_thread(_shell_sync, cmd, 120, cluster_id)
        results.append({"index": idx, **result})
        if not result.get("ok") and not dry_run:
            break
    success = all(r.get("ok") for r in results)
    verification = await asyncio.to_thread(
        _verify_heal_recovery,
        namespace=namespace,
        suggestion=suggestion,
        commands=commands,
        cluster_id=cluster_id,
        dry_run=dry_run,
        executed_success=success,
    )
    if verification.get("recovered") is False:
        success = False
    run = {
        "id": run_id,
        "status": "dry_run" if dry_run else ("succeeded" if success else "failed"),
        "success": success,
        "dry_run": dry_run,
        "namespace": namespace,
        "cluster_id": cluster_id,
        "commands": commands,
        "rollback_commands": rollback_commands,
        "results": results,
        "verification": verification,
        "source": body.get("source") or "api",
        "fault_type": body.get("fault_type") or suggestion.get("fault_type") or _normalize_heal_fault_type(body.get("message", "")),
        "diagnosis_mode": suggestion.get("diagnosis_mode") or body.get("diagnosis_mode") or "alert_only",
        "diagnosis_used": suggestion.get("diagnosis_used") or {},
        "diagnosis_gate": suggestion.get("diagnosis_gate") or {},
        "diagnosis_summary": suggestion.get("diagnosis_summary") or "",
        "edited_actions": body.get("edited_actions") or [],
        "policy": {
            "enabled": cfg.remediation.enabled,
            "dry_run_default": getattr(cfg.remediation, "dry_run", True),
            "require_approval": cfg.remediation.require_approval,
            "recommend_only": getattr(cfg.remediation, "recommend_only", True),
            "max_steps": max_steps,
            "confidence_threshold": getattr(cfg.remediation, "confidence_threshold", 0.85),
        },
        "started_at": started_at,
        "finished_at": time.time(),
    }
    _save_heal_run(run)
    return run


@app.post("/api/heal/runs/{run_id}/rollback")
async def heal_run_rollback(run_id: str, request: Request):
    """Run recorded rollback commands for a healing run."""
    body = await request.json()
    dry_run = bool(body.get("dry_run", True))
    approved = bool(body.get("approved"))
    run = None
    for item in _load_heal_runs():
        if item.get("id") == run_id:
            run = item
            break
    if not run:
        raise HTTPException(404, "Heal run not found")
    commands = [cmd for cmd in (run.get("rollback_commands") or []) if cmd]
    if not commands:
        raise HTTPException(400, "No rollback commands recorded")
    blocked = [{"command": cmd, "reason": _validate_heal_command(cmd)} for cmd in commands]
    blocked = [b for b in blocked if b["reason"]]
    if blocked:
        raise HTTPException(400, {"message": "Rollback commands failed safety validation", "blocked": blocked})
    if not dry_run and not approved:
        raise HTTPException(400, "Rollback execution requires approved=true")
    results = []
    for idx, cmd in enumerate(reversed(commands)):
        if dry_run:
            result = {"command": cmd, "ok": True, "dry_run": True, "stdout": "", "stderr": ""}
        else:
            result = await asyncio.to_thread(_shell_sync, cmd, 120, run.get("cluster_id"))
        results.append({"index": idx, **result})
    run["rollback"] = {
        "dry_run": dry_run,
        "results": results,
        "finished_at": time.time(),
        "success": all(r.get("ok") for r in results),
    }
    _save_heal_run(run)
    return run["rollback"]


# ─────────────────────────────────────────
# Pipeline APIs
# ─────────────────────────────────────────

@app.get("/api/pipeline/history")
async def pipeline_history():
    pipeline = _get_pipeline()
    return {"history": pipeline.get_history()}


@app.get("/api/pipeline/stats")
async def pipeline_stats():
    pipeline = _get_pipeline()
    return pipeline.get_stats()


# ─────────────────────────────────────────
# Daemon Management APIs
# ─────────────────────────────────────────

@app.get("/api/daemon/status")
async def daemon_status():
    daemon = _state.get("daemon")
    if daemon and daemon._running:
        return daemon.status()
    return {"running": False}


@app.post("/api/daemon/start")
async def daemon_start(request: Request):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    
    if _state.get("daemon") and _state["daemon"]._running:
        return {"status": "already_running"}

    cfg = _get_config()

    def _push_signal(signal_obj):
        """Push detection signal to SSE deque for real-time streaming."""
        _state["detection_signals"].append(
            signal_obj.to_dict() if hasattr(signal_obj, "to_dict") else signal_obj
        )

    daemon = Daemon(
        cfg,
        log_callback=lambda msg: _state["daemon_logs"].append(msg),
        signal_callback=_push_signal,
    )
    _state["daemon"] = daemon

    def run():
        asyncio.run(daemon.start())

    t = threading.Thread(target=run, daemon=True, name="sre-daemon")
    t.start()
    _state["daemon_thread"] = t
    return {"status": "started"}


@app.post("/api/daemon/stop")
async def daemon_stop():
    daemon = _state.get("daemon")
    if daemon and daemon._running:
        asyncio.run_coroutine_threadsafe(daemon.stop(), daemon._loop)
        return {"status": "stopping"}
    return {"status": "not_running"}


@app.get("/api/daemon/logs")
async def daemon_logs(limit: int = 100):
    logs = list(_state["daemon_logs"])[-limit:]
    return {"logs": logs}


@app.get("/api/daemon/logs/stream")
async def daemon_log_stream():
    """SSE stream for daemon logs."""
    async def event_gen():
        idx = 0
        while True:
            logs = list(_state["daemon_logs"])
            if idx < len(logs):
                for msg in logs[idx:]:
                    yield f"data: {json.dumps({'type': 'log', 'msg': msg})}\n\n"
                idx = len(logs)
            
            # Status heartbeat
            daemon = _state.get("daemon")
            if daemon and daemon._running:
                yield f"data: {json.dumps({'type': 'status', 'data': daemon.status()})}\n\n"
            
            yield f": heartbeat\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ─────────────────────────────────────────
# Knowledge Base APIs (故障知识库)
# ─────────────────────────────────────────

def _get_fault_store():
    if "fault_store" not in _state:
        from memory import FaultContextStore
        _state["fault_store"] = FaultContextStore(_get_config())
    return _state["fault_store"]


def _get_feedback_store():
    if "feedback_store" not in _state:
        from memory import ExpertFeedbackStore
        _state["feedback_store"] = ExpertFeedbackStore()
    return _state["feedback_store"]


def _get_human_review_store():
    if "human_review_store" not in _state:
        from memory import HumanReviewStore
        _state["human_review_store"] = HumanReviewStore()
    return _state["human_review_store"]


def _get_evolution_tracker():
    if "evolution_tracker" not in _state:
        from memory import EvolutionTracker
        _state["evolution_tracker"] = EvolutionTracker.from_config()
    return _state["evolution_tracker"]


def _get_trace_store():
    """Reload-on-read trace store so we always see the latest pipeline_traces.json."""
    from memory import TraceStore
    return TraceStore(_get_config())


@app.get("/api/knowledge/stats")
async def knowledge_stats():
    """知识库统计信息"""
    store = _get_fault_store()
    fb = _get_feedback_store()
    stats = store.stats()
    fb_stats = fb.get_feedback_stats()
    return {
        "backend": stats.get("backend", "json"),
        "rules_count": stats.get("rules_count", 0),
        "faults_count": stats.get("faults_count", 0),
        "feedback_count": fb_stats.get("total", 0),
        "total_rules_generated": fb_stats.get("total_rules_generated", 0),
        "health_score": stats.get("health_score", 0),
        "avg_quality_score": stats.get("avg_quality_score", 0),
        "stale_rules": stats.get("stale_rules", 0),
        "low_quality_rules": stats.get("low_quality_rules", 0),
        "conflicts": stats.get("conflicts", 0),
    }


@app.get("/api/knowledge/governance")
async def knowledge_governance():
    """记忆治理报告：质量、陈旧规则、冲突规则。"""
    store = _get_fault_store()
    return store.governance_report()


@app.get("/api/knowledge/rules")
async def knowledge_rules():
    """获取所有诊断规则"""
    store = _get_fault_store()
    if store._backend == "json":
        return {"rules": store._rules_data}
    else:
        try:
            result = store._rules_col.get(limit=500)
            rules = []
            for i, doc_id in enumerate(result.get("ids", [])):
                meta = result.get("metadatas", [])[i] if result.get("metadatas") else {}
                doc = result.get("documents", [])[i] if result.get("documents") else ""
                rules.append({**meta, "rule_id": doc_id, "text": doc})
            return {"rules": rules}
        except Exception as e:
            return {"rules": [], "error": str(e)}


@app.post("/api/knowledge/rules")
async def knowledge_add_rule(request: Request):
    """添加诊断规则"""
    body = await request.json()
    store = _get_fault_store()
    rule_id = store.add_rule({
        "name": body.get("name", ""),
        "condition": body.get("condition", ""),
        "conclusion": body.get("conclusion", ""),
        "fault_type": body.get("fault_type", ""),
        "namespace": body.get("namespace", "general"),
        "confidence": float(body.get("confidence", 0.8)),
        "source": "manual",
    })
    return {"status": "ok", "rule_id": rule_id}


@app.delete("/api/knowledge/rules/{rule_id}")
async def knowledge_delete_rule(rule_id: str):
    """删除诊断规则"""
    store = _get_fault_store()
    if store._backend == "json":
        store._rules_data = [r for r in store._rules_data if r.get("rule_id") != rule_id]
        store._save_json()
    else:
        try:
            store._rules_col.delete(ids=[rule_id])
        except Exception:
            pass
    return {"status": "ok"}


@app.get("/api/knowledge/faults")
async def knowledge_faults():
    """获取所有历史故障记录"""
    store = _get_fault_store()
    if store._backend == "json":
        return {"faults": store._faults_data}
    else:
        try:
            result = store._faults_col.get(limit=500)
            faults = []
            for i, doc_id in enumerate(result.get("ids", [])):
                meta = result.get("metadatas", [])[i] if result.get("metadatas") else {}
                doc = result.get("documents", [])[i] if result.get("documents") else ""
                faults.append({**meta, "fault_id": doc_id, "text": doc})
            return {"faults": faults}
        except Exception as e:
            return {"faults": [], "error": str(e)}


@app.post("/api/knowledge/faults")
async def knowledge_add_fault(request: Request):
    """添加历史故障记录"""
    body = await request.json()
    store = _get_fault_store()
    fault_id = store.add_fault({
        "description": body.get("description", ""),
        "root_cause": body.get("root_cause", ""),
        "fault_type": body.get("fault_type", ""),
        "affected_services": body.get("affected_services", ""),
        "resolution": body.get("resolution", ""),
    })
    return {"status": "ok", "fault_id": fault_id}


@app.delete("/api/knowledge/faults/{fault_id}")
async def knowledge_delete_fault(fault_id: str):
    """删除故障记录"""
    store = _get_fault_store()
    if store._backend == "json":
        store._faults_data = [f for f in store._faults_data if f.get("fault_id") != fault_id]
        store._save_json()
    else:
        try:
            store._faults_col.delete(ids=[fault_id])
        except Exception:
            pass
    return {"status": "ok"}


@app.get("/api/knowledge/search")
async def knowledge_search(q: str = ""):
    """搜索知识库"""
    if not q:
        return {"rules": [], "faults": []}
    store = _get_fault_store()
    rules = store.query_similar_rules(q, n=10)
    faults = store.query_similar_faults(q, n=10)
    return {"rules": rules, "faults": faults}


@app.post("/api/knowledge/feedback")
async def knowledge_feedback(request: Request):
    """提交专家反馈"""
    body = await request.json()
    fb = _get_feedback_store()
    cfg = _get_config()

    learner = None
    if cfg.llm.api_key:
        from memory import ContextLearner
        learner = ContextLearner(LLMClient(cfg.llm), _get_fault_store(), cfg)

    result = fb.submit_feedback(
        incident_id=body.get("incident_id", ""),
        expert_diagnosis=body.get("expert_diagnosis", ""),
        comment=body.get("comment", ""),
        context_learner=learner,
        trace_store=_get_trace_store(),
        fault_store=_get_fault_store(),
    )
    return result


@app.get("/api/knowledge/feedback")
async def knowledge_feedback_list():
    """获取反馈历史"""
    fb = _get_feedback_store()
    return {"feedback": fb.get_recent_feedback(n=50), "stats": fb.get_feedback_stats()}


@app.get("/api/hitl/reviews")
async def hitl_reviews(status: str = "", limit: int = 50):
    """List human review queue items."""
    store = _get_human_review_store()
    return {
        "reviews": store.list_reviews(status=status, limit=limit),
        "stats": store.stats(),
    }


@app.post("/api/hitl/reviews/{review_id}/decision")
async def hitl_review_decision(review_id: str, request: Request):
    """Submit a human review decision and optionally trigger supervised learning."""
    body = await request.json()
    cfg = _get_config()
    learner = None
    if cfg.llm.api_key:
        from memory import ContextLearner
        learner = ContextLearner(LLMClient(cfg.llm), _get_fault_store(), cfg)

    try:
        review = _get_human_review_store().decide_review(
            review_id=review_id,
            decision=body.get("decision", ""),
            reviewer=body.get("reviewer", ""),
            expert_diagnosis=body.get("expert_diagnosis", ""),
            comment=body.get("comment", ""),
            context_learner=learner,
            feedback_store=_get_feedback_store(),
            trace_store=_get_trace_store(),
            fault_store=_get_fault_store(),
        )
        return {"status": "ok", "review": review}
    except KeyError:
        raise HTTPException(404, "Review not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/evolution/report")
async def evolution_report():
    """Return system evolution trends and recommendations."""
    tracker = _get_evolution_tracker()
    return tracker.get_evolution_report(fault_store=_get_fault_store())


# ─────────────────────────────────────────
# SOW Alignment & Fault Injection APIs
# ─────────────────────────────────────────

def _load_fault_scenarios() -> List[Dict[str, Any]]:
    if not SCENARIOS_FILE.exists():
        return []
    try:
        data = yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8")) or {}
        return data.get("scenarios", [])
    except Exception as e:
        logger.warning("Failed to load fault scenarios: %s", e)
        return []


def _load_llm_fault_scenarios() -> List[Dict[str, Any]]:
    """Load vLLM inference fault scenarios from the智算工具包."""
    scenarios: List[Dict[str, Any]] = []
    try:
        if LLM_FAULT_SCENARIOS_FILE.exists():
            data = yaml.safe_load(LLM_FAULT_SCENARIOS_FILE.read_text(encoding="utf-8")) or {}
            scenarios.extend(data.get("scenarios", []))
        if LLM_FAULT_SCENARIOS_OVERLAY_FILE.exists():
            data = yaml.safe_load(LLM_FAULT_SCENARIOS_OVERLAY_FILE.read_text(encoding="utf-8")) or {}
            scenarios.extend(data.get("scenarios", []))

        seen = set()
        deduped: List[Dict[str, Any]] = []
        for scenario in scenarios:
            sid = scenario.get("id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            symptoms = scenario.get("expected_symptoms") or []
            scenario["symptom_count"] = len(symptoms) if isinstance(symptoms, list) else 0
            scenario["experiment_family"] = "智算故障实验"
            deduped.append(scenario)
        return deduped
    except Exception as e:
        logger.warning("Failed to load vLLM fault scenarios: %s", e)
        return []


def _llm_fault_tool_status() -> Dict[str, Any]:
    scenarios = _load_llm_fault_scenarios()
    layers = sorted({s.get("layer", "unknown") for s in scenarios})
    fault_types = sorted({s.get("fault_type", "unknown") for s in scenarios})
    return {
        "available": LLM_FAULT_INJECTOR_DIR.exists() and LLM_FAULT_SCENARIOS_FILE.exists(),
        "tool_dir": str(LLM_FAULT_INJECTOR_DIR),
        "scenarios_file": str(LLM_FAULT_SCENARIOS_FILE),
        "overlay_file": str(LLM_FAULT_SCENARIOS_OVERLAY_FILE),
        "scenario_count": len(scenarios),
        "layers": layers,
        "fault_types": fault_types,
        "design_reference": str(ROOT_DIR / "doc" / "智算推理场景运行时测试工具设计报告.docx"),
        "environment": LLM_FAULT_ENVIRONMENT,
    }


def _find_llm_fault_scenario(scenario_id: str) -> Optional[Dict[str, Any]]:
    """Look up one vLLM fault scenario by id."""
    for scenario in _load_llm_fault_scenarios():
        if scenario.get("id") == scenario_id:
            return scenario
    return None


def _find_k8s_fault_scenario(scenario_id: str) -> Optional[Dict[str, Any]]:
    """Look up one general-compute K8s fault scenario by id."""
    for scenario in _load_fault_scenarios():
        if scenario.get("id") == scenario_id:
            return scenario
    return None


def _normalize_fault_experiment_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a live fault benchmark request."""
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "steps must be a non-empty list")
    if len(steps) > 50:
        raise HTTPException(400, "steps is limited to 50 entries")

    dry_run = bool(body.get("dry_run", True))
    benchmark_type = body.get("benchmark_type") or body.get("scenario_family") or "llm"
    if benchmark_type not in {"llm", "k8s"}:
        raise HTTPException(400, "benchmark_type must be 'llm' or 'k8s'")
    target_mode = body.get("target_mode", "k8s")
    if target_mode not in {"k8s", "host"}:
        raise HTTPException(400, "target_mode must be 'k8s' or 'host'")
    if benchmark_type == "k8s":
        target_mode = "k8s"

    total_duration_s = int(body.get("total_duration_s") or 0)
    max_total_duration_s = int(body.get("max_total_duration_s") or 24 * 3600)
    if total_duration_s < 0 or max_total_duration_s <= 0:
        raise HTTPException(400, "duration values must be non-negative")
    if total_duration_s > max_total_duration_s:
        raise HTTPException(400, "total_duration_s exceeds max_total_duration_s")

    common = {
        "dry_run": dry_run,
        "target_mode": target_mode,
        "cluster_id": body.get("cluster_id") or None,
        "namespace": body.get("namespace") or "default",
        "deployment": body.get("deployment") or "vllm-server",
        "host_id": body.get("host_id") or "",
        "confirm": bool(body.get("confirm")),
        "risk_ack": bool(body.get("risk_ack")),
        "host_ack": bool(body.get("host_ack")),
        "experiment_ack": bool(body.get("experiment_ack")),
        "baseline_samples": int(body.get("baseline_samples", 2)),
        "fault_samples": int(body.get("fault_samples", 3)),
        "sample_interval": float(body.get("sample_interval", 3.0)),
        "background_load": bool(body.get("background_load")),
    }

    if not dry_run and (not common["confirm"] or not common["experiment_ack"]):
        raise HTTPException(
            400,
            "Real live benchmark execution requires confirm=true and experiment_ack=true.",
        )
    if target_mode == "host" and not dry_run and not common["host_ack"]:
        raise HTTPException(400, "Bare-metal benchmark execution requires host_ack=true.")

    normalized_steps = []
    for idx, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            raise HTTPException(400, f"step {idx} must be an object")
        scenario_id = raw_step.get("scenario_id") or raw_step.get("id")
        if not scenario_id:
            raise HTTPException(400, f"step {idx} missing scenario_id")
        scenario = _find_k8s_fault_scenario(str(scenario_id)) if benchmark_type == "k8s" else _find_llm_fault_scenario(str(scenario_id))
        if not scenario:
            raise HTTPException(404, f"scenario '{scenario_id}' not found")
        action = raw_step.get("action") or "experiment"
        if action not in {"experiment", "inject"}:
            raise HTTPException(400, f"step {idx} action must be 'experiment' or 'inject'")
        hold_s = int(raw_step.get("hold_s", raw_step.get("duration_s", 30)))
        wait_after_s = int(raw_step.get("wait_after_s", body.get("default_wait_after_s", 5)))
        if hold_s < 0 or wait_after_s < 0:
            raise HTTPException(400, f"step {idx} duration values must be non-negative")
        fault_type = scenario.get("fault_type")
        if not dry_run and benchmark_type == "llm" and fault_type in _HIGH_RISK_LLM_FAULT_TYPES and not common["risk_ack"]:
            raise HTTPException(
                400,
                f"Step {idx} fault type '{fault_type}' is high risk and requires risk_ack=true.",
            )
        normalized_steps.append({
            "index": idx,
            "benchmark_type": benchmark_type,
            "scenario_id": str(scenario_id),
            "scenario_name": scenario.get("name"),
            "fault_type": fault_type,
            "layer": scenario.get("layer"),
            "category": scenario.get("category"),
            "action": action,
            "hold_s": hold_s,
            "wait_after_s": wait_after_s,
            "baseline_samples": int(raw_step.get("baseline_samples", common["baseline_samples"])),
            "fault_samples": int(raw_step.get("fault_samples", common["fault_samples"])),
            "sample_interval": float(raw_step.get("sample_interval", common["sample_interval"])),
        })
    return {
        "name": body.get("name") or f"Live Fault Benchmark {time.strftime('%Y%m%d-%H%M%S')}",
        "business_system": body.get("business_system") or ("social-network" if benchmark_type == "k8s" else "llm-inference"),
        "benchmark_type": benchmark_type,
        "dry_run": dry_run,
        "target_mode": target_mode,
        "total_duration_s": total_duration_s,
        "max_total_duration_s": max_total_duration_s,
        "workload": body.get("workload") or {"type": "external", "enabled": False},
        "collector": body.get("collector") or {"prometheus": True, "vllm_metrics": True},
        "common_body": common,
        "steps": normalized_steps,
    }


async def _sleep_with_cancel(seconds: int, cancel_event: asyncio.Event, run: Dict[str, Any]) -> None:
    """Sleep in small increments so a running benchmark can be cancelled."""
    deadline = time.time() + max(0, seconds)
    while time.time() < deadline:
        if cancel_event.is_set():
            run["cancel_requested"] = True
            raise asyncio.CancelledError()
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.time())))


async def _run_k8s_faultlab_action(
    action: str,
    scenario: Dict[str, Any],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a general-compute K8s faultlab action with dry-run safety by default."""
    if action not in {"inject", "cleanup"}:
        raise HTTPException(400, "K8s live benchmark action must be inject or cleanup")
    dry_run = bool(body.get("dry_run", True))
    cluster_id = body.get("cluster_id") or None
    namespace_override = (body.get("namespace") or "").strip() or None
    commands_key = "commands" if action == "inject" else "cleanup"
    commands = _prepare_faultlab_commands(
        scenario,
        cluster_id,
        namespace_override,
        commands_key,
        bool(body.get("background_load")) and action == "inject",
    )
    payload = {
        "status": "dry_run" if dry_run else "executed",
        "action": action,
        "benchmark_type": "k8s",
        "scenario": scenario,
        "cluster_id": cluster_id,
        "namespace": namespace_override or scenario.get("inject", {}).get("namespace"),
        "commands": commands,
    }
    if dry_run:
        return payload
    if not bool(body.get("confirm")):
        raise HTTPException(400, "Real K8s benchmark execution requires confirm=true")
    if not bool(body.get("experiment_ack")):
        raise HTTPException(400, "Real K8s benchmark execution requires experiment_ack=true")
    results = [await asyncio.to_thread(_shell_sync, cmd, 120) for cmd in commands]
    payload["results"] = results
    payload["success"] = all(r.get("ok") for r in results)
    return payload


async def _execute_fault_experiment(run_id: str) -> None:
    """Run a live fault benchmark timeline in the background."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        return

    cancel_event = _state["fault_experiment_cancel"].setdefault(run_id, asyncio.Event())
    spec = run["spec"]
    started_at = time.time()
    run.update({
        "status": "running",
        "started_at": started_at,
        "timeline": run.get("timeline") or [],
        "results": run.get("results") or [],
    })
    _save_fault_experiment(run)

    async def add_event(phase: str, message: str, **extra: Any) -> None:
        run["timeline"].append({
            "ts": time.time(),
            "phase": phase,
            "message": message,
            **extra,
        })
        _save_fault_experiment(run)

    try:
        await add_event("start", "live benchmark started", dry_run=spec["dry_run"])
        for step in spec["steps"]:
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            benchmark_type = step.get("benchmark_type") or spec.get("benchmark_type", "llm")
            scenario = _find_k8s_fault_scenario(step["scenario_id"]) if benchmark_type == "k8s" else _find_llm_fault_scenario(step["scenario_id"])
            if not scenario:
                raise RuntimeError(f"scenario '{step['scenario_id']}' not found")

            step_body = {
                **spec["common_body"],
                "baseline_samples": step["baseline_samples"],
                "fault_samples": step["fault_samples"],
                "sample_interval": step["sample_interval"],
            }
            step_started = time.time()
            await add_event(
                "step_start",
                f"step {step['index']} {step['action']} {step['scenario_id']}",
                step=step,
            )

            if step["action"] == "experiment":
                if benchmark_type == "k8s":
                    inject_result = await _run_k8s_faultlab_action("inject", scenario, step_body)
                    await add_event("hold", f"holding injected fault for {step['hold_s']}s", step=step)
                    try:
                        await _sleep_with_cancel(step["hold_s"], cancel_event, run)
                    finally:
                        cleanup_result = await _run_k8s_faultlab_action("cleanup", scenario, step_body)
                    result = {
                        "status": "dry_run" if spec["dry_run"] else "executed",
                        "benchmark_type": "k8s",
                        "inject_result": inject_result,
                        "cleanup_result": cleanup_result,
                    }
                else:
                    result = await _run_llm_fault("experiment", scenario, step_body)
                run["results"].append({
                    "step": step,
                    "action": "experiment",
                    "started_at": step_started,
                    "finished_at": time.time(),
                    "result": result,
                })
            else:
                inject_result = await (_run_k8s_faultlab_action("inject", scenario, step_body) if benchmark_type == "k8s" else _run_llm_fault("inject", scenario, step_body))
                await add_event("hold", f"holding injected fault for {step['hold_s']}s", step=step)
                cleanup_result: Dict[str, Any]
                try:
                    await _sleep_with_cancel(step["hold_s"], cancel_event, run)
                finally:
                    cleanup_result = await (_run_k8s_faultlab_action("cleanup", scenario, step_body) if benchmark_type == "k8s" else _run_llm_fault("cleanup", scenario, step_body))
                run["results"].append({
                    "step": step,
                    "action": "inject",
                    "started_at": step_started,
                    "finished_at": time.time(),
                    "inject_result": inject_result,
                    "cleanup_result": cleanup_result,
                })

            await add_event(
                "step_done",
                f"step {step['index']} completed",
                step_index=step["index"],
                duration_s=round(time.time() - step_started, 3),
            )
            await _sleep_with_cancel(step["wait_after_s"], cancel_event, run)

            if spec["total_duration_s"] and time.time() - started_at >= spec["total_duration_s"]:
                await add_event("time_limit", "total_duration_s reached")
                break

        run["status"] = "completed"
        await add_event("completed", "live benchmark completed")
    except asyncio.CancelledError:
        run["status"] = "cancelled"
        await add_event("cancelled", "live benchmark cancelled")
    except Exception as e:
        logger.exception("Fault experiment failed")
        run["status"] = "failed"
        run["error"] = str(e)
        await add_event("failed", str(e))
    finally:
        run["finished_at"] = time.time()
        run["duration_s"] = round(run["finished_at"] - started_at, 3)
        artifact_path = _write_fault_experiment_artifact(run)
        if artifact_path:
            run["artifact_path"] = artifact_path
        _save_fault_experiment(run)
        _state["fault_experiment_tasks"].pop(run_id, None)
        _state["fault_experiment_cancel"].pop(run_id, None)


def _build_kubectl_cmd(cluster_id: Optional[str] = None) -> str:
    """Return kubectl command usable by the vLLM injector.

    When cluster_id is provided, includes --kubeconfig / --context flags so the
    command targets that cluster profile instead of the container default.
    """
    cfg = _get_config()
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    flags = ""
    if cluster:
        if cluster.get("kubeconfig"):
            flags += f" --kubeconfig={shlex.quote(cluster['kubeconfig'])}"
        if cluster.get("context"):
            flags += f" --context={shlex.quote(cluster['context'])}"
    kubectl = f"kubectl{flags}"
    if cfg.kubernetes.use_ssh and cfg.kubernetes.ssh_jump_host:
        ssh_target = cfg.kubernetes.ssh_target or cfg.kubernetes.target_host
        return (
            f"ssh -J {shlex.quote(cfg.kubernetes.ssh_jump_host)} "
            f"{shlex.quote(ssh_target)} {kubectl}"
        )
    return kubectl


def _build_host_ssh_prefix(host: Dict[str, Any]) -> str:
    """Build the SSH command prefix to reach an LLM GPU host."""
    ssh_user = host.get("ssh_user", "root")
    ssh_key = host.get("ssh_key_path", "")
    jump = host.get("jump_host", "")
    target = f"{ssh_user}@{host['host']}"
    parts = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"]
    if ssh_key:
        parts += ["-i", shlex.quote(ssh_key)]
    if jump:
        parts += ["-J", shlex.quote(jump)]
    parts.append(shlex.quote(target))
    return " ".join(parts)


async def _run_llm_fault_on_host(
    action: str,
    scenario: Dict[str, Any],
    body: Dict[str, Any],
    host: Dict[str, Any],
) -> Dict[str, Any]:
    """Drive the vLLM fault injector remotely over SSH on a bare-metal GPU host.

    The command is built locally and executed over SSH, so the target host does
    not need to have the Python fault-injector package installed.
    """
    dry_run = bool(body.get("dry_run", True))
    fault_type = scenario.get("fault_type") or scenario.get("id") or ""
    interface = body.get("interface") or "eth0"
    endpoint = body.get("endpoint") or "http://127.0.0.1:8000/v1/completions"
    model_dir = body.get("model_dir") or ""

    sys.path.insert(0, str(LLM_FAULT_INJECTOR_DIR.parent))
    try:
        from vllm_fault_injector.host_faults import build_host_fault_command
    except Exception as e:
        logger.exception("Failed to import host fault command builder")
        return {
            "status": "import_error",
            "action": action,
            "scenario": scenario,
            "target_mode": "host",
            "host": host,
            "message": str(e),
        }

    built = build_host_fault_command(
        fault_type,
        action,
        dry_run=False,
        interface=interface,
        endpoint=endpoint,
        model_dir=model_dir,
    )
    if not built.supported:
        return {
            "status": "unsupported",
            "action": action,
            "scenario": scenario,
            "target_mode": "host",
            "host": host,
            "message": built.message,
            "fault_type": fault_type,
        }
    _require_fault_execution_approval(
        body=body,
        target_mode="host",
        action=action,
        fault_type=fault_type,
    )

    ssh_prefix = _build_host_ssh_prefix(host)
    remote_cmd = built.command
    full_cmd = f"{ssh_prefix} {shlex.quote(remote_cmd)}"
    started_at = time.time()

    if dry_run:
        return {
            "status": "dry_run",
            "action": action,
            "scenario": scenario,
            "target_mode": "host",
            "host": host,
            "fault_type": fault_type,
            "layer": built.layer,
            "message": built.message,
            "remote_command": remote_cmd,
            "recovery_command": built.recovery_command,
            "command": full_cmd,
        }

    result = await asyncio.to_thread(_shell_sync, full_cmd, 300)
    payload = {
        "status": "executed" if result.get("ok") else "failed",
        "action": action,
        "scenario": scenario,
        "target_mode": "host",
        "host": host,
        "fault_type": fault_type,
        "layer": built.layer,
        "message": built.message,
        "remote_command": remote_cmd,
        "recovery_command": built.recovery_command,
        "command": result.get("command"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
        "returncode": result.get("returncode"),
    }
    _append_fault_run({
        "id": f"fault-{uuid.uuid4().hex[:10]}",
        "started_at": started_at,
        "finished_at": time.time(),
        "status": payload["status"],
        "target_mode": "host",
        "host_id": host.get("id"),
        "host": host.get("host"),
        "action": action,
        "scenario_id": scenario.get("id"),
        "fault_type": fault_type,
        "layer": built.layer,
        "remote_command": remote_cmd,
        "recovery_command": built.recovery_command,
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
    })
    return payload


async def _run_llm_fault(
    action: str,
    scenario: Dict[str, Any],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a vLLM fault action with dry-run as the default safety mode.

    Body fields:
      - target_mode: "k8s" (default) or "host"
      - cluster_id: K8s cluster profile id (k8s mode only)
      - host_id: GPU host profile id (host mode only)
      - namespace / deployment: passed through to FaultInjector (k8s mode)
    """
    dry_run = bool(body.get("dry_run", True))
    target_mode = body.get("target_mode", "k8s")

    if target_mode == "host":
        host_id = body.get("host_id")
        host = _find_llm_host(host_id)
        if not host:
            raise HTTPException(400, f"llm host '{host_id}' not found")
        return await _run_llm_fault_on_host(action, scenario, body, host)

    cluster_id = body.get("cluster_id") or None
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    namespace = body.get("namespace") or (cluster.get("default_namespace") if cluster else None) or "default"
    deployment = body.get("deployment") or "vllm-server"
    fault_type = scenario.get("fault_type")
    layer = scenario.get("layer") or "all"
    baseline_samples = int(body.get("baseline_samples", 2))
    fault_samples = int(body.get("fault_samples", 3))
    sample_interval = float(body.get("sample_interval", 3.0))
    _require_fault_execution_approval(
        body=body,
        target_mode="k8s",
        action=action,
        fault_type=fault_type,
    )

    status = _llm_fault_tool_status()
    if not status["available"]:
        return {
            "status": "unavailable",
            "message": "vLLM fault injector tool not found. Set VLLM_FAULT_INJECTOR_DIR or deploy the tool package.",
            "tool": status,
            "scenario": scenario,
            "dry_run": dry_run,
            "target_mode": "k8s",
            "cluster_id": cluster_id,
        }

    sys.path.insert(0, str(LLM_FAULT_INJECTOR_DIR.parent))
    try:
        from vllm_fault_injector.injector import FaultInjector
    except Exception as e:
        logger.exception("Failed to import vLLM fault injector")
        return {
            "status": "import_error",
            "message": str(e),
            "tool": status,
            "scenario": scenario,
            "dry_run": dry_run,
            "target_mode": "k8s",
            "cluster_id": cluster_id,
        }

    injector = FaultInjector(
        kubectl_cmd=_build_kubectl_cmd(cluster_id),
        namespace=namespace,
        deployment=deployment,
        dry_run=dry_run,
        prometheus_url=_discover_prometheus_url() or "http://localhost:9090",
    )

    base_meta = {
        "target_mode": "k8s",
        "cluster_id": cluster_id,
        "namespace": namespace,
        "deployment": deployment,
    }

    try:
        started_at = time.time()
        if action == "cleanup":
            results = await injector.recover(layer if layer in {"software", "driver", "network", "os"} else "all")
            payload = {
                "status": "dry_run" if dry_run else "executed",
                "action": "cleanup",
                "scenario": scenario,
                **base_meta,
                "results": [getattr(r, "__dict__", str(r)) for r in results],
            }
            if not dry_run:
                _append_fault_run({
                    "id": f"fault-{uuid.uuid4().hex[:10]}",
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "status": payload["status"],
                    "target_mode": "k8s",
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "deployment": deployment,
                    "action": "cleanup",
                    "scenario_id": scenario.get("id"),
                    "fault_type": fault_type,
                    "layer": layer,
                    "results": payload["results"],
                })
            return payload
        if action == "experiment":
            result = await injector.run_experiment(
                fault_type,
                baseline_samples=baseline_samples,
                fault_samples=fault_samples,
                sample_interval=sample_interval,
            )
            payload = {
                "status": "dry_run" if dry_run else "executed",
                "action": "experiment",
                "scenario": scenario,
                **base_meta,
                "result": result,
            }
            if not dry_run:
                _append_fault_run({
                    "id": f"fault-{uuid.uuid4().hex[:10]}",
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "status": payload["status"],
                    "target_mode": "k8s",
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "deployment": deployment,
                    "action": "experiment",
                    "scenario_id": scenario.get("id"),
                    "fault_type": fault_type,
                    "layer": layer,
                    "result": result,
                })
            return payload
        result = await injector.inject(fault_type)
        payload = {
            "status": "dry_run" if dry_run else "executed",
            "action": "inject",
            "scenario": scenario,
            **base_meta,
            "result": getattr(result, "__dict__", str(result)),
        }
        if not dry_run:
            _append_fault_run({
                "id": f"fault-{uuid.uuid4().hex[:10]}",
                "started_at": started_at,
                "finished_at": time.time(),
                "status": payload["status"],
                "target_mode": "k8s",
                "cluster_id": cluster_id,
                "namespace": namespace,
                "deployment": deployment,
                "action": "inject",
                "scenario_id": scenario.get("id"),
                "fault_type": fault_type,
                "layer": layer,
                "result": payload["result"],
            })
        return payload
    except Exception as e:
        logger.exception("vLLM fault action failed")
        raise HTTPException(500, str(e))


@app.get("/api/sow/alignment")
async def sow_alignment():
    """Current implementation alignment with the SoW acceptance points."""
    cfg = _get_config()
    kb = _get_fault_store().stats()
    hitl = _get_human_review_store().stats()
    evo = _get_evolution_tracker().get_evolution_report(fault_store=_get_fault_store())
    scenarios = _load_fault_scenarios()
    categories = sorted({s.get("category", "unknown") for s in scenarios})
    fault_types = sorted({s.get("fault_type", "unknown") for s in scenarios})
    return {
        "items": [
            {
                "requirement": "智算/通算专用运维智能体",
                "implementation": "告警、指标、日志、调用链、事件、Profiling、规划、修复智能体",
                "status": "implemented",
            },
            {
                "requirement": "告警压缩与根因推荐准确度 >= 80%",
                "implementation": "AlertAgent + E2E 场景评分；需以集群评测结果持续刷新",
                "status": "verifying",
            },
            {
                "requirement": "多智能体协作范式与架构选型",
                "implementation": "chain/react/reflection/plan_and_execute/debate/voting 对比评测",
                "status": "implemented",
            },
            {
                "requirement": "行为可观测与验证",
                "implementation": "TraceStore、RCAJudge、BehaviorValidator、Web SSE 事件流",
                "status": "implemented",
            },
            {
                "requirement": "反馈机制与历史轨迹增量更新上下文",
                "implementation": "FaultContextStore、ExpertFeedback、HITL 队列、EvolutionTracker",
                "status": "implemented",
            },
            {
                "requirement": "多轮反馈后根因定位准确率提升 >= 10%",
                "implementation": "enriched/baseline 评测模式 + 演化快照趋势",
                "status": "verifying",
            },
        ],
        "signals": {
            "memory": kb,
            "hitl": hitl,
            "evolution": evo,
            "fault_scenarios": {
                "count": len(scenarios),
                "categories": categories,
                "fault_types": fault_types,
            },
            "llm_fault_scenarios": _llm_fault_tool_status(),
            "config": {
                "memory_enabled": cfg.memory.enabled,
                "evolution_enabled": cfg.evolution.enabled,
                "hitl_enabled": True,
                "recovery_requires_approval": cfg.remediation.require_approval,
            },
        },
    }


# ─────────────────────────────────────────
# Fault Target Profiles (clusters + GPU hosts)
# ─────────────────────────────────────────

_FAULT_TARGET_REQUIRED_FIELDS = {
    "k8s_clusters": ("id", "name"),
    "llm_hosts": ("id", "name", "host"),
}


def _validate_target_list(kind: str, items: Any) -> List[Dict[str, Any]]:
    """Validate and normalize a list of target profile dicts."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise HTTPException(400, f"{kind} must be a list")
    required = _FAULT_TARGET_REQUIRED_FIELDS[kind]
    seen_ids = set()
    cleaned: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise HTTPException(400, f"{kind} entries must be objects")
        for field_name in required:
            if not raw.get(field_name):
                raise HTTPException(400, f"{kind} entry missing '{field_name}'")
        item_id = raw["id"]
        if item_id in seen_ids:
            raise HTTPException(400, f"{kind} duplicate id '{item_id}'")
        seen_ids.add(item_id)
        cleaned.append({k: v for k, v in raw.items() if v is not None})
    return cleaned


@app.get("/api/fault-targets")
async def get_fault_targets():
    """Return all fault target profiles (K8s clusters + GPU hosts)."""
    cfg = _get_config()
    return {
        "k8s_clusters": list(cfg.fault_targets.k8s_clusters),
        "llm_hosts": list(cfg.fault_targets.llm_hosts),
    }


@app.put("/api/fault-targets")
async def update_fault_targets(request: Request):
    """Replace fault target profiles and persist to data/cluster_profiles.json."""
    body = await request.json()
    clusters = _validate_target_list("k8s_clusters", body.get("k8s_clusters"))
    hosts = _validate_target_list("llm_hosts", body.get("llm_hosts"))
    cfg = _get_config()
    cfg.fault_targets.k8s_clusters = clusters
    cfg.fault_targets.llm_hosts = hosts
    _save_fault_targets()
    return {
        "status": "ok",
        "k8s_clusters": clusters,
        "llm_hosts": hosts,
    }


@app.get("/api/fault-targets/k8s/{cluster_id}/discover")
async def discover_k8s_cluster(cluster_id: str):
    """Probe a K8s cluster profile: list nodes + namespaces using its kubeconfig/context."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    ns_result = await asyncio.to_thread(
        _shell_sync,
        "kubectl get ns -o jsonpath='{.items[*].metadata.name}'",
        20,
        cluster_id,
    )
    nodes_result = await asyncio.to_thread(
        _shell_sync,
        "kubectl get nodes -o jsonpath='{.items[*].metadata.name}'",
        20,
        cluster_id,
    )
    return {
        "cluster": cluster,
        "namespaces": (ns_result.get("stdout") or "").replace("'", "").split() if ns_result.get("ok") else [],
        "nodes": (nodes_result.get("stdout") or "").replace("'", "").split() if nodes_result.get("ok") else [],
        "ns_probe": ns_result,
        "nodes_probe": nodes_result,
    }


@app.post("/api/fault-targets/llm-host/{host_id}/probe")
async def probe_llm_host(host_id: str):
    """SSH-probe a GPU host for reachability and fault-injection capabilities."""
    host = _find_llm_host(host_id)
    if not host:
        raise HTTPException(404, f"llm host '{host_id}' not found")
    endpoint = "http://127.0.0.1:8000/v1/models"
    remote_probe = (
        "printf 'HOST='; hostname; "
        "printf 'GPU='; nvidia-smi -L 2>/dev/null | head -20 || true; "
        "printf 'TOOLS='; for t in python3 tc iptables nvidia-smi curl; do command -v $t >/dev/null 2>&1 && printf \"$t:yes \" || printf \"$t:no \"; done; echo; "
        "printf 'IFACES='; (ls /sys/class/net 2>/dev/null || ip -o link show 2>/dev/null | awk -F': ' '{print $2}') | tr '\\n' ' '; echo; "
        f"printf 'ENDPOINT='; curl -fsS --max-time 3 {shlex.quote(endpoint)} >/dev/null 2>&1 && echo ok || echo unavailable"
    )
    cmd = f"{_build_host_ssh_prefix(host)} {shlex.quote(remote_probe)}"
    result = await asyncio.to_thread(_shell_sync, cmd, 25)
    stdout = result.get("stdout") or ""
    tools: Dict[str, bool] = {}
    interfaces: List[str] = []
    endpoint_ok = False
    for line in stdout.splitlines():
        if line.startswith("TOOLS="):
            for item in line[len("TOOLS="):].split():
                if ":" in item:
                    k, v = item.split(":", 1)
                    tools[k] = v == "yes"
        elif line.startswith("IFACES="):
            interfaces = [x for x in line[len("IFACES="):].split() if x]
        elif line.startswith("ENDPOINT="):
            endpoint_ok = line.strip().endswith("ok")
    capability = {
        "network_faults": bool(tools.get("tc")),
        "gpu_faults": bool(tools.get("nvidia-smi") and tools.get("python3")),
        "load_faults": bool(tools.get("python3")),
        "endpoint_probe": endpoint_ok,
    }
    return {
        "host": host,
        "command": result.get("command"),
        "ok": result.get("ok"),
        "tools": tools,
        "interfaces": interfaces,
        "capability": capability,
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
    }


@app.get("/api/fault-runs")
async def fault_runs(limit: int = 50):
    """Return recent real fault injection and cleanup records."""
    runs = _load_fault_runs()
    return {"runs": runs[: max(1, min(limit, 200))], "total": len(runs)}


@app.get("/api/faultlab/scenarios")
async def faultlab_scenarios():
    """List available general-compute K8s fault experiment scenarios."""
    return {"family": "通算故障实验", "scenarios": _load_fault_scenarios()}


def _apply_ns_override(cmd: str, old_ns: str, new_ns: str) -> str:
    """Replace -n <old> / --namespace=<old> occurrences in a kubectl command."""
    if not new_ns or not old_ns or new_ns == old_ns:
        return cmd
    cmd = re.sub(rf'(-n\s+){re.escape(old_ns)}\b', rf'\g<1>{new_ns}', cmd)
    cmd = re.sub(rf'(--namespace[=\s]+){re.escape(old_ns)}\b', rf'\g<1>{new_ns}', cmd)
    return cmd


def _prepare_faultlab_commands(
    scenario: Dict[str, Any],
    cluster_id: Optional[str],
    namespace_override: Optional[str],
    commands_key: str,
    background_load: bool,
) -> List[str]:
    """Apply cluster + namespace overrides to scenario inject/cleanup commands."""
    inject = scenario.get("inject", {})
    raw_commands = list(inject.get(commands_key, []))
    scenario_ns = inject.get("namespace", "")
    effective_ns = namespace_override or scenario_ns

    commands: List[str] = []
    if background_load and commands_key == "commands":
        load_command = (
            "kubectl -n %s create job aiopslab-bg-load-%s "
            "--image=busybox -- /bin/sh -c "
            "'for i in $(seq 1 180); do wget -q -O /dev/null --timeout=2 "
            "http://nginx-thrift:8080/wrk2-api/home-timeline/read?start=0\\&stop=10 || true; sleep 1; done'"
        ) % (effective_ns or "default", int(time.time()))
        commands.append(load_command)
    commands.extend(raw_commands)

    if namespace_override and scenario_ns:
        commands = [_apply_ns_override(c, scenario_ns, namespace_override) for c in commands]
    if cluster_id:
        commands = [_apply_cluster_to_cmd(c, cluster_id) for c in commands]
    return commands


@app.post("/api/faultlab/inject/{scenario_id}")
async def faultlab_inject(scenario_id: str, request: Request):
    """Inject one predefined scenario. Defaults to dry_run=true.

    Body fields (all optional):
      - dry_run (bool, default true)
      - background_load (bool, default false)
      - cluster_id (str): K8s cluster profile id (rewrites kubectl context/kubeconfig)
      - namespace (str): override the scenario's default namespace
    """
    body = await request.json()
    dry_run = body.get("dry_run", True)
    background_load = body.get("background_load", False)
    cluster_id = body.get("cluster_id") or None
    namespace_override = (body.get("namespace") or "").strip() or None
    scenarios = _load_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    commands = _prepare_faultlab_commands(
        scenario, cluster_id, namespace_override, "commands", background_load,
    )

    if dry_run:
        return {
            "status": "dry_run",
            "scenario": scenario,
            "cluster_id": cluster_id,
            "namespace": namespace_override or scenario.get("inject", {}).get("namespace"),
            "commands": commands,
            "background_load": background_load,
        }

    results = [await asyncio.to_thread(_shell_sync, cmd, 120) for cmd in commands]
    return {
        "status": "executed",
        "scenario_id": scenario_id,
        "cluster_id": cluster_id,
        "namespace": namespace_override or scenario.get("inject", {}).get("namespace"),
        "results": results,
        "background_load": background_load,
    }


@app.post("/api/faultlab/cleanup/{scenario_id}")
async def faultlab_cleanup(scenario_id: str, request: Request):
    """Run cleanup commands for one predefined scenario."""
    body = await request.json()
    dry_run = body.get("dry_run", True)
    cluster_id = body.get("cluster_id") or None
    namespace_override = (body.get("namespace") or "").strip() or None
    scenarios = _load_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    commands = _prepare_faultlab_commands(
        scenario, cluster_id, namespace_override, "cleanup", False,
    )
    if dry_run:
        return {
            "status": "dry_run",
            "scenario": scenario,
            "cluster_id": cluster_id,
            "namespace": namespace_override or scenario.get("inject", {}).get("namespace"),
            "commands": commands,
        }

    results = [await asyncio.to_thread(_shell_sync, cmd, 120) for cmd in commands]
    return {
        "status": "executed",
        "scenario_id": scenario_id,
        "cluster_id": cluster_id,
        "results": results,
    }


@app.get("/api/llm-faultlab/scenarios")
async def llm_faultlab_scenarios():
    """List vLLM/LLM inference fault experiment scenarios."""
    scenarios = _load_llm_fault_scenarios()
    tool_status = _llm_fault_tool_status()
    return {
        "family": "智算故障实验",
        "tool": tool_status,
        "environment": tool_status.get("environment"),
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }


@app.post("/api/llm-faultlab/inject/{scenario_id}")
async def llm_faultlab_inject(scenario_id: str, request: Request):
    """Inject one vLLM inference fault scenario. Defaults to dry_run=true."""
    body = await request.json()
    scenarios = _load_llm_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    return await _run_llm_fault("inject", scenario, body)


@app.post("/api/llm-faultlab/experiment/{scenario_id}")
async def llm_faultlab_experiment(scenario_id: str, request: Request):
    """Run baseline -> inject -> measure -> recover for one vLLM scenario."""
    body = await request.json()
    scenarios = _load_llm_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    return await _run_llm_fault("experiment", scenario, body)


@app.post("/api/llm-faultlab/cleanup/{scenario_id}")
async def llm_faultlab_cleanup(scenario_id: str, request: Request):
    """Recover the affected layer for one vLLM inference fault scenario."""
    body = await request.json()
    scenarios = _load_llm_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    return await _run_llm_fault("cleanup", scenario, body)


@app.get("/api/fault-experiments")
async def fault_experiments(limit: int = 50):
    """List live fault benchmark experiments."""
    runs = _load_fault_experiments()
    trimmed = runs[: max(1, min(limit, 200))]
    return {"experiments": trimmed, "total": len(runs)}


@app.post("/api/fault-experiments")
async def create_fault_experiment(request: Request):
    """Create a live benchmark spec without starting it."""
    body = await request.json()
    spec = _normalize_fault_experiment_body(body)
    run_id = f"exp-{uuid.uuid4().hex[:10]}"
    run = {
        "id": run_id,
        "name": spec["name"],
        "status": "created",
        "created_at": time.time(),
        "updated_at": time.time(),
        "spec": spec,
        "timeline": [],
        "results": [],
        "artifact_path": None,
    }
    _save_fault_experiment(run)
    return run


@app.post("/api/fault-experiments/start")
async def create_and_start_fault_experiment(request: Request):
    """Create and immediately start a live benchmark experiment."""
    body = await request.json()
    spec = _normalize_fault_experiment_body(body)
    run_id = f"exp-{uuid.uuid4().hex[:10]}"
    run = {
        "id": run_id,
        "name": spec["name"],
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "spec": spec,
        "timeline": [],
        "results": [],
        "artifact_path": None,
    }
    _save_fault_experiment(run)
    _state["fault_experiment_cancel"][run_id] = asyncio.Event()
    task = asyncio.create_task(_execute_fault_experiment(run_id))
    _state["fault_experiment_tasks"][run_id] = task
    return run


@app.post("/api/fault-experiments/{run_id}/start")
async def start_fault_experiment(run_id: str):
    """Start a previously created live benchmark experiment."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    if run.get("status") in {"running", "queued"}:
        return run
    if run.get("status") not in {"created", "failed", "cancelled"}:
        raise HTTPException(400, f"Experiment status '{run.get('status')}' cannot be started")
    run["status"] = "queued"
    run["updated_at"] = time.time()
    run["timeline"] = []
    run["results"] = []
    run["error"] = None
    _save_fault_experiment(run)
    _state["fault_experiment_cancel"][run_id] = asyncio.Event()
    task = asyncio.create_task(_execute_fault_experiment(run_id))
    _state["fault_experiment_tasks"][run_id] = task
    return run


@app.post("/api/fault-experiments/{run_id}/cancel")
async def cancel_fault_experiment(run_id: str):
    """Request cancellation for a running live benchmark."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    event = _state["fault_experiment_cancel"].get(run_id)
    if event:
        event.set()
    run["cancel_requested"] = True
    run["updated_at"] = time.time()
    if run.get("status") in {"created", "queued"}:
        run["status"] = "cancelled"
    _save_fault_experiment(run)
    return run


@app.get("/api/fault-experiments/{run_id}")
async def get_fault_experiment(run_id: str):
    """Return one live benchmark run."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    return run


@app.get("/api/fault-experiments/{run_id}/artifact")
async def get_fault_experiment_artifact(run_id: str):
    """Return the saved artifact for a completed benchmark run."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    artifact_path = run.get("artifact_path")
    if artifact_path and Path(artifact_path).exists():
        try:
            return json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(500, f"Failed to read artifact: {e}")
    return run


# ─────────────────────────────────────────
# Fault Report APIs (故障报告)
# ─────────────────────────────────────────

def _build_report(run: dict) -> dict:
    """构建完整的中文故障报告数据"""
    result = run.get("result") or {}
    inner = result.get("result", result) if isinstance(result, dict) else result
    rca = (inner.get("result") if isinstance(inner, dict) and isinstance(inner.get("result"), dict) else inner) or {}

    events = run.get("events", [])
    hypotheses = []
    evidence_list = []
    judge_data = None
    remediation_data = None
    for evt in events:
        ename = evt.get("event", "")
        if ename == "hypotheses":
            hypotheses = evt.get("items", [])
        elif ename == "evidence":
            evidence_list.append({"agent": evt.get("agent", ""), "summary": evt.get("summary", ""), "success": evt.get("success", False)})
        elif ename == "judge":
            judge_data = evt.get("data")
        elif ename == "remediation":
            remediation_data = evt.get("data")
        elif ename == "remediation_executed":
            remediation_data = evt.get("data")

    started = run.get("started_at", 0)

    return {
        "report_id": run.get("id", ""),
        "title": "故障诊断报告",
        "generated_at": time.time(),
        "basic_info": {
            "报告编号": run.get("id", ""),
            "开始时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "-",
            "故障描述": run.get("query", ""),
            "命名空间": run.get("namespace", "（全部）"),
            "分析状态": run.get("status", "unknown"),
            "耗时(秒)": round(rca.get("duration_s", 0) or (time.time() - started if started else 0), 1),
        },
        "detection": {
            "告警列表": [e.get("data", {}) for e in events if e.get("event") == "phase_complete" and e.get("phase") == 0],
        },
        "diagnosis": {
            "假设列表": [{"排名": i+1, "描述": h.get("description", ""), "置信度": f"{round(h.get('confidence', 0)*100)}%"} for i, h in enumerate(hypotheses)],
            "证据收集": [{"智能体": e["agent"], "结果": e["summary"], "成功": e["success"]} for e in evidence_list],
            "根因结论": rca.get("root_cause", "N/A"),
            "置信度": f"{round((rca.get('confidence', 0) or 0)*100)}%",
            "故障类型": rca.get("fault_type", ""),
            "受影响服务": rca.get("affected_services", []),
            "证据摘要": rca.get("evidence_summary", {}),
        },
        "remediation": {
            "状态": (remediation_data or {}).get("status", "未触发"),
            "修复方案": (remediation_data or {}).get("plan", {}),
            "执行结果": (remediation_data or {}).get("actions", []),
        },
        "timeline": rca.get("timeline", []),
        "quality": {
            "评级": (judge_data or {}).get("judge_level", ""),
            "评分": (judge_data or {}).get("combined_score", 0),
            "需要人工复核": (judge_data or {}).get("needs_review", False),
        },
        "suggestions": {
            "修复建议": rca.get("remediation_suggestion", ""),
            "预防措施": rca.get("prevention", ""),
        },
        "logs": run.get("logs", [])[-50:],
    }


@app.get("/api/report/{run_id}")
async def get_report(run_id: str):
    """生成故障诊断报告 JSON"""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    return _build_report(run)


@app.get("/api/report/{run_id}/export", response_class=HTMLResponse)
async def export_report(run_id: str):
    """导出故障报告为自包含 HTML（可打印/另存为）"""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    report = _build_report(run)
    bi = report["basic_info"]
    diag = report["diagnosis"]
    rem = report["remediation"]
    qual = report["quality"]
    sugg = report["suggestions"]

    def esc(s):
        if not isinstance(s, str):
            s = str(s) if s is not None else ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Build hypotheses table
    hyp_rows = ""
    for h in diag.get("假设列表", []):
        hyp_rows += f'<tr><td>{h["排名"]}</td><td>{esc(h["描述"])}</td><td>{h["置信度"]}</td></tr>'

    # Build evidence table
    ev_rows = ""
    for e in diag.get("证据收集", []):
        status = "成功" if e["成功"] else "失败"
        ev_rows += f'<tr><td>{esc(e["智能体"])}</td><td>{esc(e["结果"])}</td><td>{status}</td></tr>'

    # Build timeline
    tl_html = ""
    for t in report.get("timeline", []):
        tl_html += f'<div class="tl-item"><span class="tl-time">{esc(t.get("time",""))}</span><span>{esc(t.get("event",""))}</span></div>'

    # Build remediation
    rem_html = f'<p><strong>状态：</strong>{esc(rem["状态"])}</p>'
    for act in rem.get("执行结果", []):
        if isinstance(act, dict):
            rem_html += f'<div class="rem-item">{esc(act.get("description",""))} — {esc(act.get("status",""))}</div>'

    # Evidence summary
    ev_summary = ""
    for k, v in diag.get("证据摘要", {}).items():
        if v:
            ev_summary += f'<div class="ev-block"><strong>{esc(k)}:</strong> {esc(str(v)[:500])}</div>'

    # Affected services
    affected = ", ".join([str(s) for s in diag.get("受影响服务", [])]) or "未知"
    evidence_section = f"<h3>2.4 证据摘要</h3>{ev_summary}" if ev_summary else ""
    timeline_section = f"<h2>四、事件时间线</h2>{tl_html}" if tl_html else ""
    review_badge = ' &nbsp;&nbsp;<span class="badge badge-bronze">需要人工复核</span>' if qual.get("需要人工复核") else ""
    fix_suggestion = (
        f'<div class="suggestion"><strong>修复建议：</strong>{esc(sugg["修复建议"])}</div>'
        if sugg.get("修复建议") else '<p style="color:#94a3b8">无修复建议</p>'
    )
    prevention = (
        f'<div class="suggestion"><strong>预防措施：</strong>{esc(sugg["预防措施"])}</div>'
        if sugg.get("预防措施") else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>故障诊断报告 — {esc(bi['报告编号'])}</title>
<style>
  @media print {{ body {{ font-size: 12px; }} .no-print {{ display: none; }} }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; color: #1a2b42; padding: 40px; max-width: 900px; margin: 0 auto; line-height: 1.6; background: #fff; }}
  h1 {{ font-size: 24px; text-align: center; margin-bottom: 8px; color: #1e6fd9; }}
  .subtitle {{ text-align: center; color: #64748b; margin-bottom: 30px; font-size: 14px; }}
  h2 {{ font-size: 16px; color: #1e6fd9; border-bottom: 2px solid #1e6fd9; padding-bottom: 6px; margin: 24px 0 12px; }}
  h3 {{ font-size: 14px; color: #334155; margin: 16px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0 16px; font-size: 13px; }}
  th {{ background: #f0f4f8; padding: 8px 12px; text-align: left; border: 1px solid #e2e8f0; font-weight: 600; }}
  td {{ padding: 8px 12px; border: 1px solid #e2e8f0; }}
  .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0 16px; }}
  .info-item {{ padding: 8px 12px; background: #f8fafc; border-radius: 6px; font-size: 13px; }}
  .info-item strong {{ color: #1e6fd9; }}
  .root-cause {{ background: #f0f4f8; border-left: 4px solid #1e6fd9; padding: 16px; margin: 12px 0; border-radius: 0 6px 6px 0; font-size: 15px; }}
  .confidence {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 13px; font-weight: 600; }}
  .conf-high {{ background: #d1fae5; color: #065f46; }}
  .conf-mid {{ background: #fef3c7; color: #92400e; }}
  .conf-low {{ background: #fee2e2; color: #991b1b; }}
  .tl-item {{ display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid #f1f5f9; font-size: 13px; }}
  .tl-time {{ color: #64748b; min-width: 120px; font-weight: 600; }}
  .ev-block {{ padding: 8px 12px; background: #f8fafc; border-radius: 6px; margin: 4px 0; font-size: 13px; }}
  .rem-item {{ padding: 6px 12px; background: #eff6ff; border-radius: 4px; margin: 4px 0; font-size: 13px; }}
  .suggestion {{ background: #f0fdf4; border-left: 4px solid #10b981; padding: 12px 16px; margin: 8px 0; border-radius: 0 6px 6px 0; font-size: 13px; }}
  .print-btn {{ display: block; margin: 30px auto; padding: 10px 32px; background: #1e6fd9; color: #fff; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }}
  .print-btn:hover {{ background: #1558b0; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }}
  .badge-gold {{ background: #fef3c7; color: #d97706; }}
  .badge-silver {{ background: #f1f5f9; color: #64748b; }}
  .badge-bronze {{ background: #fee2e2; color: #ef4444; }}
</style>
</head>
<body>
<h1>故障诊断报告</h1>
<div class="subtitle">AgenticSRE 智能运维系统 — 自动生成</div>

<h2>一、基本信息</h2>
<div class="info-grid">
  <div class="info-item"><strong>报告编号：</strong>{esc(bi['报告编号'])}</div>
  <div class="info-item"><strong>开始时间：</strong>{esc(bi['开始时间'])}</div>
  <div class="info-item"><strong>分析状态：</strong>{esc(bi['分析状态'])}</div>
  <div class="info-item"><strong>耗时：</strong>{bi['耗时(秒)']} 秒</div>
</div>
<div class="info-item" style="margin-bottom:16px"><strong>故障描述：</strong>{esc(bi['故障描述'])}</div>

<h2>二、诊断阶段</h2>
<h3>2.1 假设列表</h3>
<table><thead><tr><th>排名</th><th>假设描述</th><th>置信度</th></tr></thead><tbody>{hyp_rows if hyp_rows else '<tr><td colspan="3" style="text-align:center;color:#94a3b8">无假设数据</td></tr>'}</tbody></table>

<h3>2.2 证据收集</h3>
<table><thead><tr><th>智能体</th><th>调查结果</th><th>状态</th></tr></thead><tbody>{ev_rows if ev_rows else '<tr><td colspan="3" style="text-align:center;color:#94a3b8">无证据数据</td></tr>'}</tbody></table>

<h3>2.3 根因结论</h3>
<div class="root-cause">{esc(diag['根因结论'])}</div>
<p style="margin:8px 0">
  <strong>置信度：</strong><span class="confidence conf-low">{diag.get('置信度','')}</span>
  &nbsp;&nbsp;<strong>故障类型：</strong>{esc(diag.get('故障类型',''))}
  &nbsp;&nbsp;<strong>受影响服务：</strong>{esc(affected)}
</p>

    {evidence_section}

<h2>三、自愈修复</h2>
{rem_html}

    {timeline_section}

<h2>{'五' if tl_html else '四'}、质量评估</h2>
<p>
  <strong>评级：</strong><span class="badge badge-{str(qual.get('评级','') or 'bronze').lower() if isinstance(qual.get('评级',''), str) else 'bronze'}">{esc(str(qual.get('评级','') or '未评估'))}</span>
  &nbsp;&nbsp;<strong>评分：</strong>{round(float(qual.get('评分',0) or 0), 3)}
      {review_badge}
</p>

<h2>{'六' if tl_html else '五'}、建议</h2>
    {fix_suggestion}
    {prevention}

<div style="text-align:center" class="no-print">
  <button class="print-btn" onclick="window.print()" style="display:inline-block;margin:30px 8px;padding:10px 28px;background:#1e6fd9;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer">打印 / 导出 PDF</button>
  <a href="/api/report/{run_id}/word" style="display:inline-block;margin:30px 8px;padding:10px 28px;background:#10b981;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;text-decoration:none">导出 Word 文档</a>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/api/report/{run_id}/word")
async def export_report_word(run_id: str):
    """导出故障报告为 Word (.docx) 文档，使用 Word-compatible HTML 格式"""
    from io import BytesIO

    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    report = _build_report(run)
    bi = report["basic_info"]
    diag = report["diagnosis"]
    rem = report["remediation"]
    qual = report["quality"]
    sugg = report["suggestions"]

    def esc(s):
        s = str(s) if s is not None else ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    affected = ", ".join([str(s) for s in diag.get("受影响服务", [])]) or "未知"

    hyp_rows = ""
    for h in diag.get("假设列表", []):
        hyp_rows += f'<tr><td>{h.get("排名","")}</td><td>{esc(h.get("描述",""))}</td><td>{h.get("置信度","")}</td></tr>'

    ev_rows = ""
    for e in diag.get("证据收集", []):
        ev_rows += f'<tr><td>{esc(e.get("智能体",""))}</td><td>{esc(e.get("结果",""))}</td><td>{"成功" if e.get("成功") else "失败"}</td></tr>'

    ev_summary = ""
    for k, v in diag.get("证据摘要", {}).items():
        if v:
            ev_summary += f'<p><b>{esc(k)}:</b> {esc(str(v)[:500])}</p>'

    tl_html = ""
    for t in report.get("timeline", []):
        tl_html += f'<p>{esc(t.get("time",""))}: {esc(t.get("event",""))}</p>'

    rem_html = f'<p><b>状态：</b>{esc(rem.get("状态","未触发"))}</p>'
    for act in rem.get("执行结果", []):
        if isinstance(act, dict):
            rem_html += f'<p>- {esc(act.get("description",""))} — {esc(act.get("status",""))}</p>'

    judge_level = str(qual.get("评级", "未评估") or "未评估")
    judge_score = 0
    try:
        judge_score = round(float(qual.get("评分", 0) or 0), 3)
    except Exception:
        pass

    word_html = f"""<html xmlns:o='urn:schemas-microsoft-com:office:office'
xmlns:w='urn:schemas-microsoft-com:office:word'
xmlns='http://www.w3.org/TR/REC-html40'>
<head><meta charset="UTF-8">
<!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View></w:WordDocument></xml><![endif]-->
<style>
body {{ font-family: '微软雅黑', 'Microsoft YaHei', SimSun, sans-serif; color: #1a2b42; padding: 30px; line-height: 1.8; font-size: 12pt; }}
h1 {{ font-size: 22pt; text-align: center; color: #1e6fd9; margin-bottom: 4px; }}
.subtitle {{ text-align: center; color: #888; margin-bottom: 24px; font-size: 10pt; }}
h2 {{ font-size: 14pt; color: #1e6fd9; border-bottom: 2px solid #1e6fd9; padding-bottom: 4px; margin-top: 20px; }}
h3 {{ font-size: 12pt; color: #334155; margin-top: 14px; }}
table {{ width: 100%; border-collapse: collapse; margin: 8px 0 14px; font-size: 10pt; }}
th {{ background: #f0f4f8; padding: 6px 10px; text-align: left; border: 1px solid #ccc; font-weight: bold; }}
td {{ padding: 6px 10px; border: 1px solid #ccc; }}
.root-cause {{ background: #f0f4f8; border-left: 4px solid #1e6fd9; padding: 12px; margin: 10px 0; font-size: 13pt; }}
.suggestion {{ background: #f0fdf4; border-left: 4px solid #10b981; padding: 10px 14px; margin: 6px 0; }}
</style>
</head>
<body>
<h1>故障诊断报告</h1>
<p class="subtitle">AgenticSRE 智能运维系统 — 自动生成</p>

<h2>一、基本信息</h2>
<table>
{''.join(f"<tr><th>{esc(k)}</th><td>{esc(str(v))}</td></tr>" for k, v in bi.items())}
</table>

<h2>二、诊断阶段</h2>
<h3>2.1 假设列表</h3>
<table><tr><th>排名</th><th>假设描述</th><th>置信度</th></tr>
{hyp_rows if hyp_rows else '<tr><td colspan="3" style="text-align:center;color:#999">无假设数据</td></tr>'}
</table>

<h3>2.2 证据收集</h3>
<table><tr><th>智能体</th><th>调查结果</th><th>状态</th></tr>
{ev_rows if ev_rows else '<tr><td colspan="3" style="text-align:center;color:#999">无证据数据</td></tr>'}
</table>

<h3>2.3 根因结论</h3>
<div class="root-cause">{esc(diag.get('根因结论', 'N/A'))}</div>
<p><b>置信度:</b> {esc(diag.get('置信度',''))} &nbsp; <b>故障类型:</b> {esc(diag.get('故障类型',''))} &nbsp; <b>受影响服务:</b> {esc(affected)}</p>

{f'<h3>2.4 证据摘要</h3>{ev_summary}' if ev_summary else ''}

<h2>三、自愈修复</h2>
{rem_html}

{f'<h2>四、事件时间线</h2>{tl_html}' if tl_html else ''}

<h2>{'五' if tl_html else '四'}、质量评估</h2>
<p><b>评级:</b> {esc(judge_level)} &nbsp; <b>评分:</b> {judge_score}{'  <b style="color:red">需要人工复核</b>' if qual.get('需要人工复核') else ''}</p>

<h2>{'六' if tl_html else '五'}、建议</h2>
{f'<div class="suggestion"><b>修复建议：</b>{esc(sugg.get("修复建议",""))}</div>' if sugg.get('修复建议') else '<p style="color:#999">无修复建议</p>'}
{f'<div class="suggestion"><b>预防措施：</b>{esc(sugg.get("预防措施",""))}</div>' if sugg.get('预防措施') else ''}

</body></html>"""

    buf = BytesIO(word_html.encode('utf-8'))
    filename = f"故障报告_{run_id}.doc"
    from urllib.parse import quote
    return StreamingResponse(
        buf,
        media_type="application/msword",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ─────────────────────────────────────────
# Hermes Agent Integration (NousResearch)
# ─────────────────────────────────────────

_hermes_sessions: Dict[str, Dict] = {}  # session_id → {agent, history, messages, status}


def _init_hermes_agent():
    """Lightweight init: the real Hermes Agent sub-project is not bundled.

    We fall back to a plain LLM chat via _run_hermes_sync, so the path
    just needs to be a truthy placeholder.
    """
    return "_local_llm_fallback", None


def _run_hermes_sync(hermes_path: str, message: str, history: list, cfg) -> dict:
    """Lightweight Hermes Agent fallback.

    Uses the configured LLMClient as a chat engine with an SRE-flavoured
    system prompt. Tool-calling is stubbed; the response is a plain text
    answer. This matches the upstream Hermes Agent JSON shape so the
    frontend works without code changes.
    """
    from tools.llm_client import LLMClient
    llm = LLMClient(cfg.llm)
    system_prompt = (
        "你是 AgenticSRE 的 Hermes 智能助手。你专注于 Kubernetes 容器运维、"
        "故障诊断、性能分析、告警处理等 SRE 领域知识。"
        "数据后端使用阿里云 Observability MCP Server，"
        "支持查询指标、日志、链路、实体等遥测数据。"
        "请用中文回答，简洁、可执行；遇到诊断问题时给出建议命令或检查步骤。"
    )
    msgs = [{"role": "system", "content": system_prompt}]
    for h in history[-10:]:
        if h.get("role") in ("user", "assistant"):
            msgs.append({"role": h["role"], "content": h.get("content", "")})
    msgs.append({"role": "user", "content": message})
    try:
        text = llm.chat(msgs)
    except Exception as exc:
        return {"error": f"LLM call failed: {exc}", "response": ""}
    return {
        "response": text,
        "tool_calls": [],
        "tokens": 0,
    }

@app.post("/api/hermes/chat")
async def hermes_chat(request: Request):
    """Send a message to Hermes Agent and get a response."""
    body = await request.json()
    message = body.get("message", "").strip()
    session_id = body.get("session_id", "")

    if not message:
        raise HTTPException(400, "Missing 'message'")

    cfg = _get_config()
    if not cfg.llm.api_key:
        raise HTTPException(503, "LLM API Key 未配置")

    # Create or get session
    if not session_id or session_id not in _hermes_sessions:
        session_id = f"hermes-{uuid.uuid4().hex[:8]}"
        _hermes_sessions[session_id] = {
            "id": session_id,
            "messages": [],
            "status": "idle",
            "created_at": time.time(),
            "total_tokens": 0,
            "tool_calls_count": 0,
        }

    session = _hermes_sessions[session_id]
    session["messages"].append({"role": "user", "content": message, "ts": time.time()})
    session["status"] = "thinking"

    # Run agent in background thread
    def _run():
        try:
            hermes_path, err = _init_hermes_agent()
            if err:
                session["messages"].append({
                    "role": "assistant", "content": f"Error: {err}", "ts": time.time()
                })
                session["status"] = "error"
                return

            # Build conversation history for context
            history = []
            for msg in session["messages"][:-1]:
                if msg["role"] in ("user", "assistant"):
                    history.append({"role": msg["role"], "content": msg["content"]})

            cfg = _get_config()
            result = _run_hermes_sync(hermes_path, message, history, cfg)

            response = result.get("response", "")
            tool_calls = result.get("tool_calls", [])
            input_tokens = result.get("input_tokens", 0) or 0
            output_tokens = result.get("output_tokens", 0) or 0

            session["messages"].append({
                "role": "assistant",
                "content": response,
                "ts": time.time(),
                "tool_calls": tool_calls,
                "api_calls": result.get("api_calls", 0),
                "tokens": {"input": input_tokens, "output": output_tokens},
            })
            session["total_tokens"] += input_tokens + output_tokens
            session["tool_calls_count"] += len(tool_calls)
            session["status"] = "idle"

        except Exception as e:
            logger.error(f"Hermes Agent error: {e}", exc_info=True)
            session["messages"].append({
                "role": "assistant",
                "content": f"Agent 执行出错: {str(e)}",
                "ts": time.time(),
            })
            session["status"] = "error"

    thread = threading.Thread(target=_run, daemon=True, name=f"hermes-{session_id}")
    thread.start()

    return {"session_id": session_id, "status": "thinking"}


@app.get("/api/hermes/chat/{session_id}")
async def hermes_session_status(session_id: str):
    """Get current session state and messages."""
    session = _hermes_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@app.get("/api/hermes/chat/{session_id}/stream")
async def hermes_stream(session_id: str):
    """SSE stream for Hermes Agent responses."""
    session = _hermes_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    async def event_gen():
        msg_count = len(session["messages"])
        while True:
            current_count = len(session["messages"])
            if current_count > msg_count:
                for msg in session["messages"][msg_count:]:
                    yield f"data: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
                msg_count = current_count

            if session["status"] in ("idle", "error"):
                yield f"data: {json.dumps({'type': 'done', 'status': session['status']})}\n\n"
                break

            yield f": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/hermes/sessions")
async def hermes_sessions_list():
    """List all Hermes Agent sessions."""
    sessions = []
    for sid, s in sorted(_hermes_sessions.items(), key=lambda x: x[1].get("created_at", 0), reverse=True):
        msg_count = len(s.get("messages", []))
        last_msg = s["messages"][-1]["content"][:80] if s.get("messages") else ""
        sessions.append({
            "id": sid,
            "status": s.get("status", "idle"),
            "message_count": msg_count,
            "last_message": last_msg,
            "total_tokens": s.get("total_tokens", 0),
            "tool_calls_count": s.get("tool_calls_count", 0),
            "created_at": s.get("created_at", 0),
        })
    return {"sessions": sessions[:20]}


@app.delete("/api/hermes/sessions/{session_id}")
async def hermes_delete_session(session_id: str):
    """Delete a Hermes Agent session."""
    if session_id in _hermes_sessions:
        del _hermes_sessions[session_id]
    return {"status": "ok"}


# ─────────────────────────────────────────
# Health & Meta
# ─────────────────────────────────────────

@app.get("/api/health")
async def health():
    cfg = _get_config()
    llm_ok = bool(cfg.llm.api_key)
    return {
        "status": "ok",
        "timestamp": time.time(),
        "llm_configured": llm_ok,
    }


@app.get("/api/config")
async def get_config_info():
    cfg = _get_config()
    return {
        "llm_model": cfg.llm.model,
        "pipeline": {
            "max_iterations": cfg.pipeline.max_evidence_iterations,
            "confidence_threshold": cfg.pipeline.hypothesis_confidence_threshold,
            "enable_correlation": cfg.pipeline.enable_correlation,
            "enable_graph_rca": cfg.pipeline.enable_graph_rca,
            "enable_recovery": cfg.pipeline.enable_recovery,
        },
        "daemon": {
            "poll_interval": cfg.daemon.poll_interval_seconds,
            "dedup_ttl": cfg.daemon.dedup_ttl_seconds,
            "max_concurrent": cfg.daemon.max_concurrent_pipelines,
        },
    }


# ─────────────────────────────────────────
# Startup
# ─────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    logger.info("AgenticSRE Dashboard starting...")
    _prewarm_caches_async()
    _load_rca_history()
    if _PLATFORM_CONFIG_FILE.exists():
        _apply_platform_config()
    else:
        _load_detection_config()
    _load_fault_targets()
    cfg = _get_config()
    if not cfg.llm.api_key:
        logger.warning(
            "⚠️  LLM API Key 未配置！RCA、告警压缩等 LLM 功能将不可用。"
            "请在项目根目录 .env 文件中设置 LLM_API_KEY=<your-key>，"
            "或在 configs/config_cluster.yaml 中配置 llm.api_key，然后重启服务。"
        )
    else:
        logger.info(f"LLM configured: model={cfg.llm.model}, base_url={cfg.llm.base_url}")


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8080)
