"""SQLite 저장소 — 조회 이력(F6) + 지정 공급처 메모(F7).

설계서 6.2 스키마를 그대로 구현. 원본 검색 결과 전체는 저장하지 않고
요약값과 후보 3선 스냅샷(JSON)만 보존한다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query         TEXT NOT NULL,
    months        INTEGER NOT NULL,
    searched_at   TEXT NOT NULL,
    result_cnt    INTEGER,
    median_prc    INTEGER,
    unit          TEXT,
    pinned        INTEGER DEFAULT 0,
    candidates    TEXT,          -- 후보 3선 스냅샷 (JSON)
    sel_company   TEXT,
    sel_price     INTEGER,
    sel_spec      TEXT,
    sel_verify_url TEXT,
    sel_item_ref  INTEGER,
    sel_at        TEXT,
    sel_changed   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_query ON search_history(query);

CREATE TABLE IF NOT EXISTS supplier_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL,
    supplier    TEXT NOT NULL,
    memo        TEXT,
    contact     TEXT,
    url         TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_keyword ON supplier_notes(keyword);

-- 앱 설정 (인증키·데모모드·관리자 비밀번호 해시 등)
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- 품명 동의어 사전 (통칭 → 조달청 세부품명 변환, 복수 매핑 지원)
CREATE TABLE IF NOT EXISTS synonyms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    alias         TEXT UNIQUE NOT NULL,   -- 정규형(소문자·공백제거)
    canonicals    TEXT NOT NULL,          -- JSON 배열: 세부품명들
    extra_filters TEXT,                   -- JSON 배열: 규격 필터에 자동 추가할 키워드
    verified      INTEGER DEFAULT 0,
    updated_at    TEXT NOT NULL
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("candidates"):
        try:
            d["candidates"] = json.loads(d["candidates"])
        except (json.JSONDecodeError, TypeError):
            d["candidates"] = []
    return d


# ── 조회 이력 (F6) ────────────────────────────────────────────────

def add_history(
    query: str,
    months: int,
    searched_at: str,
    result_cnt: int,
    median_prc: Optional[int],
    unit: Optional[str],
    candidates: list[dict[str, Any]],
) -> int:
    """검색 실행 시마다 자동 저장. 회차별 보존(갱신하지 않음)."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO search_history
               (query, months, searched_at, result_cnt, median_prc, unit, candidates)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (query, months, searched_at, result_cnt, median_prc, unit,
             json.dumps(candidates, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def prev_history(query: str, exclude_id: int) -> Optional[dict[str, Any]]:
    """같은 검색어의 직전 회차(이번 것 제외 최신 1건)."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM search_history
               WHERE query = ? AND id < ?
               ORDER BY id DESC LIMIT 1""",
            (query, exclude_id),
        ).fetchone()
        return _row_to_dict(row) if row else None


def list_history(q: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    """이력 목록. 고정(pinned) 우선, 최신순."""
    with get_conn() as conn:
        if q:
            rows = conn.execute(
                """SELECT * FROM search_history
                   WHERE query LIKE ?
                   ORDER BY pinned DESC, id DESC LIMIT ?""",
                (f"%{q}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM search_history
                   ORDER BY pinned DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_history(hid: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM search_history WHERE id = ?", (hid,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def select_candidate(
    hid: int,
    company: str,
    price: Optional[int],
    spec: Optional[str],
    verify_url: Optional[str],
    item_ref: Optional[int],
    selected_at: str,
) -> Optional[dict[str, Any]]:
    """후보(또는 전체 목록) 중 1개 선정 기록. 재선정 시 변경 횟수 증가."""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE search_history
               SET sel_company = ?, sel_price = ?, sel_spec = ?,
                   sel_verify_url = ?, sel_item_ref = ?, sel_at = ?,
                   sel_changed = sel_changed + (CASE WHEN sel_company IS NOT NULL THEN 1 ELSE 0 END)
               WHERE id = ?""",
            (company, price, spec, verify_url, item_ref, selected_at, hid),
        )
        if cur.rowcount == 0:
            return None
    return get_history(hid)


def prev_selection(query: str, exclude_id: int) -> Optional[str]:
    """같은 검색어 직전 회차의 선정 업체명."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT sel_company FROM search_history
               WHERE query = ? AND id < ? AND sel_company IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (query, exclude_id),
        ).fetchone()
        return row["sel_company"] if row else None


def delete_history(hid: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM search_history WHERE id = ?", (hid,))
        return cur.rowcount > 0


def toggle_pin(hid: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE search_history SET pinned = 1 - pinned WHERE id = ?", (hid,)
        )
        if cur.rowcount == 0:
            return None
    return get_history(hid)


# ── 지정 공급처 메모 (F7) ─────────────────────────────────────────

def list_notes() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM supplier_notes ORDER BY keyword, id"
        ).fetchall()
        return [dict(r) for r in rows]


def match_notes(query: str) -> list[dict[str, Any]]:
    """검색어에 포함된 키워드의 메모 전건. 대소문자·공백 무시 포함 매칭."""
    normalized = query.replace(" ", "").lower()
    result = []
    for note in list_notes():
        kw = (note["keyword"] or "").replace(" ", "").lower()
        if kw and kw in normalized:
            result.append(note)
    return result


def add_note(keyword: str, supplier: str, memo: Optional[str],
             contact: Optional[str], url: Optional[str], updated_at: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO supplier_notes (keyword, supplier, memo, contact, url, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (keyword, supplier, memo, contact, url, updated_at),
        )
        return int(cur.lastrowid)


def update_note(nid: int, keyword: str, supplier: str, memo: Optional[str],
                contact: Optional[str], url: Optional[str], updated_at: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE supplier_notes
               SET keyword = ?, supplier = ?, memo = ?, contact = ?, url = ?, updated_at = ?
               WHERE id = ?""",
            (keyword, supplier, memo, contact, url, updated_at, nid),
        )
        return cur.rowcount > 0


def delete_note(nid: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM supplier_notes WHERE id = ?", (nid,))
        return cur.rowcount > 0


# ── 설정 (settings) ──────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: Optional[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def seed_admin_if_empty() -> None:
    """관리자 비밀번호 해시가 없으면 기본값으로 시드."""
    import hashlib
    from .config import ADMIN_PASSWORD_DEFAULT
    if get_setting("admin_pw_hash") is None:
        h = hashlib.sha256(ADMIN_PASSWORD_DEFAULT.encode("utf-8")).hexdigest()
        set_setting("admin_pw_hash", h)


# ── 품명 동의어 사전 (synonyms) ───────────────────────────────────

def _norm_alias(text: str) -> str:
    """alias 정규형: 공백 제거 + 소문자."""
    import re
    return re.sub(r"\s+", "", str(text or "")).lower()


# 검색 요청마다 Firestore/DB 전건 읽기 방지 — 메모리 캐시
_SYN_CACHE: dict[str, dict[str, Any]] = {}


def load_synonym_cache() -> None:
    """synonyms 전건을 메모리 캐시에 적재 (기동 시·변경 시 호출)."""
    _SYN_CACHE.clear()
    for row in list_synonyms():
        _SYN_CACHE[row["alias"]] = {
            "canonicals": row["canonicals"],
            "extra_filters": row["extra_filters"],
            "verified": row["verified"],
        }


def synonym_cache() -> dict[str, dict[str, Any]]:
    if not _SYN_CACHE:
        load_synonym_cache()
    return _SYN_CACHE


def list_synonyms() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM synonyms ORDER BY alias").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("canonicals", "extra_filters"):
            try:
                d[k] = json.loads(d[k]) if d[k] else []
            except (json.JSONDecodeError, TypeError):
                d[k] = []
        d["verified"] = bool(d["verified"])
        out.append(d)
    return out


def upsert_synonym(alias: str, canonicals: list[str], extra_filters: list[str],
                   verified: bool, updated_at: str) -> None:
    a = _norm_alias(alias)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO synonyms (alias, canonicals, extra_filters, verified, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(alias) DO UPDATE SET
                 canonicals=excluded.canonicals, extra_filters=excluded.extra_filters,
                 verified=excluded.verified, updated_at=excluded.updated_at""",
            (a, json.dumps(canonicals, ensure_ascii=False),
             json.dumps(extra_filters or [], ensure_ascii=False),
             1 if verified else 0, updated_at),
        )
    load_synonym_cache()


def delete_synonym(sid: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM synonyms WHERE id = ?", (sid,))
    load_synonym_cache()
    return cur.rowcount > 0


def seed_synonyms_if_empty() -> None:
    from datetime import datetime, timezone
    if list_synonyms():
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for alias, canons, extra, verified in SEED_SYNONYMS:
        upsert_synonym(alias, canons, extra, verified, now)


# 현장 검증 완료분 (작업 지시서 #1 §2-3) — 통칭 → 세부품명
SEED_SYNONYMS: list[tuple] = [
    ("철근", ["철근콘크리트용봉강"], [], True),
    ("이형철근", ["철근콘크리트용봉강"], [], True),
    ("이형봉강", ["철근콘크리트용봉강"], [], True),
    ("암거", ["조립식철근콘크리트암거블록"], [], True),
    ("PC암거", ["조립식철근콘크리트암거블록"], [], True),
    ("조립식PC암거", ["조립식철근콘크리트암거블록"], [], True),
    ("맨홀", ["콘크리트맨홀블록"], [], True),
    ("PC맨홀", ["콘크리트맨홀블록"], [], True),
    ("맨홀고무링", ["콘크리트맨홀블록"], ["고무링"], True),
    ("맨홀연결볼트", ["콘크리트맨홀블록"], ["연결볼트"], True),
    ("맨홀사다리", ["콘크리트맨홀블록"], ["사다리"], True),
    ("PE삼중벽관", ["일반용폴리에틸렌관"], ["벽관"], True),
    ("삼중벽관", ["일반용폴리에틸렌관"], ["벽관"], True),
    ("PE이중벽관", ["일반용폴리에틸렌관"], ["벽관"], True),
    ("이중벽관", ["일반용폴리에틸렌관"], ["벽관"], True),
    ("PE수도관", ["일반용폴리에틸렌관", "일반용폴리에틸렌이음관"], [], True),
    ("PE이음관", ["일반용폴리에틸렌이음관"], [], True),
    ("폴리에틸렌이음관", ["일반용폴리에틸렌이음관"], [], True),
    ("도로경계석", ["자연석경계석", "콘크리트경계블록"], [], True),
    ("경계석", ["자연석경계석", "콘크리트경계블록"], [], True),
    ("측구수로관", ["철근콘크리트용배수로관", "철근콘크리트벤치플룸"], [], True),
    ("수로관", ["철근콘크리트용배수로관", "철근콘크리트벤치플룸"], [], True),
    ("아스콘", ["순환아스팔트콘크리트", "순환상온아스팔트콘크리트"], [], False),
    ("순환아스콘", ["순환아스팔트콘크리트"], [], True),
    ("레미콘", ["레미콘"], [], True),
    ("파형강관", ["파형강관"], [], True),
]


def seed_notes_if_empty() -> None:
    """초기 데이터: 레미콘 2곳, 아스콘 1곳 (설계서 확정)."""
    from datetime import datetime, timezone
    if list_notes():
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # 조합명은 조달청 종합쇼핑몰 실데이터 기준(2026-07 확인). 공구별 권역 담당은 정미님이 확정.
    # 조합명은 조달청 종합쇼핑몰 실데이터 기준(2026-07 확인). cntrctCorpNm 부분매칭용 정식명 사용.
    seeds = [
        ("레미콘", "전북서부레미콘사업협동조합",
         "관급 레미콘은 조합 경유 구매. 쇼핑몰 단가는 참고만 (서부권: 군산·김제·부안 등)", "063-XXX-XXXX", None),
        ("아스콘", "전북아스콘공업협동조합",
         "관급 아스콘은 조합 경유 수의계약 — 종합쇼핑몰 미등록일 수 있어 단가 자동조회 제한적", "063-XXX-XXXX", None),
    ]
    for kw, sup, memo, contact, url in seeds:
        add_note(kw, sup, memo, contact, url, now)
