from dataclasses import dataclass


@dataclass
class VpnNode:
    location_code: str
    api_url: str
    capacity: int
    current_load: int
    is_healthy: bool = True
    # Если тег inbound в Marzban не loc-<код>, задайте здесь точное имя тега.
    inbound_tag: str | None = None
    # Доп. поля proxies.vless (например {"flow": "xtls-rprx-vision"}), перекрывают MARZBAN_VLESS_FLOW.
    vless: dict | None = None
    # Подстрока для выбора одной vless-ссылки из GET /api/user (поле links), напр. публичный IP узла "77.110.".
    link_match: str | None = None


def select_best_node(nodes: list[VpnNode], location_code: str) -> VpnNode | None:
    filtered = [n for n in nodes if n.location_code == location_code and n.is_healthy]
    if not filtered:
        return None
    return sorted(filtered, key=lambda n: (n.current_load / max(n.capacity, 1), n.current_load))[0]
