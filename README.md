# 관급자재 단가조회

조달청 오픈 API로 자재 규격을 검색해 **중간가(median)** 를 산출하고,
전북·인증 우선 **후보 3선**을 제시해 담당자가 1개를 선정하는 단가조사 도우미.
설계서 `관급자재_단가조회_설계서_v0.3.4.md` 기준 구현.

## 빠른 시작 (로컬, 데모 모드)

```bash
pip install -r requirements.txt          # Playwright 제외 최소 실행은 아래 4종만으로도 가능
#   최소: pip install fastapi "uvicorn[standard]" httpx python-dotenv Pillow
cp .env.example .env                      # DEMO_MODE=1 로 인증키 없이 동작
python -m uvicorn app.main:app --port 8000
```

브라우저에서 <http://127.0.0.1:8000> → `레미콘 25-24-150` 조회.
데모 모드는 합성 데이터로 UI/로직 전체를 보여줍니다(설계 Day 2 검증 목표).

## 실제 조달청 API 연결 (관리자 메뉴)

인증키는 **웹 관리자 메뉴에서 입력·저장**합니다(코드·`.env` 하드코딩 불필요).

1. [공공데이터포털](https://www.data.go.kr) 활용신청 → **Decoding 인증키** 발급 (계정당 1개, 5종 API 공통)
2. 웹 우측 상단 **🔒 관리자** → 비밀번호 입력(**초기값 `5968`**)
3. **조달청 인증키** 칸에 Decoding 키 붙여넣기 → **데모 모드 체크 해제** → 저장
4. **연결 진단(Probe)** 클릭 → 5종 서비스 상태 확인
5. 비밀번호는 관리자 메뉴 하단 **비밀번호 변경**에서 바꿀 수 있습니다.

### 확정된 실제 엔드포인트 (2026-07 실접속 검증 완료)

| # | 서비스 | 엔드포인트 (base `http://apis.data.go.kr`) | 오퍼레이션 |
|---|---|---|---|
| ① | 종합쇼핑몰 품목정보 (검색 본체) | `/1230000/at/ShoppingMallPrdctInfoService` | `getMASCntrctPrdctInfoList` |
| ② | 가격정보현황 | `/1230000/ao/PriceInfoService` | `getPriceInfoListFcltyCmmnMtrilTotal` |
| ③ | 물품목록정보 | `/1230000/ao/ThngListInfoService02` | `getPrdctClsfcNoUnit2Info02` |
| ④ | 나라장터 계약정보 | `/1230000/ao/CntrctInfoService` | `getCntrctInfoListThngPPSSrch` |
| ⑤ | 나라장터 낙찰정보 | `/1230000/as/ScsbidInfoService` | `getOpengResultListInfoThngPPSSrch` |

- 공통 필수 파라미터: `inqryDiv=1`, `inqryBgnDate`/`inqryEndDate`(조회기간, 자동 채움), 검색어 `prdctClsfcNoNm`.
- 실검색엔 ①② 사용(설계 MVP). 검색어는 **품명부로 API 조회 → 규격부로 로컬 2차 필터**(F1).
- 레미콘·아스콘은 ①(종합쇼핑몰) 소관 — ②가격정보현황엔 없어 정상적으로 0건.

> 인증키·데모여부·관리자 비밀번호(해시)는 로컬 SQLite `settings` 테이블에 저장되어
> **재시작 없이** 즉시 반영됩니다. 관리자 토큰은 메모리 보관이라 서버 재시작 시 재로그인합니다.
> 실제 오퍼레이션명·나라장터 URL 패턴은 설계서 미결사항 7번대로 이 Probe에서 실키로 확정합니다.

> ⚠ 인증키는 로컬 DB에 평문 저장됩니다(단일 사용자 로컬 도구 전제). 팀 공유(Render) 단계에서는
> 환경변수/시크릿 매니저로 옮기고, 관리자 비밀번호를 기본값 `5968`에서 반드시 변경하세요.

## 이미지 저장(F9) — 실제 캡처 켜기

데모/미설치 시에는 자체 렌더링 카드로 폴백합니다. 나라장터 상세 페이지 실캡처는:

```bash
pip install playwright
python -m playwright install chromium     # 최초 1회
```

## 기능 ↔ 코드 매핑

| 기능 | 위치 |
|---|---|
| F1 규격 검색 / F2 중간가 / F3 4단계 정렬 | `app/search.py`, `app/main.py:api_search` |
| F4 검증 링크(URL→식별번호→키워드 폴백) | `app/search.py:build_verify_url` |
| F5 CSV(후보3선+선정+메타) | `app/main.py:api_export` |
| F6 조회 이력·직전 대비·고정 | `app/db.py`, `/api/history*` |
| F7 지정 공급처 메모(배너·CRUD) | `app/db.py`, `/api/notes*` |
| F8 후보 3선 → 선정 | `app/search.py:pick_candidates`, `/api/history/{id}/select` |
| F9 조서 PNG(Playwright+Pillow, 2단 폴백) | `app/capture.py`, `/api/image` |
| Probe 자가진단(6.4) / 퍼지 매핑(6.5) | `app/procurement.py` |

## 초기 공급처 메모 (설계 확정)

첫 실행 시 자동 시드: 레미콘=전북남부레미콘사업협동조합·전북레미콘공업협동조합(2건),
아스콘=전북아스콘공업협동조합(1건). ⚙설정에서 연락처·권역 메모를 채워 넣으세요.

## 배포(2단계) 주의

Render 무료 플랜은 재배포 시 디스크가 휘발돼 **SQLite 이력·메모가 유실**될 수 있습니다
(설계 8절). 팀 공유 진입 시 Persistent Disk / Firestore 전환 / 주기 백업 중 택1.
1단계(로컬)에서는 문제없습니다.

---
*조달청 계약단가는 참고자료이며 최종 판단은 담당자에게 있습니다.*
