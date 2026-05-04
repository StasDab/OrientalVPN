from app.config import settings
from app.services.server_selector import VpnNode, select_best_node


def marzban_provision_options(node: VpnNode | None, location_code: str) -> tuple[str, dict]:
    """Тег inbound Marzban и словарь настроек VLESS для POST /api/user."""
    code = (location_code or "").lower()
    tag: str | None = None
    if node and getattr(node, "inbound_tag", None):
        tag = str(node.inbound_tag).strip() or None
    if not tag:
        tag = f"loc-{code}"
    vless: dict = {}
    if node and getattr(node, "vless", None) and isinstance(node.vless, dict):
        vless.update(node.vless)
    flow = (settings.marzban_vless_flow or "").strip()
    if flow and "flow" not in vless:
        vless["flow"] = flow
    fingerprint = (settings.marzban_reality_fingerprint or "").strip()
    if fingerprint and "fingerprint" not in vless:
        vless["fingerprint"] = fingerprint
    return tag, vless


def available_location_codes() -> tuple[str, ...]:
    """Локации только из VPN_NODES_JSON. Без нод — пустой tuple (не подставляем de/nl/se)."""
    nodes = load_nodes()
    return tuple(sorted({n.location_code.lower() for n in nodes}))


def load_nodes() -> list[VpnNode]:
    nodes: list[VpnNode] = []
    for raw in settings.vpn_nodes:
        try:
            inbound = raw.get("inbound_tag") or raw.get("inbound")
            inbound_tag = str(inbound).strip() if inbound else None
            vless_raw = raw.get("vless")
            vless = vless_raw if isinstance(vless_raw, dict) else None
            nodes.append(
                VpnNode(
                    location_code=raw["location_code"],
                    api_url=raw["api_url"],
                    capacity=int(raw.get("capacity", 1)),
                    current_load=int(raw.get("current_load", 0)),
                    is_healthy=bool(raw.get("is_healthy", True)),
                    inbound_tag=inbound_tag or None,
                    vless=vless,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return nodes


def pick_node_for_location(location_code: str) -> VpnNode | None:
    nodes = load_nodes()
    return select_best_node(nodes, location_code)
