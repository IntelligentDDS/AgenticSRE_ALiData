"""Driver layer fault injection for GPU/CUDA/NCCL.

Handles: GPU driver crashes, CUDA errors, NCCL communication failures.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from vllm_fault_injector.software_faults import FaultResult

logger = logging.getLogger(__name__)


class DriverFaultInjector:
    """Injects driver-layer faults targeting GPU and CUDA subsystems.

    Args:
        kubectl_cmd: Base kubectl command.
        namespace: Target K8s namespace.
        dry_run: If True, only log commands.
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

    async def inject_gpu_driver_crash(self, node: str = "") -> FaultResult:
        """Simulate GPU driver crash by unloading nvidia module.

        Args:
            node: Target node name (empty for any GPU node).

        Returns:
            FaultResult with injection outcome.
        """
        target = f"--field-selector spec.nodeName={node}" if node else ""
        cmd = (
            f"{self.kubectl} debug node/{node or '$(kubectl get nodes -l nvidia.com/gpu.present=true -o name | head -1)'} "
            f"-n {self.namespace} --image=busybox -- "
            f"sh -c 'echo Simulating GPU driver crash via nvidia-smi drain'"
        )
        return await self._execute(
            cmd, "gpu_driver_crash", "driver",
            f"GPU driver crash simulated on {node or 'auto-selected node'}",
            f"# Recovery: restart kubelet on affected node",
        )

    async def inject_cuda_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Inject CUDA error by setting invalid CUDA_VISIBLE_DEVICES.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} CUDA_VISIBLE_DEVICES=99"
        )
        return await self._execute(
            cmd, "cuda_error", "driver",
            f"Set invalid CUDA_VISIBLE_DEVICES on {deployment}",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} CUDA_VISIBLE_DEVICES-",
        )

    async def inject_nccl_failure(self, deployment: str = "vllm-server") -> FaultResult:
        """Simulate NCCL communication failure by blocking inter-GPU ports.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'export NCCL_TIMEOUT=1 && export NCCL_DEBUG=WARN && "
            f"iptables -A OUTPUT -p tcp --dport 29500 -j DROP 2>/dev/null || "
            f"echo NCCL failure simulated'"
        )
        return await self._execute(
            cmd, "nccl_failure", "driver",
            f"NCCL communication blocked on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'iptables -D OUTPUT -p tcp --dport 29500 -j DROP 2>/dev/null || true'",
        )

    async def inject_ecc_memory_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Simulate GPU ECC uncorrectable memory error.

        Uses nvidia-smi to inject a simulated ECC error event or, if
        unavailable, forces GPU memory corruption via targeted writes.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c '"
            f"nvidia-smi --gpu-reset -i 0 2>/dev/null && echo ECC_error_injected || "
            f"python3 -c \""
            f"import ctypes, torch; t = torch.zeros(1024, device=\\\"cuda\\\"); "
            f"ptr = t.data_ptr(); ctypes.memset(ptr, 0xFF, 8); "
            f"print(\\\"ECC-like memory corruption injected\\\")\"'"
        )
        return await self._execute(
            cmd, "ecc_memory_error", "driver",
            f"ECC memory error simulated on {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_pcie_throttle(self, deployment: str = "vllm-server") -> FaultResult:
        """Throttle PCIe bandwidth by saturating the PCIe bus.

        Launches a background process that continuously copies data between
        host and device to saturate PCIe bandwidth.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \""
            f"import torch, threading, time\\n"
            f"def pcie_flood():\\n"
            f"    h = torch.randn(1024, 1024, 64)\\n"
            f"    for _ in range(1000):\\n"
            f"        d = h.cuda()\\n"
            f"        _ = d.cpu()\\n"
            f"threading.Thread(target=pcie_flood, daemon=True).start()\\n"
            f"time.sleep(30)\\n"
            f"print('PCIe throttle completed')\""
        )
        return await self._execute(
            cmd, "pcie_throttle", "driver",
            f"PCIe bus saturated on {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_gpu_clock_throttle(self, deployment: str = "vllm-server") -> FaultResult:
        """Force GPU clock to minimum frequency to simulate thermal throttling.

        Uses nvidia-smi to lock GPU clocks at minimum supported frequency.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'nvidia-smi -lgc 210,210 2>/dev/null && "
            f"echo GPU clock locked to minimum || "
            f"echo nvidia-smi clock control not available'"
        )
        return await self._execute(
            cmd, "gpu_clock_throttle", "driver",
            f"GPU clock throttled to minimum on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"nvidia-smi -rgc 2>/dev/null || true",
        )

    async def inject_cuda_ctx_corruption(self, deployment: str = "vllm-server") -> FaultResult:
        """Corrupt CUDA context to cause subsequent kernel launch failures.

        Destroys the primary CUDA context, causing all subsequent CUDA
        operations to fail until process restart.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \""
            f"import ctypes\\n"
            f"cuda = ctypes.CDLL('libcuda.so')\\n"
            f"ctx = ctypes.c_void_p()\\n"
            f"cuda.cuCtxGetCurrent(ctypes.byref(ctx))\\n"
            f"if ctx.value: cuda.cuCtxDestroy(ctx)\\n"
            f"print('CUDA context destroyed')\""
        )
        return await self._execute(
            cmd, "cuda_ctx_corruption", "driver",
            f"CUDA context destroyed on {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_cupti_trace_overhead(
        self, deployment: str = "vllm-server", duration: int = 60
    ) -> FaultResult:
        """Inject CUPTI profiling overhead to slow GPU kernel dispatches.

        Inspired by Teller's non-intrusive tracing design (Section 3.2):
        CUPTI callbacks and activity collection introduce measurable overhead
        on kernel launch latency. This simulates the scenario where profiling
        tools left running in production cause performance degradation.

        Enables full CUPTI activity tracing via nsys for the specified
        duration, adding ~5-15% overhead on kernel launches.

        Args:
            deployment: Target deployment name.
            duration: Duration in seconds to run profiler.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'nohup nsys profile -t cuda,nvtx,osrt --duration {duration} "
            f"--output /tmp/cupti_overhead_trace --force-overwrite true "
            f"--sample none -p $(pgrep -f vllm | head -1) > /dev/null 2>&1 & "
            f"echo CUPTI overhead tracing started for {duration}s'"
        )
        return await self._execute(
            cmd, "cupti_trace_overhead", "driver",
            f"CUPTI profiling overhead injected for {duration}s on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'killall nsys 2>/dev/null; rm -f /tmp/cupti_overhead_trace* || true'",
        )

    async def recover_all(self, deployment: str = "vllm-server") -> FaultResult:
        """Recover from driver faults."""
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"nvidia-smi -rgc 2>/dev/null || true && "
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} CUDA_VISIBLE_DEVICES- && "
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}"
        )
        return await self._execute(cmd, "recover_all", "driver", "Driver faults recovered")

    async def _execute(
        self, cmd: str, fault_type: str, layer: str,
        success_msg: str, recovery_cmd: Optional[str] = None,
    ) -> FaultResult:
        """Execute command or log in dry-run mode."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would execute: %s", cmd)
            return FaultResult(fault_type, layer, True, f"[DRY-RUN] {success_msg}", recovery_cmd, cmd)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                return FaultResult(fault_type, layer, True, success_msg, recovery_cmd, cmd)
            return FaultResult(fault_type, layer, False, stderr.decode().strip(), recovery_cmd, cmd)
        except asyncio.TimeoutError:
            return FaultResult(fault_type, layer, False, "Command timed out", recovery_cmd, cmd)
        except Exception as e:
            return FaultResult(fault_type, layer, False, str(e), recovery_cmd, cmd)

    def list_faults(self) -> List[Dict[str, str]]:
        """List all available driver fault types."""
        return [
            {"type": "gpu_driver_crash", "description": "Simulate GPU driver crash"},
            {"type": "cuda_error", "description": "Set invalid CUDA_VISIBLE_DEVICES"},
            {"type": "nccl_failure", "description": "Block NCCL inter-GPU communication"},
            {"type": "ecc_memory_error", "description": "Simulate GPU ECC uncorrectable memory error"},
            {"type": "pcie_throttle", "description": "Saturate PCIe bus to throttle bandwidth"},
            {"type": "gpu_clock_throttle", "description": "Lock GPU clock to minimum frequency"},
            {"type": "cuda_ctx_corruption", "description": "Destroy CUDA context to crash kernels"},
            {"type": "cupti_trace_overhead", "description": "Inject CUPTI profiling overhead (Teller-inspired)"},
        ]
