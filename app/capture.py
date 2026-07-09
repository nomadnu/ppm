"""선정 결과 이미지 저장 (F9) — 실무 단가 조서 붙임 형식.

종합쇼핑몰 상세페이지 전체 캡처는 서버 접근이 SSO·봇으로 차단되어 불가(설계 v0.5 접근정책).
대신 조달청 서버의 '실제 상품 이미지'(로그인 없이 열리는 공개 정적 자산)를 가져와,
"■ 물품명(규격)" 제목 + 상품사진 + 단가·업체·규격·인증 정보를 합쳐 조서 붙임용 PNG를 만든다.
상품 이미지를 못 가져오면 정보 카드만으로 폴백한다.
"""
from __future__ import annotations

import io
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

OUT_W = 760
IMG_BOX = 280


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """한글 폰트 로드 — Windows 맑은고딕 우선, 실패 시 기본 폰트."""
    for path in (r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf",
                 "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


async def _fetch_image(url: Optional[str]) -> Optional[Image.Image]:
    """조달청 공개 상품 이미지(shop.g2b.go.kr/static/...)를 가져온다. 실패 시 None.
    ※ 이는 SSO로 막힌 상세 페이지가 아니라 공개 정적 이미지 자산이다."""
    if not url or not str(url).startswith("http"):
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None
    return None


def _g(selection: dict, key: str) -> str:
    v = selection.get(key)
    return str(v) if v not in (None, "") else "-"


async def build_png(selection: dict) -> tuple[bytes, str]:
    """선정 항목 → (PNG bytes, 안내문구). 제목은 '■ 물품명(규격)'."""
    name = selection.get("name") or selection.get("company") or "품목"
    spec = selection.get("spec") or ""
    title = f"■ {name}({spec})" if spec else f"■ {name}"

    product = await _fetch_image(selection.get("imageUrl"))
    note = "" if product else "상품 이미지를 불러오지 못해 정보만 저장했어요"

    H = 430
    canvas = Image.new("RGB", (OUT_W, H), "white")
    draw = ImageDraw.Draw(canvas)
    f_title = _load_font(26)
    f_company = _load_font(23)
    f_price = _load_font(32)
    f_line = _load_font(18)
    f_badge = _load_font(20)

    # 제목 밴드
    draw.text((20, 18), title, fill="black", font=f_title)
    draw.line([(0, 60), (OUT_W, 60)], fill="#cccccc", width=1)

    content_y = 82
    info_x = 24
    # 상품 이미지 (있으면 좌측)
    if product:
        thumb = ImageOps.contain(product, (IMG_BOX, IMG_BOX))
        canvas.paste(thumb, (24, content_y))
        draw.rectangle([23, content_y - 1, 24 + thumb.width, content_y + thumb.height],
                       outline="#dddddd", width=1)
        info_x = 24 + IMG_BOX + 28

    # 정보 블록
    y = content_y
    draw.text((info_x, y), _g(selection, "company"), fill="black", font=f_company)
    price = selection.get("price")
    price_s = f"{price:,}원" if isinstance(price, (int, float)) else "-"
    draw.text((info_x, y + 40), price_s, fill="#0a7d33", font=f_price)

    y += 92
    for ln in (f"규격: {_g(selection, 'spec')}",
               f"소재지: {_g(selection, 'region')}",
               f"출처: {_g(selection, 'source')}",
               f"계약일: {_g(selection, 'contractDate')}"):
        draw.text((info_x, y), ln, fill="#333333", font=f_line)
        y += 30

    badges = []
    if selection.get("isJeonbuk"):
        badges.append("전북")
    if selection.get("isCertified"):
        badges.append("인증")
    if badges:
        draw.text((info_x, y + 6), " · ".join(f"[{b}]" for b in badges),
                  fill="#c2410c", font=f_badge)

    draw.text((24, H - 30),
              "※ 조달청 종합쇼핑몰 계약단가 기준 참고자료 (원본: 나라장터 종합쇼핑몰)",
              fill="#999999", font=_load_font(13))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), note
