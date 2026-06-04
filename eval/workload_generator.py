#!/usr/bin/env python3
"""
AgenticSRE — Background Workload Generator
Generates async HTTP traffic against social-network endpoints
to simulate real user load during fault injection evaluation.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


class WorkloadGenerator:
    """Generate background HTTP traffic against social-network."""

    def __init__(self, config: Dict):
        """
        Args:
            config: workload section from fault_scenarios.yaml, e.g.:
                {
                    "target_url": "http://localhost:8080",
                    "endpoints": [...],
                    "duration_per_scenario": 30,
                }
        """
        self.target_url = config.get("target_url", "http://localhost:8080")
        self.endpoints = config.get("endpoints", [])
        self.duration = config.get("duration_per_scenario", 30)
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._stats = {
            "total_requests": 0,
            "successful": 0,
            "errors": 0,
            "total_latency_ms": 0.0,
        }
        self._start_time: Optional[float] = None

    async def start(self):
        """Start background request loops for each endpoint."""
        if not _HAS_AIOHTTP:
            logger.warning("[Workload] aiohttp not installed, skipping workload generation")
            return
        if not self.endpoints:
            logger.warning("[Workload] No endpoints configured, skipping")
            return

        self._running = True
        self._start_time = time.time()
        self._stats = {"total_requests": 0, "successful": 0, "errors": 0, "total_latency_ms": 0.0}

        logger.info("[Workload] Starting background traffic to %s (%d endpoints)",
                     self.target_url, len(self.endpoints))

        for ep in self.endpoints:
            task = asyncio.create_task(self._endpoint_loop(ep))
            self._tasks.append(task)

    async def stop(self) -> Dict:
        """Stop all background request loops and return stats."""
        self._running = False

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        elapsed = time.time() - self._start_time if self._start_time else 0
        stats = self.get_stats()
        stats["duration_s"] = round(elapsed, 1)

        logger.info("[Workload] Stopped. Total: %d requests, %d errors, avg latency: %.0fms",
                     stats["total_requests"], stats["errors"], stats.get("avg_latency_ms", 0))
        return stats

    def get_stats(self) -> Dict:
        """Return current workload statistics."""
        total = self._stats["total_requests"]
        avg_lat = self._stats["total_latency_ms"] / total if total > 0 else 0
        return {
            "total_requests": total,
            "successful": self._stats["successful"],
            "errors": self._stats["errors"],
            "avg_latency_ms": round(avg_lat, 1),
            "error_rate": round(self._stats["errors"] / total, 3) if total > 0 else 0,
        }

    async def _endpoint_loop(self, endpoint: Dict):
        """Send requests to a single endpoint at the configured rate."""
        path = endpoint.get("path", "/")
        method = endpoint.get("method", "GET").upper()
        rate = endpoint.get("rate", 10)  # requests per second
        interval = 1.0 / rate if rate > 0 else 1.0
        url = f"{self.target_url}{path}"

        # Dummy POST body for compose endpoint
        post_body = {
            "username": "eval_user",
            "user_id": 1,
            "text": "evaluation workload test post",
            "media_ids": [],
            "media_types": [],
            "post_type": 0,
        }

        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while self._running:
                    start = time.time()
                    try:
                        if method == "POST":
                            async with session.post(url, json=post_body) as resp:
                                await resp.read()
                                status = resp.status
                        else:
                            params = {"start": 0, "stop": 10} if "timeline" in path else {}
                            async with session.get(url, params=params) as resp:
                                await resp.read()
                                status = resp.status

                        latency_ms = (time.time() - start) * 1000
                        self._stats["total_requests"] += 1
                        self._stats["total_latency_ms"] += latency_ms

                        if 200 <= status < 400:
                            self._stats["successful"] += 1
                        else:
                            self._stats["errors"] += 1

                    except Exception:
                        self._stats["total_requests"] += 1
                        self._stats["errors"] += 1

                    # Rate control
                    elapsed = time.time() - start
                    sleep_time = max(0, interval - elapsed)
                    await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            pass
