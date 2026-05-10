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
            matches: list[str] = []
            lm_one = raw.get("link_match")
            if lm_one:
                s = str(lm_one).strip()
                if s:
                    matches.append(s)
            lm_many = raw.get("link_matches")
            if isinstance(lm_many, list):
                for item in lm_many:
                    s = str(item).strip()
                    if s and s not in matches:
                        matches.append(s)
            nodes.append(
                VpnNode(
                    location_code=raw["location_code"],
                    api_url=raw["api_url"],
                    capacity=int(raw.get("capacity", 1)),
                    current_load=int(raw.get("current_load", 0)),
                    is_healthy=bool(raw.get("is_healthy", True)),
                    inbound_tag=inbound_tag or None,
                    vless=vless,
                    link_matches=tuple(matches),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return nodes


def pick_node_for_location(location_code: str) -> VpnNode | None:
    nodes = load_nodes()
    return select_best_node(nodes, location_code)


def pick_primary_node() -> VpnNode | None:
    """Первая здоровая нода по алфавиту location_code — для выдачи, когда локация не выбирается."""
    all_n = load_nodes()
    if not all_n:
        return None
    healthy = [n for n in all_n if n.is_healthy]
    pool = healthy if healthy else all_n
    return sorted(pool, key=lambda n: n.location_code.lower())[0]


def pick_node_for_panel(panel_url: str) -> VpnNode | None:
    """Любая нода из vpn_nodes с указанным api_url (та же логика здорового пула)."""
    pu = (panel_url or "").rstrip("/").lower()
    if not pu:
        return None
    match = [
        n
        for n in load_nodes()
        if (n.api_url or "").rstrip("/").lower() == pu and n.is_healthy
    ]
    if not match:
        match = [n for n in load_nodes() if (n.api_url or "").rstrip("/").lower() == pu]
    if not match:
        return None
    return sorted(match, key=lambda n: n.location_code.lower())[0]


def all_vless_inbound_tags_same_panel(panel_url: str) -> list[str]:
    """
    Все теги VLESS inbound с той же панели (`api_url`), что и переданная.
    Нужно, чтобы в Marzban у tg_* были сразу все сервера в одной ссылке /sub/.
    """
    pu = (panel_url or "").rstrip("/").lower()
    tags: list[str] = []
    seen: set[str] = set()
    for n in load_nodes():
        if (n.api_url or "").rstrip("/").lower() != pu:
            continue
        tag, _ = marzban_provision_options(n, n.location_code)
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return sorted(tags)
