from app.config import settings
from app.services.server_selector import VpnNode, select_best_node


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
