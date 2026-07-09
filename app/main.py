"""FastAPI 진입점 — 프론트 서빙 + API 중계 + 이력/메모/CSV/PNG."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import secrets
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import capture, config, db, procurement, runtime, search

app = FastAPI(title="관급자재 단가조회", version="0.5")

STATIC_DIR = config.ROOT / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    db.seed_admin_if_empty()
    db.seed_notes_if_empty()
    db.seed_synonyms_if_empty()
    db.load_synonym_cache()


# ── 관리자 인증 ───────────────────────────────────────────────────
# 로컬 업무 도구용 경량 인증: 비밀번호 확인 → 메모리 토큰 발급.
# 서버 재시작 시 토큰 소멸(재로그인). 인증키 노출을 막는 문지기 역할.
_ADMIN_TOKENS: set[str] = set()


def _pw_hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def require_admin(x_admin_token: Optional[str] = Header(None)) -> None:
    if not x_admin_token or x_admin_token not in _ADMIN_TOKENS:
        raise HTTPException(401, "관리자 인증이 필요합니다")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── 검색 (F1~F4, F8) ──────────────────────────────────────────────

@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    months: int = Query(6),
    debug: int = Query(0),
) -> JSONResponse:
    if months not in (1, 3, 6, 12):
        months = 6
    warnings: list[str] = []

    # 검색어 정규화: 통칭 → 세부품명(복수 가능) + 규격 토큰 (F1 동의어 사전)
    resolved = search.resolve_query(q, db.synonym_cache())
    canonicals = resolved["canonicals"]
    spec_tokens = resolved["specTokens"]
    normalized_query = " ".join(canonicals + spec_tokens)

    # 일반 검색과 지정 공급처(협동조합) 조회를 동시에 실행
    notes = db.match_notes(q)
    raw_items, designated_all = await asyncio.gather(
        procurement.fetch_items(canonicals, spec_tokens, months, warnings),
        procurement.fetch_supplier_items(
            canonicals, spec_tokens, months, [n["supplier"] for n in notes], warnings),
    )
    # 중간가는 일반 시장(raw) 기준으로 산출 — 지정 조합 물량이 median을 왜곡하지 않도록
    market_median = search.compute_summary(list(raw_items)).get("median")
    designated = search.reduce_designated(designated_all, market_median)
    all_items = search.merge_designated(raw_items, designated)

    summary = search.compute_summary(all_items)
    median = summary.get("median")
    items = search.sort_items(all_items, median)
    candidates = search.pick_candidates(items, median)

    # 이력 자동 저장 (F6)
    unit = next((it.get("unit") for it in items if it.get("unit")), None)
    hid = db.add_history(q, months, _now_iso(), summary.get("count", 0),
                         median, unit, candidates)

    # 직전 회차 비교 (F6)
    prev = db.prev_history(q, hid)
    prev_selected = db.prev_selection(q, hid)

    summary["unit"] = f"원/{unit}" if unit else "원"
    if prev and prev.get("median_prc"):
        summary["prevMedian"] = prev["median_prc"]
        summary["prevSearchedAt"] = (prev.get("searched_at") or "")[:10]
        summary["prevSelected"] = prev_selected

    warn = search.sample_warning(summary.get("count", 0))
    if warn:
        warnings.insert(0, warn)
    if not resolved["matched"] and summary.get("count", 0) == 0:
        warnings.append("조달청 세부품명이 달라 결과가 없을 수 있어요 — 관리자 메뉴의 "
                        "'품명 사전'에 이 통칭의 세부품명을 등록하면 다음부터 검색됩니다")

    # 세부품명별 요약 (복수 매핑 시 분리 표시 — 자연석 vs 콘크리트처럼 사실상 다른 자재)
    by_canonical = []
    if len(canonicals) > 1:
        for canon in canonicals:
            prices = [it["price"] for it in items
                      if it.get("sourceCanonical") == canon and it.get("price")]
            by_canonical.append({
                "canonical": canon, "count": len(prices),
                "median": int(statistics.median(prices)) if prices else None,
            })

    payload: dict[str, Any] = {
        "query": q,
        "normalizedQuery": normalized_query if resolved["matched"] else None,
        "matchedAlias": resolved["alias"],
        "canonicals": canonicals,
        "byCanonical": by_canonical,
        "months": months,
        "period": _period(months),
        "supplierNotes": notes,
        "summary": summary,
        "historyId": hid,
        "candidates": candidates,
        "items": items,
        "diagnostics": {
            "demoMode": runtime.is_demo(),
            "warnings": warnings,
        },
    }
    if debug:
        payload["diagnostics"]["adopted"] = procurement._ADOPTED
    return JSONResponse(payload)


def _period(months: int) -> str:
    from datetime import timedelta
    end = datetime.now()
    start = end - timedelta(days=months * 30)
    return f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}"


# ── 선정 (F8) ─────────────────────────────────────────────────────

class SelectBody(BaseModel):
    company: str
    price: Optional[int] = None
    spec: Optional[str] = None
    verifyUrl: Optional[str] = None
    itemRef: Optional[int] = None


@app.post("/api/history/{hid}/select")
def api_select(hid: str, body: SelectBody) -> JSONResponse:
    row = db.select_candidate(hid, body.company, body.price, body.spec,
                              body.verifyUrl, body.itemRef, _now_iso())
    if row is None:
        raise HTTPException(404, "이력을 찾을 수 없습니다")
    return JSONResponse(row)


# ── 이력 (F6) ─────────────────────────────────────────────────────

@app.get("/api/history")
def api_history(q: Optional[str] = None, limit: int = 20) -> JSONResponse:
    return JSONResponse(db.list_history(q, limit))


@app.delete("/api/history/{hid}")
def api_history_delete(hid: str) -> JSONResponse:
    if not db.delete_history(hid):
        raise HTTPException(404, "이력을 찾을 수 없습니다")
    return JSONResponse({"ok": True})


@app.patch("/api/history/{hid}/pin")
def api_history_pin(hid: str) -> JSONResponse:
    row = db.toggle_pin(hid)
    if row is None:
        raise HTTPException(404, "이력을 찾을 수 없습니다")
    return JSONResponse(row)


# ── 공급처 메모 (F7) ──────────────────────────────────────────────

class NoteBody(BaseModel):
    keyword: str
    supplier: str
    memo: Optional[str] = None
    contact: Optional[str] = None
    url: Optional[str] = None


@app.get("/api/notes")
def api_notes() -> JSONResponse:
    return JSONResponse(db.list_notes())


@app.post("/api/notes")
def api_notes_add(body: NoteBody) -> JSONResponse:
    nid = db.add_note(body.keyword, body.supplier, body.memo,
                      body.contact, body.url, _now_iso())
    return JSONResponse({"id": nid})


@app.put("/api/notes/{nid}")
def api_notes_update(nid: str, body: NoteBody) -> JSONResponse:
    if not db.update_note(nid, body.keyword, body.supplier, body.memo,
                          body.contact, body.url, _now_iso()):
        raise HTTPException(404, "메모를 찾을 수 없습니다")
    return JSONResponse({"ok": True})


@app.delete("/api/notes/{nid}")
def api_notes_delete(nid: str) -> JSONResponse:
    if not db.delete_note(nid):
        raise HTTPException(404, "메모를 찾을 수 없습니다")
    return JSONResponse({"ok": True})


# ── 품명 동의어 사전 (F1) ─────────────────────────────────────────

class SynonymBody(BaseModel):
    alias: str
    canonicals: list[str]
    extra_filters: list[str] = []
    verified: bool = False


@app.get("/api/synonyms")
def api_synonyms() -> JSONResponse:
    return JSONResponse(db.list_synonyms())


@app.post("/api/synonyms")
def api_synonyms_add(body: SynonymBody) -> JSONResponse:
    if not body.alias.strip() or not [c for c in body.canonicals if c.strip()]:
        raise HTTPException(400, "통칭과 세부품명은 필수입니다")
    db.upsert_synonym(body.alias, [c.strip() for c in body.canonicals if c.strip()],
                      body.extra_filters, body.verified, _now_iso())
    return JSONResponse({"ok": True})


@app.delete("/api/synonyms/{sid}")
def api_synonyms_delete(sid: str) -> JSONResponse:
    if not db.delete_synonym(sid):
        raise HTTPException(404, "사전 항목을 찾을 수 없습니다")
    return JSONResponse({"ok": True})


# ── CSV 내보내기 (F5) ─────────────────────────────────────────────

class ExportBody(BaseModel):
    query: str
    months: int = 6
    summary: dict[str, Any] = {}
    supplierNotes: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    selection: Optional[dict[str, Any]] = None


@app.post("/api/export")
def api_export(body: ExportBody) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)

    # 메타 행
    w.writerow(["단가조사 기초자료 (조달청 계약단가 기준 참고자료)"])
    w.writerow(["검색어", body.query, "조회기간", f"{body.months}개월",
                "조회일시", _now_iso()])
    s = body.summary or {}
    w.writerow(["결과건수", s.get("count", ""), "최저", s.get("min", ""),
                "중간가", s.get("median", ""), "최고", s.get("max", ""),
                "단위", s.get("unit", "")])
    if body.supplierNotes:
        names = ", ".join(n.get("supplier", "") for n in body.supplierNotes)
        w.writerow(["지정 공급처", names])
    w.writerow(["후보 추출 규칙", "중간가 ±5% · [전북+인증]→[전북]→[인증]→[해당없음] 순 · 업체중복제거 상위3"])
    w.writerow([])

    # 후보 3선 + 선정 표시
    w.writerow(["[후보 3선 비교]"])
    w.writerow(["선정", "순위", "업체명", "단가", "규격", "소재지", "지정공급처", "전북", "인증", "검증링크"])
    sel_comp = (body.selection or {}).get("company")
    for c in body.candidates:
        mark = "✔" if sel_comp and c.get("company") == sel_comp else ""
        w.writerow([mark, c.get("rank", ""), c.get("company", ""),
                    c.get("price", ""), c.get("spec", ""), c.get("region", ""),
                    "지정" if c.get("isDesignated") else "",
                    "Y" if c.get("isJeonbuk") else "", "Y" if c.get("isCertified") else "",
                    c.get("verifyUrl", "")])
    w.writerow([])

    # 전체 결과
    w.writerow(["[전체 결과]"])
    w.writerow(["품목명", "규격", "단가", "단위", "업체명", "소재지",
                "계약일자", "출처", "전북", "인증", "중간가인접", "이상치", "검증링크"])
    for it in body.items:
        w.writerow([it.get("name", ""), it.get("spec", ""), it.get("price", ""),
                    it.get("unit", ""), it.get("company", ""), it.get("region", ""),
                    it.get("contractDate", ""), it.get("source", ""),
                    "Y" if it.get("isJeonbuk") else "", "Y" if it.get("isCertified") else "",
                    "Y" if it.get("nearMedian") else "", "Y" if it.get("isOutlier") else "",
                    it.get("verifyUrl", "")])

    data = "﻿" + buf.getvalue()  # BOM (엑셀 한글 깨짐 방지)
    fname = f"단가조사_{body.query}_{_now_date()}.csv".replace(" ", "_")
    from urllib.parse import quote
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


# ── PNG 이미지 저장 (F9) ──────────────────────────────────────────

class ImageBody(BaseModel):
    name: Optional[str] = None
    company: Optional[str] = None
    spec: Optional[str] = None
    price: Optional[int] = None
    region: Optional[str] = None
    source: Optional[str] = None
    contractDate: Optional[str] = None
    isJeonbuk: bool = False
    isCertified: bool = False
    verifyUrl: Optional[str] = None


@app.post("/api/image")
async def api_image(body: ImageBody) -> Response:
    selection = body.model_dump()
    png, note = await capture.build_png(selection)
    name = selection.get("name") or selection.get("company") or "품목"
    spec = selection.get("spec") or ""
    fname = f"단가조서_{name}({spec})_{_now_date()}.png".replace(" ", "_")
    from urllib.parse import quote
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"}
    if note:
        headers["X-Capture-Note"] = quote(note)
    return Response(content=png, media_type="image/png", headers=headers)


# ── Probe 자가진단 (6.4) ──────────────────────────────────────────

@app.get("/api/probe")
async def api_probe() -> JSONResponse:
    report = await procurement.probe()
    return JSONResponse({"demoMode": runtime.is_demo(), "report": report})


# ── 관리자 API ────────────────────────────────────────────────────

class LoginBody(BaseModel):
    password: str


@app.post("/api/admin/login")
def admin_login(body: LoginBody) -> JSONResponse:
    stored = db.get_setting("admin_pw_hash")
    if not stored or _pw_hash(body.password) != stored:
        raise HTTPException(401, "비밀번호가 올바르지 않습니다")
    token = secrets.token_urlsafe(24)
    _ADMIN_TOKENS.add(token)
    return JSONResponse({"token": token})


@app.get("/api/admin/settings")
def admin_get_settings(_: None = Depends(require_admin)) -> JSONResponse:
    key = runtime.get_service_key()
    return JSONResponse({
        "hasKey": bool(key),
        "maskedKey": runtime.masked_key(),
        "demoFlag": runtime.demo_flag(),
        "isDemo": runtime.is_demo(),
    })


class SettingsBody(BaseModel):
    serviceKey: Optional[str] = None   # None=변경 안 함, ""=삭제
    demoMode: Optional[bool] = None


@app.put("/api/admin/settings")
def admin_put_settings(body: SettingsBody, _: None = Depends(require_admin)) -> JSONResponse:
    if body.serviceKey is not None:
        runtime.set_service_key(body.serviceKey)
    if body.demoMode is not None:
        runtime.set_demo(body.demoMode)
    procurement.reset_adopted()   # 키/모드 변경 → 오퍼레이션 재탐색
    return admin_get_settings()


class PasswordBody(BaseModel):
    current: str
    new: str


@app.post("/api/admin/password")
def admin_change_password(body: PasswordBody, _: None = Depends(require_admin)) -> JSONResponse:
    stored = db.get_setting("admin_pw_hash")
    if not stored or _pw_hash(body.current) != stored:
        raise HTTPException(401, "현재 비밀번호가 올바르지 않습니다")
    if len(body.new.strip()) < 4:
        raise HTTPException(400, "새 비밀번호는 4자 이상이어야 합니다")
    db.set_setting("admin_pw_hash", _pw_hash(body.new.strip()))
    return JSONResponse({"ok": True})


# ── 프론트 서빙 ───────────────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico")


if (STATIC_DIR).exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
