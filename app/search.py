"""도메인 로직 — 중간가 산출(F2), 4단계 우선 정렬(F3),
후보 3선 추출(F8), 나라장터 검증 링크 생성(F4).

순수 함수 위주로 작성해 단위 테스트가 쉽도록 한다.
"""
from __future__ import annotations

import statistics
import urllib.parse
from typing import Any, Optional

from .config import G2B_GOODS_SEARCH

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
        "contractDate": it.get("contractDate"),
        "source": it.get("source"),
        "itemRef": idx,
        "verifyUrl": it.get("verifyUrl"),
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


# ── 검증 링크 (F4) ────────────────────────────────────────────────

def build_verify_url(item: dict[str, Any], raw: dict[str, Any]) -> str:
    """1) 응답 URL 필드 → 2) 물품식별번호 상세검색 → 3) 품목명+규격 검색 폴백.

    나라장터 물품목록정보시스템(goods.g2b.go.kr)의 공개 품목검색으로 연결한다.
    로그인 없이 열리며, 물품식별번호로 실물이 나라장터에 등록돼 있음을 확인('인증샷')할 수 있다.
    (종합쇼핑몰 shop.g2b.go.kr 상세는 SSO 로그인 필수라 공개 딥링크 불가.)
    """
    # 1) 물품식별번호로 정확히 그 물건 조회 (총 1건)
    ident = _first_by_keys(raw, ("prdctidntno", "goodsidntfcno", "idntfcno"))
    if ident and str(ident).strip():
        return f"{G2B_GOODS_SEARCH}?searchGoodsIdntfcNo={urllib.parse.quote(str(ident).strip())}"

    # 2) 물품분류번호로 조회 (해당 품목군)
    clsfc = _first_by_keys(raw, ("prdctclsfcno", "goodsclsfcno", "clsfcno"))
    if clsfc and str(clsfc).strip():
        return f"{G2B_GOODS_SEARCH}?searchGoodsClsfcNo={urllib.parse.quote(str(clsfc).strip())}"

    # 3) 품명으로 조회 (폴백)
    nm = (item.get("name") or "").strip()
    if nm:
        return f"{G2B_GOODS_SEARCH}?searchGoodsNm={urllib.parse.quote(nm)}"
    return ""


def _first_by_keys(raw: dict[str, Any], key_substrs: tuple[str, ...]) -> Optional[str]:
    # key_substrs 우선순위 순서를 존중 (앞선 후보가 먼저 매칭되도록 바깥 루프로)
    for sub in key_substrs:
        for k, v in raw.items():
            if sub in k.lower() and v not in (None, "", 0):
                return str(v)
    return None
