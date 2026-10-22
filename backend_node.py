"""
Configurable Backend Node for Dynamic Load Balancer cluster.

Provides simulated HTTP backend services with real-time fault injection
(crashes and artificial latency) for testing and telemetry.
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import random
import sys
import time
from typing import Optional
from aiohttp import web

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)


class BackendNodeServer:
    def __init__(self, node_id: str, port: int, initial_delay_ms: int = 0) -> None:
        self.node_id = node_id
        self.port = port
        self.is_crashed: bool = False
        self.artificial_delay_ms: int = initial_delay_ms
        self.total_requests: int = 0
        self.start_time: float = time.time()
        self.logger = logging.getLogger(f"Node-{self.node_id}:{self.port}")
        self.app = self._build_app()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/health", self.handle_health)
        app.router.add_post("/config", self.handle_config)
        app.router.add_get("/stats", self.handle_stats)
        return app

    async def handle_index(self, request: web.Request) -> web.Response:
        """
        Main application request handler.
        Simulates network latency + processing jitter.
        Fails with 503 if node is set to crashed.
        """
        if self.is_crashed:
            self.logger.warning("GET / received while CRASHED -> returning 503")
            return web.json_response(
                {
                    "error": "Service Unavailable - Node is Crashed",
                    "server_id": self.node_id,
                    "port": self.port,
                    "status": "crashed",
                    "timestamp": time.time(),
                },
                status=503
            )

        # Calculate processing delay + random network jitter (5-15ms)
        jitter_ms = random.uniform(5.0, 15.0)
        total_delay_ms = self.artificial_delay_ms + jitter_ms
        await asyncio.sleep(total_delay_ms / 1000.0)

        self.total_requests += 1

        return web.json_response(
            {
                "server_id": self.node_id,
                "port": self.port,
                "status": "success",
                "delay_ms": round(total_delay_ms, 2),
                "timestamp": time.time(),
                "total_handled": self.total_requests,
            },
            status=200
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        Active health probe endpoint.
        Returns 200 OK if healthy, 503 Service Unavailable if crashed.
        """
        if self.is_crashed:
            return web.json_response(
                {
                    "status": "unhealthy",
                    "server_id": self.node_id,
                    "port": self.port,
                    "timestamp": time.time(),
                },
                status=503
            )

        return web.json_response(
            {
                "status": "healthy",
                "server_id": self.node_id,
                "port": self.port,
                "uptime_s": round(time.time() - self.start_time, 1),
            },
            status=200
        )

    async def handle_config(self, request: web.Request) -> web.Response:
        """
        Runtime configuration endpoint.
        Allows Load Balancer admin control plane to inject latency or crash/recover the node.
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        if "is_crashed" in payload:
            old_crashed = self.is_crashed
            self.is_crashed = bool(payload["is_crashed"])
            if old_crashed != self.is_crashed:
                state_str = "CRASHED (down)" if self.is_crashed else "RECOVERED (up)"
                self.logger.info(f"State dynamically toggled -> {state_str}")

        if "delay_ms" in payload:
            self.artificial_delay_ms = max(0, int(payload["delay_ms"]))
            self.logger.info(f"Artificial delay updated -> {self.artificial_delay_ms} ms")

        return web.json_response(
            {
                "status": "updated",
                "server_id": self.node_id,
                "port": self.port,
                "is_crashed": self.is_crashed,
                "delay_ms": self.artificial_delay_ms,
            },
            status=200
        )

    async def handle_stats(self, request: web.Request) -> web.Response:
        """
        Node internal statistics endpoint.
        """
        return web.json_response(
            {
                "server_id": self.node_id,
                "port": self.port,
                "is_crashed": self.is_crashed,
                "delay_ms": self.artificial_delay_ms,
                "total_handled": self.total_requests,
                "uptime_seconds": round(time.time() - self.start_time, 2),
            }
        )

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await self.site.start()
        self.logger.info(f"Started Backend Node '{self.node_id}' listening on http://127.0.0.1:{self.port}")

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.logger.info(f"Stopped Backend Node '{self.node_id}' (port {self.port})")


def make_app(node_id: str, port: int, initial_delay_ms: int = 0) -> web.Application:
    server = BackendNodeServer(node_id, port, initial_delay_ms)
    return server.app


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulated HTTP Backend Node")
    parser.add_argument("--id", type=str, default="node-1", help="Node identifier (e.g. node-1)")
    parser.add_argument("--port", type=int, default=8001, help="Port to bind (e.g. 8001)")
    parser.add_argument("--delay", type=int, default=0, help="Initial artificial delay in ms")
    args = parser.parse_args()

    server = BackendNodeServer(node_id=args.id, port=args.port, initial_delay_ms=args.delay)
    web.run_app(server.app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
