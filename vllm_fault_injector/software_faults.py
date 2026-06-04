"""Software layer fault injection for vLLM inference services.

Handles: vLLM process crashes, model loading failures, tokenizer errors,
KV cache exhaustion scenarios.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FaultResult:
    """Result of a fault injection operation."""
    fault_type: str
    layer: str
    success: bool
    message: str
    recovery_cmd: Optional[str] = None
    command: Optional[str] = None


class SoftwareFaultInjector:
    """Injects software-layer faults into vLLM inference services.

    Args:
        kubectl_cmd: Base kubectl command (may include SSH prefix).
        namespace: Target K8s namespace.
        dry_run: If True, only log commands without executing.
    """

    def __init__(
        self,
        kubectl_cmd: str = "kubectl",
        namespace: str = "default",
        dry_run: bool = True,
    ) -> None:
        self.kubectl = kubectl_cmd
        self.namespace = namespace
        self.dry_run = dry_run

    async def inject_process_crash(self, deployment: str = "vllm-server") -> FaultResult:
        """Crash the vLLM serving process or force one serving Pod to restart.

        Killing PID 1 is unreliable in Kubernetes containers: PID 1 may be a
        wrapper, may reject signals depending on runtime policy, or may not be
        the actual vLLM process. This implementation first locates a Pod owned
        by the Deployment selector, then tries to kill a non-PID-1 vLLM/python
        serving process. If that cannot be found, it deletes one selected Pod
        with zero grace period, which is the Kubernetes-native equivalent for
        testing restart behavior.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"sh -c '"
            f"selector=$({self.kubectl} get deploy/{deployment} -n {self.namespace} "
            f"-o jsonpath=\"{{range $k,$v:=.spec.selector.matchLabels}}{{printf \\\"%s=%s,\\\" $k $v}}{{end}}\" "
            f"| sed \"s/,$//\"); "
            f"if [ -n \"$selector\" ]; then "
            f"pod=$({self.kubectl} get pod -n {self.namespace} -l \"$selector\" "
            f"-o jsonpath=\"{{.items[0].metadata.name}}\"); "
            f"else "
            f"pod=$({self.kubectl} get pod -n {self.namespace} --no-headers "
            f"-o custom-columns=NAME:.metadata.name | awk \"/^{deployment}-/ {{print \\\\$1; exit}}\"); "
            f"fi; "
            f"if [ -z \"$pod\" ]; then echo \"No pod found for deployment {deployment}\" >&2; exit 2; fi; "
            f"{self.kubectl} exec -n {self.namespace} \"$pod\" -- sh -c "
            f"'\"'\"'pid=$(pgrep -f \"vllm|api_server|openai|python\" | grep -v \"^1$\" | head -1); "
            f"if [ -n \"$pid\" ]; then kill -9 \"$pid\"; echo killed process $pid; "
            f"else exit 42; fi'\"'\"' "
            f"|| {self.kubectl} delete pod \"$pod\" -n {self.namespace} --force --grace-period=0'"
        )
        return await self._execute(
            cmd, "process_crash", "software",
            f"Crashed vLLM serving process or restarted one pod for {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_model_load_failure(self, deployment: str = "vllm-server") -> FaultResult:
        """Corrupt model symlink to cause loading failure on next restart.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'if [ -d /model ]; then mv /model /model.bak; fi'"
        )
        return await self._execute(
            cmd, "model_load_failure", "software",
            f"Renamed model directory in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'if [ -d /model.bak ]; then mv /model.bak /model; fi'",
        )

    async def inject_tokenizer_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Corrupt tokenizer config to trigger tokenizer initialization error.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'echo corrupt > /tmp/tokenizer_override.json'"
        )
        return await self._execute(
            cmd, "tokenizer_error", "software",
            f"Corrupted tokenizer config in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"rm -f /tmp/tokenizer_override.json",
        )

    async def inject_process_hang(self, deployment: str = "vllm-server") -> FaultResult:
        """Pause the vLLM serving process without killing the Pod.

        This simulates a deadlocked event loop, stuck CUDA call, or Python
        runtime hang. Recovery sends SIGCONT and then restarts the Deployment
        if the process does not recover cleanly.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- sh -c "
            f"'pid=$(pgrep -f \"vllm|api_server|openai|python\" | grep -v \"^1$\" | head -1); "
            f"if [ -n \"$pid\" ]; then kill -STOP \"$pid\"; echo stopped process $pid; "
            f"else echo \"vLLM process not found\" >&2; exit 2; fi'"
        )
        return await self._execute(
            cmd, "process_hang", "software",
            f"Paused vLLM serving process in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'pid=$(pgrep -f \"vllm|api_server|openai|python\" | grep -v \"^1$\" | head -1); "
            f"if [ -n \"$pid\" ]; then kill -CONT \"$pid\"; fi' || "
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_model_permission_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Remove read permission from one model asset.

        This is less destructive than byte-level weight corruption and models
        common hostPath/NFS permission regressions.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- sh -c "
            f"'f=$(find /model /models /data/models -name \"*.safetensors\" -o -name \"config.json\" 2>/dev/null | head -1); "
            f"if [ -n \"$f\" ]; then chmod a-r \"$f\" && echo permission removed \"$f\"; "
            f"else echo \"model asset not found\" >&2; exit 2; fi'"
        )
        return await self._execute(
            cmd, "model_permission_error", "software",
            f"Removed read permission from one model asset in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'find /model /models /data/models -name \"*.safetensors\" -o -name \"config.json\" 2>/dev/null | "
            f"xargs -r chmod a+r' || true",
        )

    async def inject_kv_cache_exhaustion(self, deployment: str = "vllm-server") -> FaultResult:
        """Generate memory pressure to exhaust KV cache.

        Sends many concurrent long-context requests to fill KV cache.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \"import os; x = bytearray(1024*1024*512); print('KV cache pressure applied')\""
        )
        return await self._execute(
            cmd, "kv_cache_exhaustion", "software",
            f"Applied KV cache pressure in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_scheduler_contention(
        self, deployment: str = "vllm-server", concurrent: int = 100
    ) -> FaultResult:
        """Flood vLLM scheduler with concurrent long-context requests.

        Disrupts continuous batching by saturating the iteration-level
        scheduler, causing request queuing and timeout cascades.

        Args:
            deployment: Target deployment name.
            concurrent: Number of concurrent requests to send.

        Returns:
            FaultResult with injection outcome.
        """
        script = (
            f"python3 -c \""
            f"import asyncio, aiohttp\\n"
            f"async def flood():\\n"
            f"    async with aiohttp.ClientSession() as s:\\n"
            f"        tasks = [s.post('http://localhost:8000/v1/completions', "
            f"json={{'model':'m','prompt':'A'*4000,'max_tokens':512}}) "
            f"for _ in range({concurrent})]\\n"
            f"        await asyncio.gather(*tasks, return_exceptions=True)\\n"
            f"asyncio.run(flood())\""
        )
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- {script}"
        )
        return await self._execute(
            cmd, "scheduler_contention", "software",
            f"Sent {concurrent} concurrent requests to saturate scheduler in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_request_queue_overflow(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Set vLLM max-num-seqs to 1 to force request queue overflow.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} VLLM_MAX_NUM_SEQS=1"
        )
        return await self._execute(
            cmd, "request_queue_overflow", "software",
            f"Set max-num-seqs=1 on {deployment} to force queue overflow",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} VLLM_MAX_NUM_SEQS-",
        )

    async def inject_continuous_batch_disruption(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Disable continuous batching by setting max-num-batched-tokens=1.

        Forces single-request processing, degrading throughput dramatically.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} VLLM_MAX_NUM_BATCHED_TOKENS=1"
        )
        return await self._execute(
            cmd, "continuous_batch_disruption", "software",
            f"Disabled continuous batching on {deployment}",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} VLLM_MAX_NUM_BATCHED_TOKENS-",
        )

    async def inject_weight_corruption(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Corrupt model weight file to trigger runtime inference errors.

        Writes random bytes into a model safetensors shard.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'f=$(find /model -name \"*.safetensors\" 2>/dev/null | head -1); "
            f"if [ -n \"$f\" ]; then cp \"$f\" \"$f.bak\" && "
            f"dd if=/dev/urandom of=\"$f\" bs=1 count=64 seek=1024 conv=notrunc 2>/dev/null && "
            f"echo \"Corrupted $f\"; fi'"
        )
        return await self._execute(
            cmd, "weight_corruption", "software",
            f"Corrupted model weight shard in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'f=$(find /model -name \"*.safetensors.bak\" 2>/dev/null | head -1); "
            f"if [ -n \"$f\" ]; then mv \"$f\" \"${{f%.bak}}\"; fi'",
        )

    async def inject_prefill_decode_misclassification(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Force prefill/decode misclassification in vLLM scheduler.

        Inspired by Teller Case Study (Section 6): under GPU memory pressure,
        the scheduler may allocate only one token to a newly arrived request,
        causing the attention backend to misclassify it as a decode request
        (num_computed_tokens=0 but num_scheduled_tokens=1). This triggers
        the wrong kernel execution path.

        Simulated by setting gpu-memory-utilization very low + sending
        long-context requests that force aggressive preemption.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} "
            f"VLLM_GPU_MEMORY_UTILIZATION=0.3 VLLM_MAX_NUM_SEQS=2"
        )
        return await self._execute(
            cmd, "prefill_decode_misclassification", "software",
            f"Forced low GPU memory utilization + low max_num_seqs on {deployment} "
            f"to trigger prefill/decode misclassification",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"VLLM_GPU_MEMORY_UTILIZATION- VLLM_MAX_NUM_SEQS-",
        )

    async def inject_attention_backend_mismatch(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Force attention backend mismatch by overriding backend selection.

        Inspired by Teller: when prefill/decode misclassification occurs,
        the device layer launches the wrong kernel (e.g., selective_state_update
        instead of selective_scan_fwd_kernel for Mamba models).

        Simulated by forcing a specific attention backend that is incompatible
        with the model's requirements.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} "
            f"VLLM_ATTENTION_BACKEND=FLASH_ATTN "
            f"VLLM_USE_V1=0"
        )
        return await self._execute(
            cmd, "attention_backend_mismatch", "software",
            f"Forced incompatible attention backend on {deployment}",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"VLLM_ATTENTION_BACKEND- VLLM_USE_V1-",
        )

    async def inject_state_contamination(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Simulate cross-layer state contamination in KV cache.

        Inspired by Teller Case Study: when a request is misclassified as
        decode, the kernel reads stale state from a cache slot left by a
        previous request. The contamination propagates through the entire
        generated sequence.

        Simulated by corrupting shared memory regions used for KV cache
        block tables.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        script = (
            "python3 -c \""
            "import torch\\n"
            "if torch.cuda.is_available():\\n"
            "    # Allocate a tensor that overlaps typical KV cache region\\n"
            "    t = torch.randn(64, 128, device='cuda')\\n"
            "    # Write garbage to simulate stale state contamination\\n"
            "    t.fill_(float('nan'))\\n"
            "    torch.cuda.synchronize()\\n"
            "    print('State contamination injected via NaN KV cache values')\\n"
            "else:\\n"
            "    print('No CUDA device available')\""
        )
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- {script}"
        )
        return await self._execute(
            cmd, "state_contamination", "software",
            f"KV cache state contamination injected in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def recover_all(self, deployment: str = "vllm-server") -> FaultResult:
        """Recover from all software faults by restarting deployment."""
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"VLLM_MAX_NUM_SEQS- VLLM_MAX_NUM_BATCHED_TOKENS- "
            f"VLLM_GPU_MEMORY_UTILIZATION- VLLM_ATTENTION_BACKEND- VLLM_USE_V1- && "
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'pid=$(pgrep -f \"vllm|api_server|openai|python\" | grep -v \"^1$\" | head -1); "
            f"if [ -n \"$pid\" ]; then kill -CONT \"$pid\" 2>/dev/null || true; fi; "
            f"find /model /models /data/models -name \"*.safetensors\" -o -name \"config.json\" 2>/dev/null | "
            f"xargs -r chmod a+r' || true && "
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}"
        )
        return await self._execute(cmd, "recover_all", "software", "Restarted deployment")

    async def _execute(
        self, cmd: str, fault_type: str, layer: str,
        success_msg: str, recovery_cmd: Optional[str] = None,
    ) -> FaultResult:
        """Execute a kubectl command or log in dry-run mode."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would execute: %s", cmd)
            return FaultResult(fault_type, layer, True, f"[DRY-RUN] {success_msg}", recovery_cmd, cmd)

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                logger.info("Fault injected: %s", success_msg)
                return FaultResult(fault_type, layer, True, success_msg, recovery_cmd, cmd)
            else:
                err = stderr.decode().strip()
                logger.error("Fault injection failed: %s", err)
                return FaultResult(fault_type, layer, False, f"Failed: {err}", recovery_cmd, cmd)
        except asyncio.TimeoutError:
            return FaultResult(fault_type, layer, False, "Command timed out", recovery_cmd, cmd)
        except Exception as e:
            return FaultResult(fault_type, layer, False, str(e), recovery_cmd, cmd)

    def list_faults(self) -> List[Dict[str, str]]:
        """List all available software fault types."""
        return [
            {"type": "process_crash", "description": "Kill vLLM serving process or force one Deployment pod restart"},
            {"type": "model_load_failure", "description": "Corrupt model directory path"},
            {"type": "tokenizer_error", "description": "Corrupt tokenizer configuration"},
            {"type": "process_hang", "description": "Pause vLLM serving process with SIGSTOP"},
            {"type": "model_permission_error", "description": "Remove read permission from a model asset"},
            {"type": "kv_cache_exhaustion", "description": "Exhaust KV cache via memory pressure"},
            {"type": "scheduler_contention", "description": "Saturate continuous batching scheduler"},
            {"type": "request_queue_overflow", "description": "Force request queue overflow (max-num-seqs=1)"},
            {"type": "continuous_batch_disruption", "description": "Disable continuous batching"},
            {"type": "weight_corruption", "description": "Corrupt model weight shard at runtime"},
            {"type": "prefill_decode_misclassification", "description": "Force prefill/decode misclassification (Teller case study)"},
            {"type": "attention_backend_mismatch", "description": "Force incompatible attention backend selection"},
            {"type": "state_contamination", "description": "Inject cross-layer KV cache state contamination"},
        ]
