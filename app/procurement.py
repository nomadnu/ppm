"""조달청 오픈 API 클라이언트.

- 오퍼레이션 자동 탐색(Probe, 설계 6.4): 후보 목록을 순서대로 호출해 살아있는 조합 채택·캐싱
- 필드 퍼지 매핑(설계 6.5): 정확한 필드명에 의존하지 않는 추출 규칙
- 데모 모드: 인증키 없이 합성 데이터로 UI/로직 확인

실제 오퍼레이션명·파라미터는 Day 1 Probe에서 실키로 확정한다. 아래 후보 배열은
공공데이터포털 문서 기준의 추정치이며, /api/probe 로 생존 조합을 가시화한다.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import httpx

from . import config, runtime
from .search import build_goods_url, build_verify_url, is_jeonbuk

# ── 오퍼레이션 후보 목록 (Probe 대상, 5종) ────────────────────────
# 각 항목: 표시명·역할·경로·오퍼레이션명 후보·검색 파라미터명 후보.
# path/operations/query_params 는 문서 기반 "추정치" — 공공데이터포털 End Point로 확정 예정.
#   use_in_search=True 인 서비스만 실제 검색에 사용(설계상 MVP는 ①②).
#   extra_params: 날짜범위 등 필수 파라미터. {bgn}{end}=yyyymmdd, {bgn_dt}{end_dt}=yyyymmddHHMM
OPERATION_CANDIDATES: list[dict[str, Any]] = [
    {  # ① 검색의 본체 — 실데이터로 확정 (2026-07 실접속 검증)
        "service": "종합쇼핑몰 품목정보",
        "role": "핵심(MVP)",
        "use_in_search": True,
        "path": "/1230000/at/ShoppingMallPrdctInfoService",
        "operations": ["getMASCntrctPrdctInfoList",       # 다수공급자계약(관급자재 주 경로)
                       "getThptyUcntrctPrdctInfoList",     # 제3자단가계약
                       "getUcntrctPrdctInfoList"],         # 일반단가계약
        "query_params": ["prdctClsfcNoNm", "prdctIdntNoNm"],
        "extra_params": {"inqryDiv": "1", "inqryBgnDate": "{bgn}", "inqryEndDate": "{end}"},
    },
    {  # ② 시설공통자재 가격 — 실데이터로 확정 (2026-07 실접속 검증)
        "service": "가격정보현황",
        "role": "핵심(MVP)",
        "use_in_search": True,
        "path": "/1230000/ao/PriceInfoService",
        "operations": ["getPriceInfoListFcltyCmmnMtrilTotal",   # 시설공통자재 전체
                       "getPriceInfoListFcltyCmmnMtrilEngrk",   # 토목
                       "getPriceInfoListFcltyCmmnMtrilBildng"], # 건축
        "query_params": ["prdctClsfcNoNm", "krnPrdctNm"],
        "extra_params": {"inqryDiv": "1", "inqryBgnDate": "{bgn}", "inqryEndDate": "{end}"},
    },
    {  # ③ 품명→분류번호 변환 (보조)
        "service": "물품목록정보",
        "role": "보조(v1.1)",
        "use_in_search": False,
        "path": "/1230000/ao/ThngListInfoService02",
        "operations": ["getPrdctClsfcNoUnit2Info02", "getPrdctClsfcNoUnit4Info02",
                       "getLsfgdNdPrdlstChghstlnfoSttus02"],
        "query_params": ["prdctClsfcNoNm", "prdctClsfcNo"],
    },
    {  # ④ 일반 계약 건 단가 사례 (확장)
        "service": "나라장터 계약정보",
        "role": "확장(v0.3)",
        "use_in_search": False,
        "path": "/1230000/ao/CntrctInfoService",
        "operations": ["getCntrctInfoListThngPPSSrch", "getCntrctInfoListThng",
                       "getCntrctInfoList"],
        "query_params": ["prdctClsfcNoNm", "cntrctNm", "inqryWrd"],
        "extra_params": {"inqryDiv": "1", "inqryBgnDate": "{bgn}", "inqryEndDate": "{end}"},
    },
    {  # ⑤ 유사 건 낙찰가 참고 (확장)
        "service": "나라장터 낙찰정보",
        "role": "확장(v0.3)",
        "use_in_search": False,
        "path": "/1230000/as/ScsbidInfoService",
        "operations": ["getOpengResultListInfoThngPPSSrch", "getOpengResultListInfoThng",
                       "getScsbidListSttusThngPPSSrch"],
        "query_params": ["prdctClsfcNoNm", "bidNtceNm"],
        "extra_params": {"inqryDiv": "1", "inqryBgnDate": "{bgn}", "inqryEndDate": "{end}"},
    },
]

# 채택된 조합 캐시: service → {operation, query_param}
_ADOPTED: dict[str, dict[str, str]] = {}


def reset_adopted() -> None:
    """인증키·데모모드 변경 시 채택 캐시를 비워 다음 호출에서 재탐색."""
    _ADOPTED.clear()


def _ensure_search_adopted() -> None:
    """검색용 ①② 서비스의 오퍼레이션을 확정값으로 시드(전체 probe 회피 — 속도).
    엔드포인트가 실접속 검증돼 있으므로 첫 후보를 바로 채택."""
    for c in OPERATION_CANDIDATES:
        if c.get("use_in_search"):
            _ADOPTED.setdefault(c["service"], {"operation": c["operations"][0],
                                               "query_param": c["query_params"][0]})


# ── 필드 퍼지 매핑 (6.5) ──────────────────────────────────────────

def _to_number(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(v))
    if not s or s in {"-", ".", "-."}:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _find(raw: dict[str, Any], substrs: tuple[str, ...], numeric: bool = False) -> Any:
    """키에 substr(우선순위 순) 이 포함된 첫 값을 반환."""
    for sub in substrs:
        for k, v in raw.items():
            if sub.lower() in k.lower():
                if numeric:
                    n = _to_number(v)
                    if n is not None:
                        return n
                elif v not in (None, ""):
                    return v
    return None


# ── 단가 추출 (지시서 #3 작업 1-2) ────────────────────────────────
# ID성 필드(가격공고번호 등)를 단가로 오인하지 않도록 제외 + 화이트리스트 우선순위.
_PRICE_EXCLUDE = ("no", "id", "sn", "seq", "regno", "mngno", "idnt", "num", "code", "cd")
_PRICE_WHITELIST = ("uprc", "untpc", "prce", "price", "amt")


def _extract_price(raw: dict[str, Any]) -> Optional[int]:
    """단가 필드 추출. 키에 ID성 토큰이 있으면 제외하고, 화이트리스트 우선순위로 탐색."""
    for token in _PRICE_WHITELIST:
        for k, v in raw.items():
            lk = k.lower()
            if token not in lk:
                continue
            if any(x in lk for x in _PRICE_EXCLUDE):   # prceNticeNo 등 ID 제외
                continue
            n = _to_number(v)
            if n is not None and n > 0:
                return n
    return None


_SIDO = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
         "경기", "강원", "충북", "충남", "전북", "전라북", "전남", "전라남",
         "경북", "경상북", "경남", "경상남", "제주")


def _scan_region(raw: dict[str, Any]) -> Optional[str]:
    for v in raw.values():
        if isinstance(v, str):
            for s in _SIDO:
                if s in v:
                    return v
    return None


def normalize_item(raw: dict[str, Any], source: str, warnings: list[str]) -> dict[str, Any]:
    """원본 응답 1건 → 표준 스키마. 매핑 실패는 빈 값 + 경고."""
    price = _extract_price(raw)
    name = _find(raw, ("prdctIdntNoNm", "krnPrdctNm", "PrdctNm", "ClsfcNoNm"))
    spec = _find(raw, ("prdctSpecNm", "Spec", "Stndrd"))
    unit = _find(raw, ("Unit", "prdctUnit"))
    company = _find(raw, ("CorpNm", "EntrpsNm", "scsbidCorpNm"))
    region = _find(raw, ("RgnNm", "Adrs")) or _scan_region(raw)

    certified, cert_info = _detect_cert(raw)

    contract_date = _find(raw, ("CntrctDt", "CntrctDate", "cntrctCnclsDate", "nticeDt"))
    contract_date = _fmt_date(contract_date)

    # 조사가격(가격정보현황)은 업체·지역·계약일이 없는 성격 (지시서 #3 작업 1-5)
    is_survey = "가격정보" in source

    if price is None:
        warnings.append(f"단가 매핑 실패: keys={list(raw.keys())[:8]}")

    item = {
        "name": name or "",
        "spec": spec or "",
        "price": price,
        "unit": unit or "",
        "company": company or "",
        "region": region or "",
        "isJeonbuk": is_jeonbuk(region),
        "isCertified": certified,
        "certInfo": cert_info,
        "contractDate": contract_date,
        "source": source,
        "isSurveyPrice": is_survey,
        "imageUrl": _find(raw, ("prdctImgUrl", "ImgUrl")) or "",
        "identNo": _find(raw, ("prdctIdntNo", "goodsIdntfcNo")) or "",
    }
    item["verifyUrl"] = build_verify_url(item, raw)   # 쇼핑몰 상세 딥링크(가격, 거래정지 시 막힘)
    item["goodsUrl"] = build_goods_url(raw)            # 목록정보시스템(항상 실물 확인)
    return item


_NONE_VALUES = {"", "해당 없음", "해당없음", "N", None}


def _detect_cert(raw: dict[str, Any]) -> tuple[bool, str]:
    """실제 종합쇼핑몰 스키마 기준 인증 판정.
    우수조달물품(exclncPrcrmntPrdctYn=Y) 또는 우선/의무구매대상·품질 인증 보유 시 True.
    (KS 등 기본 제품인증 prodctCertList는 거의 보편적이라 배지 기준에선 제외, 라벨로만 표기)
    """
    labels: list[str] = []
    if str(raw.get("exclncPrcrmntPrdctYn", "")).strip().upper() == "Y":
        labels.append("우수조달")
    for key, tag in (("prefrpurchsObjCertNm", "우선구매"),
                     ("dutyPurchsObjCertNm", "의무구매"),
                     ("qltyRltnCertInfo", "품질인증")):
        v = raw.get(key)
        if v is not None and str(v).strip() not in _NONE_VALUES:
            labels.append(tag)
    certified = bool(labels)
    # KS 등 제품인증은 참고 라벨로만
    prodct = raw.get("prodctCertList")
    if prodct and str(prodct).strip() not in _NONE_VALUES:
        labels.append(str(prodct).strip())
    return certified, " · ".join(labels)


def _fmt_date(v: Any) -> Optional[str]:
    if not v:
        return None
    s = re.sub(r"[^\d]", "", str(v))
    if len(s) >= 8:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return str(v)


# ── 실제 API 호출 ─────────────────────────────────────────────────

def _date_tokens(months: int) -> dict[str, str]:
    from datetime import datetime, timedelta
    end = datetime.now()
    bgn = end - timedelta(days=months * 30)
    return {
        "{bgn}": bgn.strftime("%Y%m%d"),
        "{end}": end.strftime("%Y%m%d"),
        "{bgn_dt}": bgn.strftime("%Y%m%d") + "0000",
        "{end_dt}": end.strftime("%Y%m%d") + "2359",
    }


async def _call(client: httpx.AsyncClient, cand: dict[str, Any], operation: str,
                query_param: str, keyword: str, months: int,
                extra: Optional[dict[str, str]] = None) -> Optional[list[dict]]:
    url = f"{config.API_GATEWAY}{cand['path']}/{operation}"
    params = {
        "serviceKey": runtime.get_service_key(),
        "type": "json",
        "numOfRows": "100",
        "pageNo": "1",
        query_param: keyword,
    }
    # 확장 서비스의 필수 파라미터(날짜범위 등) 주입
    tokens = _date_tokens(months)
    for k, v in cand.get("extra_params", {}).items():
        params[k] = tokens.get(v, v)
    if extra:                       # 추가 필터(예: 업체명 cntrctCorpNm)
        params.update(extra)
    try:
        r = await client.get(url, params=params)
        data = r.json()
    except Exception:
        return None
    # resultCode 확인
    header = _dig(data, ("response", "header")) or {}
    if str(header.get("resultCode", "")) not in ("00", "0", ""):
        return None
    items = _dig(data, ("response", "body", "items"))
    if isinstance(items, dict):
        items = items.get("item")
    if isinstance(items, dict):
        items = [items]
    if isinstance(items, list):
        return items
    return []   # resultCode 00 이지만 결과 0건 → "연결 정상, 빈 응답"(None=실패와 구분)


def _dig(d: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if isinstance(d, dict):
            d = d.get(key)
        else:
            return None
    return d


async def probe() -> list[dict[str, Any]]:
    """각 서비스별 후보 조합을 시도, 생존 조합을 채택·캐싱하고 표로 반환."""
    if runtime.is_demo():
        return [{"service": c["service"], "role": c.get("role", ""), "status": "DEMO",
                 "operation": "-", "queryParam": "-", "note": "데모 모드 — 실제 호출 안 함"}
                for c in OPERATION_CANDIDATES]

    report = []
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
        for cand in OPERATION_CANDIDATES:
            adopted = None      # 결과 rows 까지 확인된 조합
            alive = None        # resultCode 00 이지만 테스트 키워드엔 0건인 조합
            for op in cand["operations"]:
                for qp in cand["query_params"]:
                    items = await _call(client, cand, op, qp, "레미콘", 6)
                    if items:
                        adopted = {"operation": op, "query_param": qp}
                        break
                    if items == [] and alive is None:
                        alive = {"operation": op, "query_param": qp}
                if adopted:
                    break
            base = {"service": cand["service"], "role": cand.get("role", "")}
            if adopted:
                _ADOPTED[cand["service"]] = adopted
                report.append({**base, "status": "OK",
                               "operation": adopted["operation"],
                               "queryParam": adopted["query_param"], "note": ""})
            elif alive:
                _ADOPTED[cand["service"]] = alive
                report.append({**base, "status": "OK",
                               "operation": alive["operation"],
                               "queryParam": alive["query_param"],
                               "note": "연결 정상 (테스트 키워드 '레미콘' 미매칭 — 해당 품목 검색 시 응답)"})
            else:
                report.append({**base, "status": "FAIL", "operation": "-",
                               "queryParam": "-",
                               "note": "생존 조합 없음 — End Point/오퍼레이션명 확인 필요"})
    return report


_SPEC_FW = {0xFF01 + i: 0x21 + i for i in range(94)}   # 전각 → 반각


def _norm_spec(s: str) -> str:
    """규격 정규화: 전각→반각, 소문자, ×·x·* → '*' 통일, 공백 제거 (지시서 #3 작업 2)."""
    t = str(s or "").translate(_SPEC_FW).lower()
    t = re.sub(r"[×✕╳*x]", "*", t)
    return re.sub(r"\s+", "", t)


def _nums(s: str) -> list[str]:
    """규격 문자열에서 숫자 시퀀스만 순서대로 추출. 예: 'M20*220mm' → ['20','220']."""
    return re.findall(r"\d+", _norm_spec(s))


def _num_subseq(needle: list[str], hay: list[str]) -> bool:
    """needle 숫자열이 hay 숫자열의 '순서 유지 부분수열'이면 True."""
    i = 0
    for h in hay:
        if i < len(needle) and h == needle[i]:
            i += 1
    return i == len(needle)


def _spec_match(item: dict[str, Any], spec_tokens: list[str]) -> bool:
    """규격 매칭 — (1)정규화 문자열 포함 OR (2)숫자 시퀀스 부분수열. 둘 중 하나면 통과."""
    if not spec_tokens:
        return True
    raw_hay = f"{item.get('spec', '')} {item.get('name', '')}"
    hay = _norm_spec(raw_hay)
    hay_nums = _nums(raw_hay)
    for tok in spec_tokens:
        t = _norm_spec(tok)
        if t and t in hay:
            continue
        tnums = _nums(tok)
        if tnums and _num_subseq(tnums, hay_nums):
            continue
        return False
    return True


async def fetch_items(canonicals: list[str], spec_tokens: list[str],
                      months: int, warnings: list[str]) -> list[dict[str, Any]]:
    """세부품명(복수 가능)으로 서비스 호출 → 표준화 → 규격 로컬 2차 필터.
    각 아이템에 출처 세부품명(sourceCanonical) 표시. 데모 모드면 합성 데이터."""
    if runtime.is_demo():
        return _demo_items(canonicals, spec_tokens)

    _ensure_search_adopted()
    active = [c for c in OPERATION_CANDIDATES
              if c.get("use_in_search") and _ADOPTED.get(c["service"])]
    jobs = [(cand, canon) for canon in canonicals for cand in active]

    async def one(client: httpx.AsyncClient, cand: dict[str, Any], canon: str) -> list[dict]:
        adopted = _ADOPTED[cand["service"]]
        return await _call(client, cand, adopted["operation"],
                           adopted["query_param"], canon, months) or []

    raw_count = 0
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
        groups = await asyncio.gather(*(one(client, cand, canon) for cand, canon in jobs))
    for (cand, canon), raw_items in zip(jobs, groups):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            raw_count += 1
            item = normalize_item(raw, cand["service"], warnings)
            item["sourceCanonical"] = canon
            if _spec_match(item, spec_tokens):
                results.append(item)

    if spec_tokens and raw_count and not results:
        warnings.append(f"세부품명 {canonicals} {raw_count}건 중 규격 "
                        f"'{' '.join(spec_tokens)}' 일치 0건 — 규격 뒷부분을 줄여보세요")
    return results


async def fetch_supplier_items(canonicals: list[str], spec_tokens: list[str],
                               months: int, suppliers: list[str],
                               warnings: list[str]) -> list[dict[str, Any]]:
    """지정 공급처(협동조합)를 종합쇼핑몰에서 업체명으로 직접 조회 → isDesignated 아이템.
    후보 3선에 무조건 포함시키기 위한 재료(설계 F7↔F8). 규격은 로컬 필터 적용."""
    if not suppliers:
        return []
    if runtime.is_demo():
        return _demo_designated(canonicals, spec_tokens, suppliers)

    _ensure_search_adopted()
    cand = OPERATION_CANDIDATES[0]           # ① 종합쇼핑몰
    adopted = _ADOPTED.get(cand["service"])
    if not adopted:
        return []

    async def one(client: httpx.AsyncClient, sup: str, canon: str) -> list[dict[str, Any]]:
        raw_items = await _call(client, cand, adopted["operation"],
                                adopted["query_param"], canon, months,
                                extra={"cntrctCorpNm": sup, "numOfRows": "100"})
        found = []
        for raw in (raw_items or []):
            if not isinstance(raw, dict):
                continue
            item = normalize_item(raw, cand["service"], warnings)
            if _spec_match(item, spec_tokens):
                item["isDesignated"] = True
                item["designatedFor"] = sup
                item["sourceCanonical"] = canon
                found.append(item)
        return found

    jobs = [(sup, canon) for sup in suppliers for canon in canonicals]
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
        groups = await asyncio.gather(*(one(client, sup, canon) for sup, canon in jobs))
    out = [it for g in groups for it in g]
    for sup in suppliers:
        if not any(it.get("designatedFor") == sup for it in out):
            warnings.append(f"지정 공급처 '{sup}' — 종합쇼핑몰에서 해당 규격 단가를 "
                            f"찾지 못했습니다 (미등록이거나 업체명 불일치)")
    return out


# ── 품명 탐색 (F1-2) — ③ 물품목록으로 세부품명 후보 제시 ───────────
_THNG_LIST_URL = (config.API_GATEWAY
                  + "/1230000/ao/ThngListInfoService02/getPrdctClsfcNoUnit10Info02")
_MODIFIERS = ("조립식", "PC", "무근", "이형", "원심력", "진동및전압", "일반", "고급")


async def suggest_classifications(keyword: str, warnings: list[str],
                                  limit: int = 10) -> list[dict[str, Any]]:
    """③ 물품목록정보로 세부품명 후보 탐색. 사전 미등록·0건 품목의 검색을 돕는다.
    (완전일치 → 접두 → 포함 순 랭킹, 수식어 제거 재시도.) 데모면 빈 리스트."""
    kw = (keyword or "").strip()
    if runtime.is_demo() or not kw:
        return []

    async def fetch(word: str) -> list[dict[str, Any]]:
        params = {"serviceKey": runtime.get_service_key(), "type": "json",
                  "numOfRows": "50", "pageNo": "1", "dtilPrdctClsfcNoNm": word}
        try:
            async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
                data = (await client.get(_THNG_LIST_URL, params=params)).json()
        except Exception:
            return []
        items = _dig(data, ("response", "body", "items"))
        if isinstance(items, dict):
            items = items.get("item")
        if isinstance(items, dict):
            items = [items]
        out = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            if str(it.get("useYn", "Y")).upper() != "Y":
                continue
            name = it.get("dtilPrdctClsfcNoNm")
            if name:
                out.append({"name": name, "no": it.get("dtilPrdctClsfcNo"),
                            "desc": (it.get("dtilPrdctClsfcNoNmDscrpt") or "")[:70]})
        return out

    cands = await fetch(kw)
    if not cands:                       # 수식어 제거 후 재시도
        stripped = kw
        for m in _MODIFIERS:
            stripped = stripped.replace(m, "")
        stripped = stripped.strip()
        if stripped and stripped != kw:
            cands = await fetch(stripped)

    # 의미 있는 겹침(2글자 n-gram)만 유지 — '관' 한 글자 매칭 같은 잡음 제거
    grams = {kw[i:i + 2] for i in range(len(kw) - 1)}

    def relevant(name: str) -> bool:
        if not grams:
            return kw in name
        return any(g in name for g in grams) or name in kw

    def rank(c: dict[str, Any]):
        n = c["name"]
        r = 0 if n == kw else (1 if n.startswith(kw) else (2 if kw in n else 3))
        return (r, len(n), n)

    seen, uniq = set(), []
    for c in sorted((c for c in cands if relevant(c["name"])), key=rank):
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        uniq.append(c)
    return uniq[:limit]


def _demo_designated(canonicals: list[str], spec_tokens: list[str],
                     suppliers: list[str]) -> list[dict[str, Any]]:
    """데모 모드용 — 각 지정 공급처를 합성 단가로 생성해 후보 포함을 시연."""
    base_name = canonicals[0] if canonicals else "레미콘"
    spec = " ".join(spec_tokens) or "25-24-150"
    out = []
    for i, sup in enumerate(suppliers):
        raw = {
            "prdctIdntNoNm": base_name, "prdctSpec": spec,
            "prdctPrceAmt": 96500 + i * 300, "prdctUnit": "㎥",
            "cntrctCorpNm": sup, "cntrctRgnNm": "전북특별자치도",
            "excltPrdctYn": "N", "cntrctCnclsDate": "20260512",
            "prdctIdntNo": 30000000 + i,
        }
        item = normalize_item(raw, "종합쇼핑몰(데모)", [])
        item["isDesignated"] = True
        item["designatedFor"] = sup
        out.append(item)
    return out


# ── 데모 데이터 ───────────────────────────────────────────────────

def _demo_items(canonicals: list[str], spec_tokens: list[str]) -> list[dict[str, Any]]:
    """인증키 없이 UI/로직을 확인하기 위한 합성 데이터. 레미콘류 24건 정도 생성."""
    base_name = canonicals[0] if canonicals else "레미콘"
    spec = " ".join(spec_tokens) or "25-24-150"
    # 91,000 ~ 102,000 사이 24건, 일부 전북/인증
    samples = [
        ("○○레미콘(주)", 96500, "전북특별자치도", True, True),
        ("△△산업", 95800, "전북특별자치도", True, False),
        ("□□콘크리트", 96700, "충청남도", False, True),
        ("전북레미콘공업협동조합", 97000, "전북특별자치도", True, True),
        ("한빛레미콘", 91000, "경기도", False, False),
        ("대성콘크리트", 102000, "서울특별시", False, True),
        ("우진산업", 96200, "전라북도", True, False),
        ("가온레미콘", 99000, "전북특별자치도", True, False),
        ("동방콘크리트", 94500, "충청북도", False, False),
        ("신성레미콘", 96800, "경상남도", False, True),
        ("태양산업", 93000, "전북특별자치도", True, False),
        ("금강콘크리트", 100500, "인천광역시", False, False),
        ("하나레미콘", 96400, "전북특별자치도", True, True),
        ("백제산업", 95000, "충청남도", False, False),
        ("삼우콘크리트", 97500, "전라북도", True, False),
        ("명진레미콘", 145000, "서울특별시", False, False),  # 이상치
        ("청우산업", 92500, "강원특별자치도", False, False),
        ("대한레미콘", 96600, "전북특별자치도", True, False),
        ("서해콘크리트", 98200, "전라남도", False, True),
        ("동양레미콘", 95600, "경기도", False, False),
        ("남부산업", 96900, "전북특별자치도", True, True),
        ("에스콘크리트", 101000, "부산광역시", False, False),
        ("정읍레미콘", 96300, "전북특별자치도", True, False),
        ("군산콘크리트", 97100, "전북특별자치도", True, True),
    ]
    items = []
    for i, (comp, price, region, jb, cert) in enumerate(samples):
        raw = {
            "prdctIdntNoNm": base_name,
            "prdctSpec": spec,
            "prdctPrceAmt": price,
            "prdctUnit": "㎥",
            "cntrctCorpNm": comp,
            "cntrctRgnNm": region,
            "excltPrdctYn": "Y" if cert else "N",
            "cntrctCnclsDate": "20260512",
            "prdctIdntNo": 20000000 + i,
        }
        it = normalize_item(raw, "종합쇼핑몰(데모)", [])
        it["sourceCanonical"] = base_name
        items.append(it)
    return items
