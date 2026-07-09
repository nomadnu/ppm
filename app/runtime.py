"""런타임 설정 — 인증키·데모모드를 저장소(settings) 우선으로 해석.

.env 값은 초기 시드로만 쓰이고, 관리자 메뉴에서 저장한 값이 우선한다.
Firestore 읽기 비용·지연을 줄이기 위해 메모리 캐시를 두고, 관리자 변경 시에만 갱신한다.
"""
from __future__ import annotations

from typing import Optional

from . import config, db

_cache: dict[str, Optional[str]] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _cache["service_key"] = db.get_setting("service_key")
    _cache["demo_mode"] = db.get_setting("demo_mode")
    _loaded = True


def refresh() -> None:
    """관리자가 설정을 바꾼 뒤 캐시를 무효화."""
    global _loaded
    _loaded = False
    _cache.clear()


def get_service_key() -> str:
    _load()
    return (_cache.get("service_key") or config.SERVICE_KEY or "").strip()


def demo_flag() -> bool:
    """관리자가 명시 설정한 데모 플래그(없으면 .env 기본값)."""
    _load()
    v = _cache.get("demo_mode")
    if v is None:
        return config.DEMO_MODE_ENV
    return v == "1"


def is_demo() -> bool:
    """실제 데모 여부 — 플래그가 켜져 있거나 인증키가 없으면 데모."""
    return demo_flag() or not get_service_key()


def set_service_key(key: str) -> None:
    db.set_setting("service_key", (key or "").strip())
    refresh()


def set_demo(flag: bool) -> None:
    db.set_setting("demo_mode", "1" if flag else "0")
    refresh()


def masked_key() -> str:
    """관리자 화면 표시용 — 끝 4자리만 노출."""
    k = get_service_key()
    if not k:
        return ""
    return ("•" * max(0, len(k) - 4)) + k[-4:]
