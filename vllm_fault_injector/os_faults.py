"""OS layer fault injection for GPU memory and system resources.

Handles: GPU memory OOM, NUMA configuration errors, Huge Pages exhaustion.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from vllm_fault_injector.software_faults import FaultResult

logger = logging.getLogger(__name__)


class OSFaultInjector:
    """Injects OS-layer faults targeting system resources.

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

    async def inject_gpu_oom(self, deployment: str = "vllm-server") -> FaultResult:
        """Allocate all available GPU memory to cause OOM.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \""
            f"import torch; "
            f"tensors = []; "
            f"try:\\n"
            f"    while True:\\n"
            f"        tensors.append(torch.zeros(1024, 1024, 256, device='cuda'))\\n"
            f"except RuntimeError:\\n"
            f"    print(f'GPU OOM triggered with {{len(tensors)}} allocations')\\n"
            f"\""
        )
        return await self._execute(
            cmd, "gpu_oom", "os",
            f"GPU OOM triggered in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_cpu_stress(
        self, deployment: str = "vllm-server", cores: int = 4, duration: int = 60
    ) -> FaultResult:
        """Apply CPU stress to impact inference throughput.

        Args:
            deployment: Target deployment name.
            cores: Number of CPU cores to stress.
            duration: Duration in seconds.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'for i in $(seq 1 {cores}); do "
            f"timeout {duration} yes > /dev/null & done; "
            f"echo CPU stress started on {cores} cores for {duration}s'"
        )
        return await self._execute(
            cmd, "cpu_stress", "os",
            f"CPU stress on {cores} cores for {duration}s in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- killall yes 2>/dev/null || true",
        )

    async def inject_memory_pressure(
        self, deployment: str = "vllm-server", size_mb: int = 1024
    ) -> FaultResult:
        """Consume system memory to create memory pressure.

        Args:
            deployment: Target deployment name.
            size_mb: Amount of memory to consume in MB.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \"x = bytearray({size_mb} * 1024 * 1024); "
            f"print(f'Allocated {size_mb}MB'); "
            f"import time; time.sleep(60)\""
        )
        return await self._execute(
            cmd, "memory_pressure", "os",
            f"Memory pressure ({size_mb}MB) applied in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_disk_pressure(self, deployment: str = "vllm-server") -> FaultResult:
        """Fill disk to create I/O pressure.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"dd if=/dev/zero of=/tmp/disk_pressure bs=1M count=512 2>/dev/null; "
            f"echo 'Disk pressure applied'"
        )
        return await self._execute(
            cmd, "disk_pressure", "os",
            f"Disk pressure (512MB) applied in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- rm -f /tmp/disk_pressure",
        )

    async def inject_numa_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Set invalid NUMA configuration for GPU processes.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} CUDA_DEVICE_ORDER=FASTEST_FIRST "
            f"GOMP_CPU_AFFINITY=0-1 OMP_NUM_THREADS=1"
        )
        return await self._execute(
            cmd, "numa_error", "os",
            f"NUMA misconfiguration applied to {deployment}",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"CUDA_DEVICE_ORDER- GOMP_CPU_AFFINITY- OMP_NUM_THREADS-",
        )

    async def inject_cgroup_limit(
        self, deployment: str = "vllm-server", memory_limit_mb: int = 256
    ) -> FaultResult:
        """Apply restrictive cgroup memory limit to container.

        Sets an extremely low memory limit via cgroup to trigger OOM kills.

        Args:
            deployment: Target deployment name.
            memory_limit_mb: Memory limit in MB.

        Returns:
            FaultResult with injection outcome.
        """
        # Patch resource limits via kubectl
        patch = (
            f'{{"spec":{{"template":{{"spec":{{"containers":[{{"name":"vllm",'
            f'"resources":{{"limits":{{"memory":"{memory_limit_mb}Mi"}}}}}}]}}}}}}}}'
        )
        cmd = (
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} "
            f"--type=strategic -p '{patch}'"
        )
        return await self._execute(
            cmd, "cgroup_limit", "os",
            f"Memory cgroup limit set to {memory_limit_mb}Mi on {deployment}",
            f"{self.kubectl} rollout undo deploy/{deployment} -n {self.namespace}",
        )

    async def inject_hugepages_exhaustion(self, deployment: str = "vllm-server") -> FaultResult:
        """Exhaust huge pages to degrade GPU memory pinning performance.

        Allocates all available huge pages in the container, forcing the
        CUDA runtime to fall back to regular pages with higher TLB miss rates.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \""
            f"import mmap, os\\n"
            f"pages = []\\n"
            f"try:\\n"
            f"    for _ in range(1024):\\n"
            f"        pages.append(mmap.mmap(-1, 2*1024*1024, flags=mmap.MAP_PRIVATE|mmap.MAP_ANONYMOUS|0x40000))\\n"
            f"except OSError:\\n"
            f"    pass\\n"
            f"print(f'Exhausted {{len(pages)}} huge pages')\\n"
            f"import time; time.sleep(60)\""
        )
        return await self._execute(
            cmd, "hugepages_exhaustion", "os",
            f"Huge pages exhausted in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_fd_exhaustion(self, deployment: str = "vllm-server") -> FaultResult:
        """Exhaust file descriptors to prevent new connections and file I/O.

        Opens maximum number of file descriptors, preventing the vLLM
        server from accepting new connections.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \""
            f"import os\\n"
            f"fds = []\\n"
            f"try:\\n"
            f"    while True:\\n"
            f"        fds.append(os.open('/dev/null', os.O_RDONLY))\\n"
            f"except OSError:\\n"
            f"    pass\\n"
            f"print(f'Exhausted {{len(fds)}} file descriptors')\\n"
            f"import time; time.sleep(60)\""
        )
        return await self._execute(
            cmd, "fd_exhaustion", "os",
            f"File descriptors exhausted in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_io_scheduler_interference(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Interfere with I/O scheduler by generating heavy sequential I/O.

        Saturates disk I/O bandwidth, impacting model weight loading,
        checkpoint writes, and log file operations.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'for i in $(seq 1 4); do "
            f"dd if=/dev/zero of=/tmp/io_pressure_$i bs=4M count=256 oflag=direct 2>/dev/null & "
            f"done; echo IO pressure started'"
        )
        return await self._execute(
            cmd, "io_scheduler_interference", "os",
            f"I/O scheduler interference applied in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'killall dd 2>/dev/null; rm -f /tmp/io_pressure_* || true'",
        )

    async def inject_clock_skew(self, deployment: str = "vllm-server") -> FaultResult:
        """Inject system clock skew to disrupt timeouts and scheduling.

        Shifts the container's system clock forward, causing certificate
        validation failures, timeout miscalculations, and log ordering issues.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'date -s \"+2 hours\" 2>/dev/null || "
            f"python3 -c \"import time; print(f\\\"Clock skew simulated at {{time.time()}}\\\")\"'"
        )
        return await self._execute(
            cmd, "clock_skew", "os",
            f"System clock skewed in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'hwclock -s 2>/dev/null || true'",
        )

    async def recover_all(self, deployment: str = "vllm-server") -> FaultResult:
        """Recover from all OS-layer faults."""
        cmd = f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}"
        return await self._execute(cmd, "recover_all", "os", "OS faults recovered")

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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0:
                return FaultResult(fault_type, layer, True, success_msg, recovery_cmd, cmd)
            return FaultResult(fault_type, layer, False, stderr.decode().strip(), recovery_cmd, cmd)
        except asyncio.TimeoutError:
            return FaultResult(fault_type, layer, False, "Command timed out", recovery_cmd, cmd)
        except Exception as e:
            return FaultResult(fault_type, layer, False, str(e), recovery_cmd, cmd)

    def list_faults(self) -> List[Dict[str, str]]:
        """List all available OS fault types."""
        return [
            {"type": "gpu_oom", "description": "Exhaust GPU memory to cause OOM"},
            {"type": "cpu_stress", "description": "Apply CPU stress to impact throughput"},
            {"type": "memory_pressure", "description": "Consume system memory"},
            {"type": "disk_pressure", "description": "Fill disk space"},
            {"type": "numa_error", "description": "Apply invalid NUMA configuration"},
            {"type": "cgroup_limit", "description": "Apply restrictive cgroup memory limit"},
            {"type": "hugepages_exhaustion", "description": "Exhaust huge pages"},
            {"type": "fd_exhaustion", "description": "Exhaust file descriptors"},
            {"type": "io_scheduler_interference", "description": "Saturate disk I/O bandwidth"},
            {"type": "clock_skew", "description": "Inject system clock skew"},
        ]
