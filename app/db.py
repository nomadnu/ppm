"""Firestore 저장소 (v0.4) — 조회 이력·지정 공급처 메모·품명 동의어 사전·설정.

Render 무료 인스턴스의 디스크 휘발성으로 SQLite 이력이 유실되던 문제를 해결하기 위해
저장소를 Firestore로 전환. FastAPI(Render)에서 firebase-admin SDK로 접근하며,
서비스 계정 키는 환경변수로만 주입한다(코드·저장소에 미포함).

  FIREBASE_CREDENTIALS       서비스계정 키 JSON '문자열' 전체 (Render 권장)
  FIREBASE_CREDENTIALS_FILE  또는 키 JSON '파일 경로' (로컬 개발 편의)

함수 시그니처는 기존 SQLite 버전과 동일하게 유지해 나머지 코드 변경을 최소화한다.
문서 ID는 문자열이므로 이력/메모 id 는 str, 동의어 id 는 정규형 alias 이다.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .config import ADMIN_PASSWORD_DEFAULT

_db = None


def init_db() -> None:
    """Firestore 클라이언트 초기화. 자격증명이 없으면 명확한 에러로 기동 중단."""
    global _db
    if _db is not None:
        return
    raw = os.environ.get("FIREBASE_CREDENTIALS", "").strip()
    path = os.environ.get("FIREBASE_CREDENTIALS_FILE", "").strip()
    if raw:
        cred = credentials.Certificate(json.loads(raw))
    elif path and os.path.exists(path):
        cred = credentials.Certificate(path)
    else:
        raise RuntimeError(
            "FIREBASE_CREDENTIALS(키 JSON 문자열) 또는 FIREBASE_CREDENTIALS_FILE(키 파일 경로)가 "
            "설정되지 않았습니다. Firebase 서비스계정 키를 환경변수로 주입하세요."
        )
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    _db = firestore.client()


def _col(name: str):
    if _db is None:
        init_db()
    return _db.collection(name)


# ── 설정 (settings) — 인증키·데모모드·관리자 비밀번호 해시 ──────────

def get_setting(key: str) -> Optional[str]:
    doc = _col("settings").document(key).get()
    return doc.to_dict().get("value") if doc.exists else None


def set_setting(key: str, value: Optional[str]) -> None:
    _col("settings").document(key).set({"value": value})


def seed_admin_if_empty() -> None:
    import hashlib
    if get_setting("admin_pw_hash") is None:
        h = hashlib.sha256(ADMIN_PASSWORD_DEFAULT.encode("utf-8")).hexdigest()
        set_setting("admin_pw_hash", h)


# ── 조회 이력 (search_history) ────────────────────────────────────

def add_history(query: str, months: int, searched_at: str, result_cnt: int,
                median_prc: Optional[int], unit: Optional[str],
                candidates: list[dict[str, Any]]) -> str:
    ref = _col("search_history").document()
    ref.set({
        "query": query, "months": months, "searched_at": searched_at,
        "result_cnt": result_cnt, "median_prc": median_prc, "unit": unit,
        "pinned": 0, "candidates": candidates,
        "sel_company": None, "sel_price": None, "sel_spec": None,
        "sel_verify_url": None, "sel_item_ref": None, "sel_at": None,
        "sel_changed": 0,
    })
    return ref.id


def get_history(hid: str) -> Optional[dict[str, Any]]:
    doc = _col("search_history").document(hid).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d["id"] = doc.id
    return d


def _history_by_query(query: str) -> list[dict[str, Any]]:
    """같은 검색어의 이력 전건(파이썬 정렬 — 복합 색인 불필요)."""
    rows = [{**d.to_dict(), "id": d.id}
            for d in _col("search_history")
            .where(filter=FieldFilter("query", "==", query)).stream()]
    rows.sort(key=lambda r: r.get("searched_at") or "", reverse=True)
    return rows


def prev_history(query: str, exclude_id: str) -> Optional[dict[str, Any]]:
    for r in _history_by_query(query):
        if r["id"] != exclude_id:
            return r
    return None


def prev_selection(query: str, exclude_id: str) -> Optional[str]:
    for r in _history_by_query(query):
        if r["id"] != exclude_id and r.get("sel_company"):
            return r["sel_company"]
    return None


def list_history(q: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    # searched_at 단일 필드 정렬(자동 색인) 후 파이썬에서 고정 우선 재정렬
    docs = [{**d.to_dict(), "id": d.id}
            for d in _col("search_history")
            .order_by("searched_at", direction=firestore.Query.DESCENDING)
            .limit(200).stream()]
    if q:
        docs = [d for d in docs if q in (d.get("query") or "")]
    docs.sort(key=lambda d: (d.get("pinned") or 0, d.get("searched_at") or ""),
              reverse=True)
    return docs[:limit]


def select_candidate(hid: str, company: str, price: Optional[int], spec: Optional[str],
                     verify_url: Optional[str], item_ref: Optional[int],
                     selected_at: str) -> Optional[dict[str, Any]]:
    ref = _col("search_history").document(hid)
    doc = ref.get()
    if not doc.exists:
        return None
    cur = doc.to_dict()
    changed = (cur.get("sel_changed") or 0) + (1 if cur.get("sel_company") else 0)
    ref.update({
        "sel_company": company, "sel_price": price, "sel_spec": spec,
        "sel_verify_url": verify_url, "sel_item_ref": item_ref,
        "sel_at": selected_at, "sel_changed": changed,
    })
    return get_history(hid)


def delete_history(hid: str) -> bool:
    ref = _col("search_history").document(hid)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def toggle_pin(hid: str) -> Optional[dict[str, Any]]:
    ref = _col("search_history").document(hid)
    doc = ref.get()
    if not doc.exists:
        return None
    ref.update({"pinned": 0 if doc.to_dict().get("pinned") else 1})
    return get_history(hid)


# ── 지정 공급처 메모 (supplier_notes) ─────────────────────────────

def list_notes() -> list[dict[str, Any]]:
    rows = [{**d.to_dict(), "id": d.id} for d in _col("supplier_notes").stream()]
    rows.sort(key=lambda r: (r.get("keyword") or "", r.get("supplier") or ""))
    return rows


def match_notes(query: str) -> list[dict[str, Any]]:
    normalized = query.replace(" ", "").lower()
    out = []
    for note in list_notes():
        kw = (note.get("keyword") or "").replace(" ", "").lower()
        if kw and kw in normalized:
            out.append(note)
    return out


def add_note(keyword: str, supplier: str, memo: Optional[str], contact: Optional[str],
             url: Optional[str], updated_at: str) -> str:
    ref = _col("supplier_notes").document()
    ref.set({"keyword": keyword, "supplier": supplier, "memo": memo,
             "contact": contact, "url": url, "updated_at": updated_at})
    return ref.id


def update_note(nid: str, keyword: str, supplier: str, memo: Optional[str],
                contact: Optional[str], url: Optional[str], updated_at: str) -> bool:
    ref = _col("supplier_notes").document(nid)
    if not ref.get().exists:
        return False
    ref.update({"keyword": keyword, "supplier": supplier, "memo": memo,
                "contact": contact, "url": url, "updated_at": updated_at})
    return True


def delete_note(nid: str) -> bool:
    ref = _col("supplier_notes").document(nid)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def seed_notes_if_empty() -> None:
    from datetime import datetime, timezone
    if list_notes():
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seeds = [
        ("레미콘", "전북서부레미콘사업협동조합",
         "관급 레미콘은 조합 경유 구매. 쇼핑몰 단가는 참고만 (서부권: 군산·김제·부안 등)", "063-XXX-XXXX", None),
        ("아스콘", "전북아스콘공업협동조합",
         "관급 아스콘은 조합 경유 수의계약 — 종합쇼핑몰 미등록일 수 있어 단가 자동조회 제한적", "063-XXX-XXXX", None),
    ]
    for kw, sup, memo, contact, url in seeds:
        add_note(kw, sup, memo, contact, url, now)


# ── 품명 동의어 사전 (synonyms) — 문서 ID = 정규형 alias ───────────

def _norm_alias(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


_SYN_CACHE: dict[str, dict[str, Any]] = {}


def load_synonym_cache() -> None:
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
    out = []
    for d in _col("synonyms").stream():
        v = d.to_dict()
        out.append({
            "id": d.id, "alias": v.get("alias") or d.id,
            "canonicals": v.get("canonicals") or [],
            "extra_filters": v.get("extra_filters") or [],
            "verified": bool(v.get("verified")),
        })
    out.sort(key=lambda x: x["alias"] or "")
    return out


def upsert_synonym(alias: str, canonicals: list[str], extra_filters: list[str],
                   verified: bool, updated_at: str) -> None:
    a = _norm_alias(alias)
    _col("synonyms").document(a).set({
        "alias": a, "canonicals": canonicals, "extra_filters": extra_filters or [],
        "verified": bool(verified), "updated_at": updated_at,
    })
    load_synonym_cache()


def delete_synonym(sid: str) -> bool:
    ref = _col("synonyms").document(sid)
    if not ref.get().exists:
        return False
    ref.delete()
    load_synonym_cache()
    return True


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
    # 연결볼트 단독 검색 대응 — 맨홀 부속 용도 (지시서 #3 작업 3)
    ("연결볼트", ["콘크리트맨홀블록"], ["연결볼트"], True),
]
