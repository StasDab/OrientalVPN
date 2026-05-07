from __future__ import annotations

from dataclasses import dataclass, field


def _vless_authority(link: str) -> str | None:
    try:
        rest = link.strip().split("://", 1)[1]
        part = rest.split("@", 1)[1]
        return part.split("?", 1)[0].split("#", 1)[0].strip()
    except IndexError:
        return None


def _authority_host(authority: str) -> str:
    """host из authority (учёт IPv6 в скобках и порта у IPv4)."""
    a = authority.strip()
    if a.startswith("["):
        end = a.find("]")
        return a[1:end] if end != -1 else a
    if ":" in a:
        host, _, port = a.rpartition(":")
        if port.isdigit():
            return host
    return a


def pick_share_link_for_node(links: list[str], node: VpnNode) -> str | None:
    """
    Одна vless/vmess ссылка из Marzban GET /api/user → links по подстрокам link_matches
    (полная строка или хост после @).
    """
    needles = [n.strip().lower() for n in node.link_matches if n and str(n).strip()]
    if not needles:
        return None
    for ln in links:
        low = ln.lower()
        auth = _vless_authority(ln)
        host_l = _authority_host(auth).lower() if auth else ""
        auth_l = auth.lower() if auth else ""
        for needle in needles:
            if needle in low or needle in host_l or (auth_l and needle in auth_l):
                return ln
    return None


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
    # Подстроки из vless-ссылки узла (поле links): IP, домен, часть remark — уникально для узла.
    link_matches: tuple[str, ...] = field(default_factory=tuple)

    @property
    def link_match(self) -> str | None:
        return self.link_matches[0] if self.link_matches else None


def select_best_node(nodes: list[VpnNode], location_code: str) -> VpnNode | None:
    filtered = [n for n in nodes if n.location_code == location_code and n.is_healthy]
    if not filtered:
        return None
    return sorted(filtered, key=lambda n: (n.current_load / max(n.capacity, 1), n.current_load))[0]
