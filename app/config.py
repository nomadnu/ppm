"""환경설정 로딩. .env 에서 인증키·모드를 읽는다."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 프로젝트 루트 (app/ 의 부모)
ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

# .env 값은 "초기 시드"로만 쓰인다. 실제 런타임 인증키·데모 여부는
# DB(settings 테이블) 우선 — 관리자 메뉴에서 변경 가능. app/runtime.py 참조.
SERVICE_KEY: str = os.getenv("SERVICE_KEY", "").strip()
DEMO_MODE_ENV: bool = os.getenv("DEMO_MODE", "1").strip() == "1"

# 관리자 초기 비밀번호 (설계 요청값). DB에 해시로 시드되며 관리자 메뉴에서 변경 가능.
ADMIN_PASSWORD_DEFAULT: str = os.getenv("ADMIN_PASSWORD", "5968").strip()

DB_PATH: Path = ROOT / os.getenv("DB_PATH", "ppm.db")

# 조달청 오픈 API 게이트웨이 (data.go.kr)
# 서비스별 base 경로는 procurement.py 의 후보 목록에서 관리한다.
API_GATEWAY = "http://apis.data.go.kr"

# 나라장터 종합쇼핑몰 (구 도메인 — 캡처 참고용, 현재 로그인 필요)
G2B_SHOPPING_BASE = "https://shop.g2b.go.kr"
# 나라장터 물품목록정보시스템 품목검색 (공개, 로그인 불필요) — 검증 링크의 본체
G2B_GOODS_SEARCH = "https://goods.g2b.go.kr/search/productSearch.do"

# HTTP 타임아웃(초)
HTTP_TIMEOUT = 12.0
