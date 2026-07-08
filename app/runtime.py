"""런타임 설정 — 인증키·데모모드를 DB(settings) 우선으로 해석.

.env 값은 초기 시드로만 쓰이고, 관리자 메뉴에서 저장한 DB 값이 우선한다.
재시작 없이 인증키를 바꿀 수 있도록 매 호출 시 DB를 읽는다(로컬 SQLite라 저렴).
"""
from __future__ import annotations

from . import config, db


def get_service_key() -> str:
    return (db.get_setting("service_key") or config.SERVICE_KEY or "").strip()


def demo_flag() -> bool:
    """관리자가 명시 설정한 데모 플래그(없으면 .env 기본값)."""
    v = db.get_setting("demo_mode")
    if v is None:
        return config.DEMO_MODE_ENV
    return v == "1"


def is_demo() -> bool:
    """실제 데모 여부 — 플래그가 켜져 있거나 인증키가 없으면 데모."""
    return demo_flag() or not get_service_key()


def set_service_key(key: str) -> None:
    db.set_setting("service_key", (key or "").strip())


def set_demo(flag: bool) -> None:
    db.set_setting("demo_mode", "1" if flag else "0")


def masked_key() -> str:
    """관리자 화면 표시용 — 끝 4자리만 노출."""
    k = get_service_key()
    if not k:
        return ""
    return ("•" * max(0, len(k) - 4)) + k[-4:]
