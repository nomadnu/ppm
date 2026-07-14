"""도메인 로직 — 중간가 산출(F2), 4단계 우선 정렬(F3),
후보 3선 추출(F8), 나라장터 검증 링크 생성(F4).

순수 함수 위주로 작성해 단위 테스트가 쉽도록 한다.
"""
from __future__ import annotations

import re
import statistics
import urllib.parse
from typing import Any, Optional

from .config import G2B_GOODS_SEARCH

# ── 검색어 정규화·동의어 해석 (F1) ────────────────────────────────
_HD_RE = re.compile(r"\bH?D(\d{2})\b", re.IGNORECASE)   # HD13/H13 → D13
_FULLWIDTH = {0xFF01 + i: 0x21 + i for i in range(94)}   # 전각 → 반각


def norm_alias(text: str) -> str:
    """alias 정규형: 공백 제거 + 소문자."""
    return re.sub(r"\s+", "", str(text or "")).lower()


def normalize_spec_token(tok: str) -> str:
    """규격 표기 정규화: 전각→반각, HD13/H13→D13."""
    t = str(tok or "").translate(_FULLWIDTH)
    return _HD_RE.sub(r"D\1", t).strip()


# 수식어(접두) — alias 매칭 실패 시 벗겨내고 재시도 (지시서 #3 작업 3)
_ALIAS_MODIFIER_RE = re.compile(r"(?i)^\s*(조립식|프리캐스트|pc|pe)\s*")


def resolve_query(query: str, syn_cache: dict[str, dict]) -> dict[str, Any]:
    """검색어 → {alias, canonicals[], specTokens[], entry}. 사전 alias 매칭.
    실패 시 앞쪽 수식어(조립식·PC 등)를 벗기고 한 번 더 시도한다."""
    r = _resolve_once(query, syn_cache)
    if r["matched"]:
        return r
    stripped = _ALIAS_MODIFIER_RE.sub("", query, count=1).strip()
    if stripped and stripped != query.strip():
        r2 = _resolve_once(stripped, syn_cache)
        if r2["matched"]:
            return r2
    return r


def _resolve_once(query: str, syn_cache: dict[str, dict]) -> dict[str, Any]:
    tokens = query.split()
    best_alias, consumed = None, 0
    joined = ""
    for i, t in enumerate(tokens):
        joined += norm_alias(t)
        if joined in syn_cache:
            best_alias, consumed = joined, i + 1
    spec: list[str] = []
    if best_alias:
        spec = [normalize_spec_token(t) for t in tokens[consumed:]]
    else:
        # 공백 없이 붙여 쓴 입력 대비: 전체 정규형의 최장 alias 접두 매칭
        full = norm_alias(query)
        for alias in sorted(syn_cache, key=len, reverse=True):
            if full.startswith(alias):
                best_alias = alias
                rest = full[len(alias):]
                spec = [normalize_spec_token(rest)] if rest else []
                break

    if best_alias:
        entry = syn_cache[best_alias]
        spec = [s for s in spec if s] + [normalize_spec_token(f)
                                         for f in entry.get("extra_filters", [])]
        return {"alias": best_alias, "canonicals": list(entry.get("canonicals") or []),
                "specTokens": spec, "entry": entry, "matched": True}

    name = tokens[0] if tokens else query
    spec = [normalize_spec_token(t) for t in tokens[1:]]
    return {"alias": None, "canonicals": [name], "specTokens": spec,
            "entry": None, "matched": False}


OUTLIER_RATIO = 0.70   # 중앙값 ±70% 벗어나면 이상치 가능
NEAR_MEDIAN_RATIO = 0.05  # 후보군: 중간가 ±5%
JEONBUK_TOKENS = ("전북특별자치도", "전라북도", "전북")


# ── 중간가 (F2) ───────────────────────────────────────────────────

def compute_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """단가 목록에서 최저/중간/최고/건수 산출. 이상치 플래그도 부여."""
    prices = [it["price"] for it in items if isinstance(it.get("price"), (int, float)) and it["price"] > 0]
    if not prices:
        return {"count": len(items), "min": None, "median": None, "max": None}

    median = statistics.median(prices)
    median = int(round(median))
    lo, hi = min(prices), max(prices)

    # 이상치·중간가 인접 플래그 부여
    near = _nearest_index(items, median)
    for idx, it in enumerate(items):
        p = it.get("price")
        if not isinstance(p, (int, float)) or p <= 0:
            it["isOutlier"] = False
            it["nearMedian"] = False
            continue
        it["isOutlier"] = abs(p - median) > median * OUTLIER_RATIO
        it["nearMedian"] = (idx == near)

    return {
        "count": len(prices),
        "min": int(lo),
        "median": median,
        "max": int(hi),
    }


def sanity_filter_prices(items: list[dict[str, Any]], warnings: list[str]) -> int:
    """단가 타당성 검증(지시서 #3 작업 1-3) — 출처 단위로 ID성/비정상 단가를 기각.
    (a) 10억 초과·0 이하  (b) 정렬 시 인접 차이가 대부분 1~2인 연속 정수(거대값) → ID로 판정.
    걸린 건은 price=None(중간가 계산 제외). 기각 건수를 반환하고 경고를 추가한다."""
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        p = it.get("price")
        if isinstance(p, (int, float)):
            groups[it.get("source") or ""].append(it)

    excluded = 0
    for group in groups.values():
        prices = sorted(g["price"] for g in group)
        anyhuge = any(p > 1_000_000_000 or p <= 0 for p in prices)
        consec = False
        if len(prices) >= 4:
            diffs = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
            near1 = sum(1 for d in diffs if 0 <= d <= 2) / len(diffs)
            consec = near1 > 0.7 and prices[-1] > 1_000_000
        if anyhuge or consec:
            for g in group:
                g["price"] = None
                g["priceInvalid"] = True
            excluded += len(group)

    if excluded:
        warnings.append(f"단가 필드를 확인할 수 없어 제외된 결과 {excluded}건 "
                        f"(ID·비정상값 감지 — 틀린 단가 대신 제외 처리)")
    return excluded


def _nearest_index(items: list[dict[str, Any]], median: int) -> int:
    best_idx, best_diff = -1, None
    for idx, it in enumerate(items):
        p = it.get("price")
        if not isinstance(p, (int, float)) or p <= 0:
            continue
        diff = abs(p - median)
        if best_diff is None or diff < best_diff:
            best_idx, best_diff = idx, diff
    return best_idx


# ── 판정 (F3) ─────────────────────────────────────────────────────

def is_jeonbuk(region: Optional[str]) -> bool:
    if not region:
        return False
    return any(tok in region for tok in JEONBUK_TOKENS)


def grade(item: dict[str, Any]) -> int:
    """4단계 등급: 1=[전북+인증] 2=[전북] 3=[인증] 4=[해당없음]. 낮을수록 우선."""
    jb = bool(item.get("isJeonbuk"))
    ct = bool(item.get("isCertified"))
    if jb and ct:
        return 1
    if jb:
        return 2
    if ct:
        return 3
    return 4


def sort_items(items: list[dict[str, Any]], median: Optional[int]) -> list[dict[str, Any]]:
    """등급 오름차순 → 같은 등급 내 |단가-중간가| 오름차순."""
    m = median or 0

    def key(it: dict[str, Any]):
        p = it.get("price") or 0
        return (grade(it), abs(p - m) if p else float("inf"))

    return sorted(items, key=key)


# ── 후보 3선 (F8) ─────────────────────────────────────────────────

def merge_designated(items: list[dict[str, Any]],
                     designated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """지정 공급처 조회 결과를 전체 목록에 병합.
    이미 있는 동일 건이면 isDesignated 플래그만 세우고, 없으면 목록에 추가."""
    for d in designated:
        key = (d.get("company"), d.get("price"), d.get("spec"))
        match = next((it for it in items
                      if (it.get("company"), it.get("price"), it.get("spec")) == key), None)
        if match:
            match["isDesignated"] = True
            match["designatedFor"] = d.get("designatedFor")
        else:
            items.append(d)
    return items


def reduce_designated(designated: list[dict[str, Any]],
                      median: Optional[int]) -> list[dict[str, Any]]:
    """지정공급처 조회 결과를 업체별 대표 1건으로 축약(중간가 왜곡·과다병합 방지)."""
    return _best_designated_per_company(designated, median)


def _cand_card(it: dict[str, Any], idx: int, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "company": it.get("company"),
        "price": it.get("price"),
        "spec": it.get("spec"),
        "region": it.get("region"),
        "isJeonbuk": bool(it.get("isJeonbuk")),
        "isCertified": bool(it.get("isCertified")),
        "isDesignated": bool(it.get("isDesignated")),
        "isOutlier": bool(it.get("isOutlier")),
        "isSurveyPrice": bool(it.get("isSurveyPrice")),
        "contractDate": it.get("contractDate"),
        "source": it.get("source"),
        "itemRef": idx,
        "verifyUrl": it.get("verifyUrl"),
        "goodsUrl": it.get("goodsUrl"),
        "identNo": it.get("identNo"),
        "imageUrl": it.get("imageUrl"),
    }


def _best_designated_per_company(items: list[dict[str, Any]],
                                 median: Optional[int]) -> list[dict[str, Any]]:
    """지정공급처 아이템을 업체별 1건(중간가 근접, median 없으면 최저가)으로 축약."""
    m = median or 0
    by_company: dict[str, dict[str, Any]] = {}
    for it in items:
        comp = (it.get("company") or "").strip()
        cur = by_company.get(comp)
        if cur is None:
            by_company[comp] = it
            continue
        better = abs(it["price"] - m) < abs(cur["price"] - m) if m else it["price"] < cur["price"]
        if better:
            by_company[comp] = it
    reps = list(by_company.values())
    reps.sort(key=lambda it: (grade(it), abs(it["price"] - m) if m else it["price"]))
    return reps


def pick_candidates(items: list[dict[str, Any]], median: Optional[int]) -> list[dict[str, Any]]:
    """후보 3선. 지정 공급처(협동조합)는 **무조건 우선 포함**하고(F7↔F8),
    남은 자리를 중간가 ±5% + 4단계 등급으로 채운다. 업체 중복 제거.
    """
    if not items:
        return []

    idx_of = {id(it): i for i, it in enumerate(items)}
    out: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(it: dict[str, Any]) -> None:
        out.append(_cand_card(it, idx_of[id(it)], len(out) + 1))
        used.add((it.get("company") or "").strip())

    # 1) 지정 공급처 강제 포함 (이상치·밴드 무관 — 사용자 요구: 무조건)
    designated = [it for it in items if it.get("isDesignated") and it.get("price")]
    for it in _best_designated_per_company(designated, median):
        if len(out) >= 3:
            break
        if (it.get("company") or "").strip() in used:
            continue
        add(it)

    # 2) 남은 자리를 일반 후보로 채움 (중간가 ±5% → 부족 시 인접 6건)
    if len(out) < 3 and median:
        indexed = [it for it in items
                   if it.get("price") and not it.get("isOutlier")
                   and not it.get("isDesignated")]
        band = [it for it in indexed
                if abs(it["price"] - median) <= median * NEAR_MEDIAN_RATIO]
        if len(band) < 3:
            band = sorted(indexed, key=lambda it: abs(it["price"] - median))[:6]
        band.sort(key=lambda it: (grade(it), abs(it["price"] - median)))
        for it in band:
            if len(out) >= 3:
                break
            if (it.get("company") or "").strip() in used:
                continue
            add(it)
    return out


def sample_warning(count: int) -> Optional[str]:
    if count == 0:
        return "결과 없음 — 검색어를 줄여보세요 (예: 규격 뒷부분 생략)"
    if count < 3:
        return "표본 부족 — 결과가 3건 미만이라 후보 비교의 신뢰도가 낮습니다"
    return None


# ── 검증 링크 (F4, v0.5.1) ────────────────────────────────────────
# ⚠ 이 URL은 사용자 브라우저에서만 열린다(로그인 없이 열람 확인됨).
#    서버는 절대 fetch/접속하지 않는다 — 나라장터가 자동 접근을 SSO·봇으로 차단.
DETAIL_LINK_BASE = "https://shop.g2b.go.kr/link/GMSF001_01/?ctrtItemMngNo="


def build_detail_url(cntrct_no: str) -> Optional[str]:
    """하이픈 3분절 계약번호 → 종합쇼핑몰 상세 딥링크 (지시서 공식, 표본 2건 검증).
    예: 'R25TA00248570-01-3' → ...?ctrtItemMngNo=R25TA00248570010000003"""
    parts = str(cntrct_no).strip().split("-")
    if len(parts) != 3:
        return None
    p1, p2, p3 = parts
    return DETAIL_LINK_BASE + p1 + p2.zfill(2) + p3.zfill(7)


def build_goods_url(raw: dict[str, Any]) -> str:
    """물품목록정보시스템(공개) 검색 링크 — 물품식별번호로 '실물 존재'를 항상 확인.
    거래정지·상품코드미존재로 상세가 막혀도 이 링크는 총 1건으로 뜬다."""
    ident = _first_by_keys(raw, ("prdctidntno", "goodsidntfcno", "idntfcno"))
    if ident and str(ident).strip():
        return f"{G2B_GOODS_SEARCH}?searchGoodsIdntfcNo={urllib.parse.quote(str(ident).strip())}"
    return ""


def build_verify_url(item: dict[str, Any], raw: dict[str, Any]) -> str:
    """검증 링크 생성. 서버는 생성만 하고 접속하지 않는다.

    1) 계약 상세 딥링크 (브라우저에서 로그인 없이 종합쇼핑몰 상세가 열림) ⭐
       - 우리 API: shopngCntrctNo(계약번호+차수) + shopngCntrctSno(품목순번 7자리 패딩)
       - 또는 하이픈 3분절 계약번호 필드가 있으면 build_detail_url 공식
    2) 폴백: 물품목록정보시스템 공개 검색 (물품식별번호) — 로그인 없이 실물 확인
    """
    cno = _first_by_keys(raw, ("shopngcntrctno", "cntrctno", "ctrtno"))
    if cno:
        cno = str(cno).strip()
        if "-" in cno:
            u = build_detail_url(cno)
            if u:
                return u
        else:
            sno = _first_by_keys(raw, ("shopngcntrctsno", "cntrctsno", "ctrtsno"))
            if sno not in (None, "", 0):
                return DETAIL_LINK_BASE + cno + str(sno).strip().zfill(7)

    # 폴백: 물품목록정보시스템 공개 품목검색 (식별번호)
    ident = _first_by_keys(raw, ("prdctidntno", "goodsidntfcno", "idntfcno"))
    if ident and str(ident).strip():
        return f"{G2B_GOODS_SEARCH}?searchGoodsIdntfcNo={urllib.parse.quote(str(ident).strip())}"
    return ""


def _first_by_keys(raw: dict[str, Any], key_substrs: tuple[str, ...]) -> Optional[str]:
    # key_substrs 우선순위 순서를 존중 (앞선 후보가 먼저 매칭되도록 바깥 루프로)
    for sub in key_substrs:
        for k, v in raw.items():
            if sub in k.lower() and v not in (None, "", 0):
                return str(v)
    return None
