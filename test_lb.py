"""
Comprehensive Automated Test Suite for Dynamic Load Balancer.

Verifies:
1. Exact sequential distribution in Round-Robin.
2. Smooth proportional distribution in Weighted Round-Robin (e.g., 3:2:1:1 ratio).
3. Least-Connections routing away from busy/high-latency nodes.
4. Strategy router hot-swapping and error handling.
5. End-to-End integration HTTP proxying with active health probing and transparent failover.
"""

import asyncio
import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer, TestClient

from algorithms import (
    RoundRobinAlgorithm,
    WeightedRoundRobinAlgorithm,
    LeastConnectionsAlgorithm,
    AlgorithmRouter,
)
from backend_node import BackendNodeServer
from load_balancer import LoadBalancer, BackendNodeState


class DummyNode:
    """Mock node for algorithmic unit testing."""
    def __init__(self, node_id: str, weight: int = 1, is_healthy: bool = True) -> None:
        self.id = node_id
        self.host = "127.0.0.1"
        self.port = 8000
        self.weight = weight
        self.current_weight = 0
        self.is_healthy = is_healthy
        self.active_connections = 0
        self.avg_latency_ms = 10.0


# ==============================================================================
# 1. Round-Robin Algorithm Unit Tests
# ==============================================================================
def test_round_robin_distribution():
    algo = RoundRobinAlgorithm()
    nodes = [
        DummyNode("n1"),
        DummyNode("n2"),
        DummyNode("n3"),
        DummyNode("n4"),
    ]

    # Verify sequential cycling (n1 -> n2 -> n3 -> n4 -> n1)
    sequence = [algo.select_node(nodes).id for _ in range(8)]
    assert sequence == ["n1", "n2", "n3", "n4", "n1", "n2", "n3", "n4"]


def test_round_robin_with_unhealthy_node():
    algo = RoundRobinAlgorithm()
    nodes = [
        DummyNode("n1", is_healthy=True),
        DummyNode("n2", is_healthy=False),  # Crashed node
        DummyNode("n3", is_healthy=True),
        DummyNode("n4", is_healthy=True),
    ]

    # Node 2 must be completely skipped
    sequence = [algo.select_node(nodes).id for _ in range(6)]
    assert sequence == ["n1", "n3", "n4", "n1", "n3", "n4"]
    assert "n2" not in sequence


def test_round_robin_all_unhealthy():
    algo = RoundRobinAlgorithm()
    nodes = [DummyNode("n1", is_healthy=False)]
    assert algo.select_node(nodes) is None


# ==============================================================================
# 2. Weighted Round-Robin Algorithm Unit Tests (Smooth NGINX-style)
# ==============================================================================
def test_weighted_round_robin_proportional_distribution():
    algo = WeightedRoundRobinAlgorithm()
    nodes = [
        DummyNode("n1", weight=3),
        DummyNode("n2", weight=2),
        DummyNode("n3", weight=1),
        DummyNode("n4", weight=1),
    ]

    total_weight = 3 + 2 + 1 + 1  # 7
    cycles = 10
    total_requests = total_weight * cycles  # 70 requests

    counts = {"n1": 0, "n2": 0, "n3": 0, "n4": 0}
    for _ in range(total_requests):
        selected = algo.select_node(nodes)
        counts[selected.id] += 1

    # Exact proportional distribution matching configured integer weights
    assert counts["n1"] == 30  # 3 * 10
    assert counts["n2"] == 20  # 2 * 10
    assert counts["n3"] == 10  # 1 * 10
    assert counts["n4"] == 10  # 1 * 10


def test_weighted_round_robin_smooth_interleaving():
    algo = WeightedRoundRobinAlgorithm()
    nodes = [
        DummyNode("n1", weight=3),
        DummyNode("n2", weight=2),
        DummyNode("n3", weight=1),
    ]
    # Sum of weights = 6. Smooth WRR must not cluster all 3 requests to n1 sequentially
    seq = [algo.select_node(nodes).id for _ in range(6)]
    # First 3 selections should not all be n1
    assert seq[:3] != ["n1", "n1", "n1"]
    assert seq.count("n1") == 3
    assert seq.count("n2") == 2
    assert seq.count("n3") == 1


# ==============================================================================
# 3. Least-Connections Algorithm Unit Tests
# ==============================================================================
def test_least_connections_routes_to_least_loaded():
    algo = LeastConnectionsAlgorithm()
    n1 = DummyNode("n1")
    n2 = DummyNode("n2")
    n3 = DummyNode("n3")
    n4 = DummyNode("n4")

    nodes = [n1, n2, n3, n4]

    # Set varied connection counts
    n1.active_connections = 12
    n2.active_connections = 5
    n3.active_connections = 1
    n4.active_connections = 8

    # Must select n3 (least active = 1)
    selected = algo.select_node(nodes)
    assert selected.id == "n3"

    # Simulate n3 taking on load
    n3.active_connections = 6
    # Now n2 is the lowest (5)
    selected = algo.select_node(nodes)
    assert selected.id == "n2"


def test_least_connections_tie_breaking_by_latency():
    algo = LeastConnectionsAlgorithm()
    n1 = DummyNode("n1")
    n2 = DummyNode("n2")

    # Both have 0 active connections, but n2 has lower avg latency
    n1.active_connections = 0
    n1.avg_latency_ms = 45.0

    n2.active_connections = 0
    n2.avg_latency_ms = 12.0

    selected = algo.select_node([n1, n2])
    assert selected.id == "n2"


# ==============================================================================
# 4. Strategy Router Hot-Swap Tests
# ==============================================================================
def test_algorithm_router_hot_swap():
    router = AlgorithmRouter("round_robin")
    assert router.current_algorithm == "round_robin"

    router.set_algorithm("weighted_round_robin")
    assert router.current_algorithm == "weighted_round_robin"

    router.set_algorithm("least_connections")
    assert router.current_algorithm == "least_connections"

    with pytest.raises(ValueError):
        router.set_algorithm("non_existent_algorithm")


# ==============================================================================
# 5. Integration HTTP Tests: Failover & Transparent Retry
# ==============================================================================
@pytest.mark.asyncio
async def test_http_proxy_and_failover():
    """
    Spawns 2 real backend servers and a Load Balancer instance.
    Verifies that when the first server crashes, the load balancer
    transparently retries and routes to the second server with 200 OK.
    """
    # 1. Create two test backend nodes
    backend1 = BackendNodeServer("test-node-1", port=8901)
    backend2 = BackendNodeServer("test-node-2", port=8902)

    server1 = TestServer(backend1.app, port=8901)
    server2 = TestServer(backend2.app, port=8902)

    await server1.start_server()
    await server2.start_server()

    # 2. Setup Load Balancer with these 2 nodes
    lb_config = [
        {"id": "test-node-1", "host": "127.0.0.1", "port": 8901, "weight": 1},
        {"id": "test-node-2", "host": "127.0.0.1", "port": 8902, "weight": 1},
    ]
    lb = LoadBalancer(
        nodes_config=lb_config,
        default_algo="round_robin",
        health_check_interval=10.0  # Keep active probe from interfering with instant test
    )
    lb_server = TestServer(lb.app, port=8980)
    await lb_server.start_server()

    try:
        async with ClientSession() as client:
            # Request 1: Both nodes healthy -> routed to node 1
            async with client.get(f"http://127.0.0.1:8980/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "success"
                assert data["server_id"] == "test-node-1"

            # Request 2: Next in round-robin -> routed to node 2
            async with client.get(f"http://127.0.0.1:8980/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "success"
                assert data["server_id"] == "test-node-2"

            # NOW SIMULATE CRASH ON NODE 1
            backend1.is_crashed = True

            # Request 3: Round-Robin would select Node 1, but Node 1 will fail (503).
            # The Load Balancer MUST transparently failover to Node 2 and return 200 OK!
            async with client.get(f"http://127.0.0.1:8980/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "success"
                # Response was fulfilled by test-node-2 via transparent retry!
                assert data["server_id"] == "test-node-2"
                assert resp.headers.get("X-LB-Node-ID") == "test-node-2"
                assert resp.headers.get("X-LB-Retries") == "1"

            # Node 1 should now be marked UNHEALTHY
            node1_state = next(n for n in lb.nodes if n.id == "test-node-1")
            assert node1_state.is_healthy is False

    finally:
        await lb_server.close()
        await server1.close()
        await server2.close()


@pytest.mark.asyncio
async def test_admin_api_endpoints():
    """
    Tests the admin API: metrics retrieval, algorithm hot-swap, and reset.
    """
    lb = LoadBalancer(
        nodes_config=[
            {"id": "node-1", "host": "127.0.0.1", "port": 8001, "weight": 2},
            {"id": "node-2", "host": "127.0.0.1", "port": 8002, "weight": 1},
        ],
        default_algo="round_robin",
        health_check_interval=10.0
    )
    lb_server = TestServer(lb.app, port=8981)
    await lb_server.start_server()

    try:
        async with ClientSession() as client:
            # 1. Fetch metrics
            async with client.get("http://127.0.0.1:8981/api/admin/metrics") as resp:
                assert resp.status == 200
                metrics = await resp.json()
                assert metrics["algorithm"] == "round_robin"
                assert len(metrics["nodes"]) == 2

            # 2. Hot-swap algorithm to weighted_round_robin
            async with client.post(
                "http://127.0.0.1:8981/api/admin/algorithm",
                json={"algorithm": "weighted_round_robin"}
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["algorithm"] == "weighted_round_robin"
                assert lb.router.current_algorithm == "weighted_round_robin"

            # 3. Reset stats
            async with client.post("http://127.0.0.1:8981/api/admin/reset") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "reset_successful"
    finally:
        await lb_server.close()


@pytest.mark.asyncio
async def test_active_health_probing_and_auto_recovery():
    """
    Verifies that the background active health monitor probe detects node crashes,
    marks them UNHEALTHY after 2 failures, and automatically restores them
    when they come back online.
    """
    backend = BackendNodeServer("probe-node-1", port=8905)
    server = TestServer(backend.app, port=8905)
    await server.start_server()

    lb_config = [{"id": "probe-node-1", "host": "127.0.0.1", "port": 8905, "weight": 1}]
    lb = LoadBalancer(
        nodes_config=lb_config,
        default_algo="round_robin",
        health_check_interval=0.15,  # Fast probe interval for rapid test
        health_check_timeout=0.2
    )
    lb_server = TestServer(lb.app, port=8985)
    await lb_server.start_server()

    try:
        node_state = lb.nodes[0]
        # Initially healthy
        await asyncio.sleep(0.3)
        assert node_state.is_healthy is True

        # Crash the backend
        backend.is_crashed = True
        # Wait for 2 probes to fail (~0.4s)
        await asyncio.sleep(0.5)
        assert node_state.is_healthy is False
        assert node_state.consecutive_failures >= 2

        # Recover the backend
        backend.is_crashed = False
        # Wait for next probe to succeed
        await asyncio.sleep(0.4)
        assert node_state.is_healthy is True
        assert node_state.consecutive_failures == 0
    finally:
        await lb_server.close()
        await server.close()


@pytest.mark.asyncio
async def test_backend_node_config_endpoints():
    """
    Verifies that backend nodes and load balancer admin correctly handle
    dynamic configuration changes (weight, latency injection, crash toggle).
    """
    backend = BackendNodeServer("cfg-node", port=8906)
    server = TestServer(backend.app, port=8906)
    await server.start_server()

    lb_config = [{"id": "cfg-node", "host": "127.0.0.1", "port": 8906, "weight": 1}]
    lb = LoadBalancer(nodes_config=lb_config, default_algo="round_robin", health_check_interval=10.0)
    lb_server = TestServer(lb.app, port=8986)
    await lb_server.start_server()

    try:
        async with ClientSession() as client:
            # Configure via LB admin endpoint
            async with client.post(
                "http://127.0.0.1:8986/api/admin/node/cfg-node/config",
                json={"weight": 5, "delay_ms": 50, "is_crashed": True}
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["node"]["weight"] == 5
                assert data["node"]["delay_ms"] == 50
                assert data["node"]["is_crashed"] is True

            # Verify physical node state updated
            assert backend.artificial_delay_ms == 50
            assert backend.is_crashed is True

            # Restore node
            async with client.post(
                "http://127.0.0.1:8986/api/admin/node/cfg-node/config",
                json={"is_crashed": False, "delay_ms": 0}
            ) as resp:
                assert resp.status == 200
                assert backend.is_crashed is False
                assert backend.artificial_delay_ms == 0
    finally:
        await lb_server.close()
        await server.close()

