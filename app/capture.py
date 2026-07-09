"""선정 결과 이미지 저장 (F9) — 실무 조서 붙임 형식.

기본: 나라장터 상세 페이지를 Playwright(헤드리스 Chromium)로 캡처 → Pillow로
"■ 물품명(규격)" 제목 밴드 합성 → PNG.

폴백(캡처 실패·Playwright 미설치·데모 모드): 자체 렌더링 요약 카드(Pillow)로 대체.
"""
from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from . import runtime

CAPTURE_WIDTH = 1280
CAPTURE_MAX_HEIGHT = 1400   # 상단(상품정보 영역) 위주로 자름
TITLE_BAND_H = 64


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """한글 폰트 로드 — Windows 맑은고딕 우선, 실패 시 기본 폰트."""
    for path in (r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _title_band(width: int, title: str) -> Image.Image:
    band = Image.new("RGB", (width, TITLE_BAND_H), "white")
    draw = ImageDraw.Draw(band)
    font = _load_font(28)
    draw.text((16, 16), title, fill="black", font=font)
    draw.line([(0, TITLE_BAND_H - 1), (width, TITLE_BAND_H - 1)], fill="#cccccc", width=1)
    return band


def _compose(title: str, body: Image.Image) -> bytes:
    """제목 밴드 + 본문 이미지 세로 결합."""
    w = body.width
    band = _title_band(w, title)
    out = Image.new("RGB", (w, band.height + body.height), "white")
    out.paste(band, (0, 0))
    out.paste(body, (0, band.height))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


async def _playwright_capture(url: str) -> Optional[Image.Image]:
    """나라장터 페이지 접속 → 상단 영역 캡처. 실패 시 None."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": CAPTURE_WIDTH, "height": 900})
            await page.goto(url, wait_until="networkidle", timeout=15000)
            png = await page.screenshot(clip={"x": 0, "y": 0,
                                              "width": CAPTURE_WIDTH,
                                              "height": min(CAPTURE_MAX_HEIGHT, 900)})
            await browser.close()
        return Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return None


def _fallback_card(selection: dict) -> Image.Image:
    """2차 폴백: 자체 렌더링 선정 요약 카드."""
    def g(key: str) -> str:
        v = selection.get(key)
        return str(v) if v not in (None, "") else "-"

    w, h = CAPTURE_WIDTH, 420
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    f_big = _load_font(30)
    f_mid = _load_font(24)
    f_small = _load_font(18)

    y = 24
    card_top, card_bottom = y, y + 360
    draw.rectangle([(24, card_top), (w - 24, card_bottom)], outline="#333333", width=2)
    pad = 48
    draw.text((pad, y + 24), g("company"), fill="black", font=f_big)
    price = selection.get("price")
    price_s = f"{price:,}원" if isinstance(price, (int, float)) else "-"
    draw.text((pad, y + 78), price_s, fill="#0a7d33", font=f_big)

    lines = [
        f"규격: {g('spec')}",
        f"소재지: {g('region')}",
        f"출처: {g('source')}",
        f"계약일: {g('contractDate')}",
    ]
    yy = y + 130
    for ln in lines:
        draw.text((pad, yy), ln, fill="#333", font=f_small)
        yy += 30

    badges = []
    if selection.get("isJeonbuk"):
        badges.append("전북")
    if selection.get("isCertified"):
        badges.append("인증")
    if badges:
        draw.text((pad, yy + 8), " · ".join(f"[{b}]" for b in badges),
                  fill="#c2410c", font=f_mid)

    draw.text((pad, card_bottom - 30),
              "※ 원본 캡처 대체 형식 — 조달청 계약단가 기준 참고자료",
              fill="#999", font=_load_font(14))
    return img


async def build_png(selection: dict) -> tuple[bytes, str]:
    """선정 항목 → (PNG bytes, 폴백여부 문구). 제목은 '■ 물품명(규격)'."""
    name = selection.get("name") or selection.get("company") or "품목"
    spec = selection.get("spec") or ""
    title = f"■ {name}({spec})" if spec else f"■ {name}"

    note = ""
    body: Optional[Image.Image] = None

    demo = runtime.is_demo()
    if not demo:
        url = selection.get("verifyUrl")
        # ⚠ shop.g2b.go.kr(종합쇼핑몰)는 서버 자동 접근을 SSO·봇으로 차단 → 절대 접속 안 함(지시서 §3-2).
        #    캡처는 차단되지 않는 URL만 시도하고, 그 외엔 자체 렌더링 카드로 폴백.
        if url and "g2b.go.kr" not in url:
            body = await _playwright_capture(url)
            if body is None:
                note = "원본 캡처에 실패해 대체 형식으로 저장했어요"

    if body is None:
        body = _fallback_card(selection)
        if not note:
            note = "데모/폴백 형식(자체 렌더링)으로 저장했어요" if demo else note

    return _compose(title, body), note
