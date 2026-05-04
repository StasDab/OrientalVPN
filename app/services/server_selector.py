from dataclasses import dataclass


@dataclass
class VpnNode:
    location_code: str
    api_url: str
    capacity: int
    current_load: int
    is_healthy: bool = True


def select_best_node(nodes: list[VpnNode], location_code: str) -> VpnNode | None:
    filtered = [n for n in nodes if n.location_code == location_code and n.is_healthy]
    if not filtered:
        return None
    return sorted(filtered, key=lambda n: (n.current_load / max(n.capacity, 1), n.current_load))[0]
