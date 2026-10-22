"""
Layer-7 Dynamic Asynchronous HTTP Reverse Proxy and Load Balancer.

Features:
- Built with aiohttp.web and a shared aiohttp.ClientSession connection pool.
- Hot-swappable algorithms: Round-Robin, Smooth Weighted Round-Robin, Least-Connections.
- Active background health probing (every 2.0s, 2 consecutive failures to mark UNHEALTHY, auto-recovery).
- Passive health checking with transparent retry (up to 2 retries) on backend crash/drop.
- Real-time telemetry, admin control plane API (/api/admin/*), and live dashboard (/dashboard).
"""

from __future__ import annotations
import asyncio
import collections
import datetime
import logging
import os
import pathlib
import sys
import time
from typing import Any, Deque, Dict, List, Optional
from aiohttp import web, ClientSession, TCPConnector, ClientTimeout, ClientError

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from algorithms import AlgorithmRouter

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [LB] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("load_balancer")


class BackendNodeState:
    def __init__(self, node_id: str, host: str, port: int, weight: int = 1) -> None:
        self.id: str = node_id
        self.host: str = host
        self.port: int = port
        self.weight: int = weight
        self.current_weight: int = 0  # For Smooth WRR
        self.is_healthy: bool = True
        self.is_crashed: bool = False
        self.active_connections: int = 0
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.failed_requests: int = 0
        self.consecutive_failures: int = 0
        self.consecutive_successes: int = 0
        self.last_latency_ms: float = 0.0
        self.latency_samples: Deque[float] = collections.deque(maxlen=30)
        self.delay_ms: int = 0

    @property
    def avg_latency_ms(self) -> float:
        if not self.latency_samples:
            return self.last_latency_ms
        return round(sum(self.latency_samples) / len(self.latency_samples), 2)

    def record_success(self, latency_ms: float) -> None:
        self.total_requests += 1
        self.successful_requests += 1
        self.last_latency_ms = round(latency_ms, 2)
        self.latency_samples.append(latency_ms)

    def record_failure(self) -> None:
        self.total_requests += 1
        self.failed_requests += 1

    def reset_stats(self) -> None:
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.latency_samples.clear()
        self.last_latency_ms = 0.0
        self.current_weight = 0

    def to_dict(self, cluster_total: int = 0) -> Dict[str, Any]:
        share_pct = round((self.total_requests / cluster_total * 100), 1) if cluster_total > 0 else 0.0
        return {
            "id": self.id,
            "host": self.host,
            "port": self.port,
            "weight": self.weight,
            "current_weight": self.current_weight,
            "is_healthy": self.is_healthy,
            "is_crashed": self.is_crashed,
            "active_connections": self.active_connections,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "request_share_pct": share_pct,
            "last_latency_ms": self.last_latency_ms,
            "avg_latency_ms": self.avg_latency_ms,
            "consecutive_failures": self.consecutive_failures,
            "delay_ms": self.delay_ms,
        }


class LoadBalancer:
    def __init__(
        self,
        nodes_config: Optional[List[Dict[str, Any]]] = None,
        default_algo: str = "round_robin",
        health_check_interval: float = 2.0,
        health_check_timeout: float = 1.0,
        max_retries: int = 2
    ) -> None:
        self.health_check_interval = health_check_interval
        self.health_check_timeout = health_check_timeout
        self.max_retries = max_retries

        # Default 4 backend nodes as per spec
        if nodes_config is None:
            nodes_config = [
                {"id": "node-1", "host": "127.0.0.1", "port": 8001, "weight": 3},
                {"id": "node-2", "host": "127.0.0.1", "port": 8002, "weight": 2},
                {"id": "node-3", "host": "127.0.0.1", "port": 8003, "weight": 1},
                {"id": "node-4", "host": "127.0.0.1", "port": 8004, "weight": 1},
            ]

        self.nodes: List[BackendNodeState] = [
            BackendNodeState(
                node_id=cfg["id"],
                host=cfg.get("host", "127.0.0.1"),
                port=cfg["port"],
                weight=cfg.get("weight", 1)
            )
            for cfg in nodes_config
        ]

        self.router = AlgorithmRouter(default_algo)
        self.session: Optional[ClientSession] = None
        self.health_task: Optional[asyncio.Task] = None
        self.request_logs: Deque[Dict[str, Any]] = collections.deque(maxlen=150)

        # Global metrics
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.failed_requests: int = 0
        self.total_retries: int = 0
        self.cluster_latencies: Deque[float] = collections.deque(maxlen=100)

        self.app = self._build_app()

    def _build_app(self) -> web.Application:
        app = web.Application()

        # Lifecycle signals
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)

        # Admin Control Plane & Telemetry endpoints
        app.router.add_get("/api/admin/metrics", self.handle_get_metrics)
        app.router.add_post("/api/admin/algorithm", self.handle_set_algorithm)
        app.router.add_post("/api/admin/node/{node_id}/config", self.handle_node_config)
        app.router.add_post("/api/admin/reset", self.handle_reset_stats)
        app.router.add_post("/api/admin/stress", self.handle_stress_test)

        # Dashboard UI
        static_dir = pathlib.Path(__file__).parent / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        app.router.add_get("/dashboard", self.handle_dashboard)
        app.router.add_static("/static", path=str(static_dir), name="static")

        # Catch-all Reverse Proxy Handler for all other paths
        app.router.add_route("*", "/{path:.*}", self.handle_proxy)

        return app

    async def _on_startup(self, app: web.Application) -> None:
        connector = TCPConnector(limit=500, limit_per_host=200, use_dns_cache=True)
        self.session = ClientSession(connector=connector)
        self.health_task = asyncio.create_task(self._active_health_monitor_loop())
        logger.info("Load Balancer core initialized. Active health monitor started.")

    async def _on_cleanup(self, app: web.Application) -> None:
        if self.health_task:
            self.health_task.cancel()
            try:
                await self.health_task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()
        logger.info("Load Balancer cleaned up.")

    # --------------------------------------------------------------------------
    # Active Health Monitor
    # --------------------------------------------------------------------------
    async def _active_health_monitor_loop(self) -> None:
        """
        Background loop executing active health probes concurrently every 2.0 seconds.
        A backend is marked UNHEALTHY upon 2 consecutive failures.
        An UNHEALTHY backend is automatically restored to HEALTHY on a successful probe.
        """
        while True:
            try:
                await asyncio.sleep(self.health_check_interval)
                if not self.session:
                    continue
                tasks = [self._probe_node(node) for node in self.nodes]
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in health monitor loop: {e}", exc_info=True)

    async def _probe_node(self, node: BackendNodeState) -> None:
        url = f"http://{node.host}:{node.port}/health"
        timeout = ClientTimeout(total=self.health_check_timeout)
        start_t = time.time()
        is_ok = False

        try:
            async with self.session.get(url, timeout=timeout) as resp:
                latency_ms = (time.time() - start_t) * 1000.0
                if resp.status == 200:
                    is_ok = True
                    node.last_latency_ms = round(latency_ms, 2)
                    node.latency_samples.append(latency_ms)
                else:
                    is_ok = False
        except (ClientError, asyncio.TimeoutError, Exception):
            is_ok = False

        if is_ok:
            node.consecutive_successes += 1
            node.consecutive_failures = 0
            node.is_crashed = False

            if not node.is_healthy:
                node.is_healthy = True
                logger.info(f"HEALTH CHECK: Node '{node.id}' (:{node.port}) PASSED probe -> Restored to HEALTHY")
                self._add_log_event(
                    client_ip="HEALTH_CHECK",
                    algo=self.router.current_algorithm,
                    target_node=f"{node.id} (:{node.port})",
                    status_code=200,
                    latency_ms=round(node.last_latency_ms, 1),
                    failover=False,
                    event_type="RECOVERED"
                )
        else:
            node.consecutive_failures += 1
            node.consecutive_successes = 0

            # 2 consecutive failures mark node UNHEALTHY
            if node.consecutive_failures >= 2 and node.is_healthy:
                node.is_healthy = False
                node.is_crashed = True
                logger.warning(
                    f"HEALTH CHECK: Node '{node.id}' (:{node.port}) failed 2 consecutive probes -> Marked UNHEALTHY"
                )
                self._add_log_event(
                    client_ip="HEALTH_CHECK",
                    algo=self.router.current_algorithm,
                    target_node=f"{node.id} (:{node.port})",
                    status_code=503,
                    latency_ms=0.0,
                    failover=True,
                    event_type="UNHEALTHY"
                )

    # --------------------------------------------------------------------------
    # Reverse Proxy & Passive Health Monitoring with Transparent Retry
    # --------------------------------------------------------------------------
    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        """
        Reverse proxy handler.
        Distributes client requests according to the selected algorithm.
        Enforces connection counter tracking and transparent retry if a node fails mid-flight.
        """
        client_ip = request.remote or "127.0.0.1"
        target_path = request.rel_url.raw_path_qs
        method = request.method
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "transfer-encoding")}
        headers["X-Forwarded-For"] = client_ip

        body = await request.read() if method in ("POST", "PUT", "PATCH") else None

        self.total_requests += 1
        tried_nodes: List[BackendNodeState] = []
        last_error = None
        attempt = 0
        max_attempts = 1 + self.max_retries

        while attempt < max_attempts:
            attempt += 1

            # Select available healthy node excluding already attempted nodes for this request
            available = [n for n in self.nodes if n.is_healthy and n not in tried_nodes]
            if not available:
                # If all healthy nodes were tried, or no healthy nodes exist
                break

            chosen_node = self.router.select_node(available)
            if not chosen_node:
                break

            tried_nodes.append(chosen_node)
            is_failover_retry = (attempt > 1)
            if is_failover_retry:
                self.total_retries += 1

            chosen_node.active_connections += 1
            start_time = time.time()
            try:
                dest_url = f"http://{chosen_node.host}:{chosen_node.port}{target_path}"
                timeout = ClientTimeout(total=5.0)

                async with self.session.request(
                    method=method,
                    url=dest_url,
                    headers=headers,
                    data=body,
                    timeout=timeout,
                    allow_redirects=False
                ) as resp:
                    resp_body = await resp.read()
                    latency_ms = (time.time() - start_time) * 1000.0

                    # Passive Health Check: Detect 500/503 server errors
                    if resp.status in (500, 502, 503):
                        logger.warning(
                            f"PASSIVE FAIL: Node '{chosen_node.id}' (:{chosen_node.port}) returned HTTP {resp.status}. "
                            f"Marking UNHEALTHY. Attempting transparent retry."
                        )
                        chosen_node.is_healthy = False
                        chosen_node.is_crashed = True
                        chosen_node.consecutive_failures = 2
                        chosen_node.record_failure()
                        self._add_log_event(
                            client_ip=client_ip,
                            algo=self.router.current_algorithm,
                            target_node=f"{chosen_node.id} (:{chosen_node.port})",
                            status_code=resp.status,
                            latency_ms=round(latency_ms, 1),
                            failover=True,
                            event_type="FAILOVER_DROP"
                        )
                        last_error = f"HTTP {resp.status}"
                        continue  # Try next healthy node

                    # Request Succeeded!
                    chosen_node.record_success(latency_ms)
                    self.successful_requests += 1
                    self.cluster_latencies.append(latency_ms)

                    self._add_log_event(
                        client_ip=client_ip,
                        algo=self.router.current_algorithm,
                        target_node=f"{chosen_node.id} (:{chosen_node.port})",
                        status_code=resp.status,
                        latency_ms=round(latency_ms, 1),
                        failover=is_failover_retry,
                        event_type="SUCCESS" if not is_failover_retry else "FAILOVER_RECOVERED"
                    )

                    # Construct client response with custom routing headers
                    response_headers = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection", "content-type")
                    }
                    response_headers["X-LB-Node-ID"] = chosen_node.id
                    response_headers["X-LB-Node-Port"] = str(chosen_node.port)
                    response_headers["X-LB-Algorithm"] = self.router.current_algorithm
                    response_headers["X-LB-Retries"] = str(attempt - 1)

                    return web.Response(
                        body=resp_body,
                        status=resp.status,
                        headers=response_headers,
                        content_type=resp.content_type
                    )

            except (ClientError, asyncio.TimeoutError, Exception) as exc:
                latency_ms = (time.time() - start_time) * 1000.0
                logger.warning(
                    f"PASSIVE FAIL: Connection error to node '{chosen_node.id}' (:{chosen_node.port}): {exc}. "
                    f"Marking UNHEALTHY and retrying."
                )
                chosen_node.is_healthy = False
                chosen_node.is_crashed = True
                chosen_node.consecutive_failures = 2
                chosen_node.record_failure()
                last_error = str(exc)

                self._add_log_event(
                    client_ip=client_ip,
                    algo=self.router.current_algorithm,
                    target_node=f"{chosen_node.id} (:{chosen_node.port})",
                    status_code=503,
                    latency_ms=round(latency_ms, 1),
                    failover=True,
                    event_type="FAILOVER_CONN_ERR"
                )
                continue  # Retry on next node
            finally:
                chosen_node.active_connections = max(0, chosen_node.active_connections - 1)

        # If all retries failed or no healthy backends
        self.failed_requests += 1
        logger.error("All backend attempts exhausted. Returning 503 Service Unavailable.")
        return web.json_response(
            {
                "error": "No healthy backend servers available",
                "attempts": attempt,
                "last_error": last_error,
                "timestamp": time.time(),
            },
            status=503
        )

    # --------------------------------------------------------------------------
    # Telemetry & Admin API Handlers
    # --------------------------------------------------------------------------
    def _add_log_event(
        self,
        client_ip: str,
        algo: str,
        target_node: str,
        status_code: int,
        latency_ms: float,
        failover: bool,
        event_type: str = "REQUEST"
    ) -> None:
        now_str = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.request_logs.append({
            "timestamp": now_str,
            "client_ip": client_ip,
            "algorithm": algo,
            "target_node": target_node,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "failover": failover,
            "event_type": event_type,
        })

    async def handle_get_metrics(self, request: web.Request) -> web.Response:
        """
        Telemetry endpoint consumed by the dashboard every 500ms - 1s.
        """
        healthy_count = sum(1 for n in self.nodes if n.is_healthy)
        total_active_conns = sum(n.active_connections for n in self.nodes)
        success_rate = (
            round((self.successful_requests / self.total_requests * 100), 1)
            if self.total_requests > 0 else 100.0
        )
        avg_cluster_latency = (
            round(sum(self.cluster_latencies) / len(self.cluster_latencies), 1)
            if self.cluster_latencies else 0.0
        )

        nodes_data = [n.to_dict(self.total_requests) for n in self.nodes]

        return web.json_response({
            "algorithm": self.router.current_algorithm,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "total_retries": self.total_retries,
            "success_rate": success_rate,
            "active_in_flight": total_active_conns,
            "avg_cluster_latency_ms": avg_cluster_latency,
            "healthy_count": healthy_count,
            "total_nodes": len(self.nodes),
            "nodes": nodes_data,
            "logs": list(self.request_logs)[-40:],  # Recent 40 logs
        })

    async def handle_set_algorithm(self, request: web.Request) -> web.Response:
        """
        Dynamically hot-swaps the load balancing algorithm.
        """
        try:
            body = await request.json()
            new_algo = body.get("algorithm")
            updated = self.router.set_algorithm(new_algo)
            return web.json_response({"status": "success", "algorithm": updated})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_node_config(self, request: web.Request) -> web.Response:
        """
        Configures node weight, latency injection, or kill/recover state.
        Forwards latency and crash commands to the backend node directly.
        """
        node_id = request.match_info["node_id"]
        target_node = next((n for n in self.nodes if n.id == node_id), None)
        if not target_node:
            return web.json_response({"error": f"Node {node_id} not found"}, status=404)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        # 1. Update Weight
        if "weight" in payload:
            target_node.weight = max(1, int(payload["weight"]))
            logger.info(f"Updated weight for {node_id} to {target_node.weight}")

        # 2. Forward delay_ms / is_crashed to the physical backend node
        forward_payload = {}
        if "delay_ms" in payload:
            delay_ms = max(0, int(payload["delay_ms"]))
            target_node.delay_ms = delay_ms
            forward_payload["delay_ms"] = delay_ms

        if "is_crashed" in payload:
            is_crashed = bool(payload["is_crashed"])
            target_node.is_crashed = is_crashed
            forward_payload["is_crashed"] = is_crashed
            if is_crashed:
                target_node.is_healthy = False
                target_node.consecutive_failures = 2

        if forward_payload and self.session:
            node_url = f"http://{target_node.host}:{target_node.port}/config"
            try:
                async with self.session.post(
                    node_url,
                    json=forward_payload,
                    timeout=ClientTimeout(total=2.0)
                ) as resp:
                    resp_json = await resp.json()
                    logger.info(f"Node {node_id} config response: {resp_json}")
            except Exception as e:
                logger.warning(f"Could not forward config to {node_id} (might be dead): {e}")

        return web.json_response({
            "status": "success",
            "node": target_node.to_dict(self.total_requests)
        })

    async def handle_reset_stats(self, request: web.Request) -> web.Response:
        """
        Clears stats, counters, and recent telemetry logs.
        """
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_retries = 0
        self.cluster_latencies.clear()
        self.request_logs.clear()
        self.router.reset()
        for node in self.nodes:
            node.reset_stats()
        logger.info("Telemetry statistics and logs reset.")
        return web.json_response({"status": "reset_successful"})

    async def handle_stress_test(self, request: web.Request) -> web.Response:
        """
        Triggers internal concurrent batch requests for stress testing.
        """
        try:
            body = await request.json()
            count = int(body.get("count", 50))
        except Exception:
            count = 50

        count = max(1, min(count, 500))

        async def send_single():
            try:
                # Issue request internally through the proxy handler or via session to localhost:8080
                async with self.session.get("http://127.0.0.1:8080/", timeout=ClientTimeout(total=5.0)) as r:
                    await r.read()
                    return r.status
            except Exception as e:
                return 500

        # Run concurrently
        tasks = [send_single() for _ in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successes = sum(1 for r in results if r == 200)

        return web.json_response({
            "requested": count,
            "dispatched": len(results),
            "successes": successes
        })

    async def handle_dashboard(self, request: web.Request) -> web.Response:
        """
        Serves the single-page control plane UI (static/index.html).
        """
        index_file = pathlib.Path(__file__).parent / "static" / "index.html"
        if not index_file.exists():
            return web.Response(text="Dashboard file not found. Ensure static/index.html exists.", status=404)
        return web.FileResponse(index_file)


def create_app() -> web.Application:
    lb = LoadBalancer()
    return lb.app


def main() -> None:
    port = int(os.environ.get("PORT", 8080))
    lb = LoadBalancer()
    print(f"\n=======================================================")
    print(f" Dynamic Layer-7 Load Balancer & Health Monitor")
    print(f" Reverse Proxy listening on: http://localhost:{port}")
    print(f" Telemetry Dashboard at:     http://localhost:{port}/dashboard")
    print(f"=======================================================\n")
    web.run_app(lb.app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
