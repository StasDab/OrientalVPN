from app.config import settings
from app.services.server_selector import VpnNode, select_best_node


def available_location_codes() -> tuple[str, ...]:
    """Локации только из VPN_NODES_JSON. Без нод — пустой tuple (не подставляем de/nl/se)."""
    nodes = load_nodes()
    return tuple(sorted({n.location_code.lower() for n in nodes}))


def load_nodes() -> list[VpnNode]:
    nodes: list[VpnNode] = []
    for raw in settings.vpn_nodes:
        try:
            nodes.append(
                VpnNode(
                    location_code=raw["location_code"],
                    api_url=raw["api_url"],
                    capacity=int(raw.get("capacity", 1)),
                    current_load=int(raw.get("current_load", 0)),
                    is_healthy=bool(raw.get("is_healthy", True)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return nodes


def pick_node_for_location(location_code: str) -> VpnNode | None:
    nodes = load_nodes()
    return select_best_node(nodes, location_code)
