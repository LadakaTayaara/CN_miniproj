"""
Routing Algorithms for Layer-7 Dynamic HTTP Load Balancer.

Supports:
1. Round-Robin (sequential cycling across healthy nodes).
2. Smooth Weighted Round-Robin (NGINX-style smooth interleaved distribution).
3. Least-Connections (lowest active in-flight requests, with latency and weight tie-breaking).
"""

from __future__ import annotations
from typing import List, Optional, Protocol
import logging

logger = logging.getLogger("load_balancer.algorithms")


class BackendNodeProtocol(Protocol):
    id: str
    host: str
    port: int
    weight: int
    is_healthy: bool
    active_connections: int
    current_weight: int
    avg_latency_ms: float


class BaseAlgorithm:
    name: str = "base"

    def select_node(self, nodes: List[BackendNodeProtocol]) -> Optional[BackendNodeProtocol]:
        raise NotImplementedError

    def reset(self) -> None:
        pass


class RoundRobinAlgorithm(BaseAlgorithm):
    name = "round_robin"

    def __init__(self) -> None:
        self.index: int = 0

    def select_node(self, nodes: List[BackendNodeProtocol]) -> Optional[BackendNodeProtocol]:
        healthy_nodes = [n for n in nodes if n.is_healthy]
        if not healthy_nodes:
            return None

        # Modulo indexing over healthy nodes
        selected = healthy_nodes[self.index % len(healthy_nodes)]
        self.index = (self.index + 1) % len(healthy_nodes)
        return selected

    def reset(self) -> None:
        self.index = 0


class WeightedRoundRobinAlgorithm(BaseAlgorithm):
    """
    Smooth Weighted Round-Robin (NGINX algorithm).
    Prevents bursty clustering of requests to high-weight nodes by distributing
    requests uniformly over the cycle:
    1. For each healthy node: current_weight += weight
    2. Pick the healthy node with maximum current_weight
    3. current_weight[selected] -= total_weight
    """
    name = "weighted_round_robin"

    def select_node(self, nodes: List[BackendNodeProtocol]) -> Optional[BackendNodeProtocol]:
        healthy_nodes = [n for n in nodes if n.is_healthy]
        if not healthy_nodes:
            return None

        total_weight = sum(max(1, n.weight) for n in healthy_nodes)
        if total_weight == 0:
            return healthy_nodes[0]

        best_node: Optional[BackendNodeProtocol] = None
        max_current_weight = -float("inf")

        for node in healthy_nodes:
            effective_weight = max(1, node.weight)
            node.current_weight += effective_weight
            if node.current_weight > max_current_weight:
                max_current_weight = node.current_weight
                best_node = node

        if best_node is not None:
            best_node.current_weight -= total_weight

        return best_node

    def reset(self) -> None:
        pass


class LeastConnectionsAlgorithm(BaseAlgorithm):
    """
    Least-Connections algorithm.
    Selects the healthy node with the lowest number of active in-flight connections.
    Tie-breaks:
    1. Lower moving average latency (avg_latency_ms)
    2. Higher configured weight
    """
    name = "least_connections"

    def select_node(self, nodes: List[BackendNodeProtocol]) -> Optional[BackendNodeProtocol]:
        healthy_nodes = [n for n in nodes if n.is_healthy]
        if not healthy_nodes:
            return None

        # Sort key: (active_connections ASC, avg_latency_ms ASC, -weight DESC, node_id)
        selected = min(
            healthy_nodes,
            key=lambda n: (
                n.active_connections,
                getattr(n, "avg_latency_ms", 0.0),
                -max(1, getattr(n, "weight", 1)),
                n.id
            )
        )
        return selected

    def reset(self) -> None:
        pass


class AlgorithmRouter:
    """
    Strategy selector and state manager for dynamic load-balancing algorithm switching.
    """
    ALGORITHMS = {
        "round_robin": RoundRobinAlgorithm,
        "weighted_round_robin": WeightedRoundRobinAlgorithm,
        "least_connections": LeastConnectionsAlgorithm,
    }

    def __init__(self, default_algorithm: str = "round_robin") -> None:
        self._current_algo_name = default_algorithm
        self._algorithm = self._instantiate_algorithm(default_algorithm)

    def _instantiate_algorithm(self, name: str) -> BaseAlgorithm:
        algo_cls = self.ALGORITHMS.get(name)
        if not algo_cls:
            valid = list(self.ALGORITHMS.keys())
            raise ValueError(f"Unknown algorithm '{name}'. Valid choices are: {valid}")
        return algo_cls()

    @property
    def current_algorithm(self) -> str:
        return self._current_algo_name

    def set_algorithm(self, name: str) -> str:
        if name not in self.ALGORITHMS:
            raise ValueError(f"Invalid algorithm '{name}'. Choose from: {list(self.ALGORITHMS.keys())}")
        self._current_algo_name = name
        self._algorithm = self._instantiate_algorithm(name)
        logger.info(f"Switched load balancing algorithm to: {name}")
        return self._current_algo_name

    def select_node(self, nodes: List[BackendNodeProtocol]) -> Optional[BackendNodeProtocol]:
        return self._algorithm.select_node(nodes)

    def reset(self) -> None:
        self._algorithm.reset()
