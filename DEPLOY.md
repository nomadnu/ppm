# 배포 가이드 — 동료 공유용 지속 URL (Render)

로컬 커밋까지 끝나 있습니다(`main` 브랜치). 아래 5분 절차만 하시면 공개 URL이 생깁니다.

## 1) GitHub에 올리기
1. <https://github.com/new> 에서 새 저장소 생성 (이름 예: `ppm`, **Private 권장**). README/gitignore 추가 옵션은 **끄기**.
2. 생성 후 나오는 주소로 아래 실행 (터미널에서 `C:\ppm` 위치):
   ```bash
   git remote add origin https://github.com/<내아이디>/ppm.git
   git push -u origin main
   ```
   - 인증 창이 뜨면 GitHub 계정으로 로그인(또는 Personal Access Token).
   - ⚠ `.env`와 `ppm.db`(인증키 포함)는 `.gitignore`로 **올라가지 않습니다** — 안전합니다.

## 2) Render에 배포
1. <https://render.com> 가입/로그인 (GitHub 계정으로 로그인 가능).
2. **New +** → **Blueprint** → 방금 만든 GitHub 저장소 선택.
   - 저장소의 `render.yaml`을 자동 인식합니다.
3. 배포 전에 **환경변수(Environment)** 를 채웁니다:
   | 변수 | 값 |
   |---|---|
   | `SERVICE_KEY` | 조달청 **Decoding 인증키** (로컬에서 쓰던 그 키) |
   | `DEMO_MODE` | `0` (실데이터) |
   | `ADMIN_PASSWORD` | 원하는 관리자 비밀번호 (기본 `5968`에서 변경 권장) |
   | `FIREBASE_CREDENTIALS` | Firebase 서비스계정 키 **JSON 전체를 한 줄로** 붙여넣기 (v0.4 Firestore) |

   > ⚠ Firestore 전환(v0.4) 후에는 `FIREBASE_CREDENTIALS` 가 **없으면 앱이 기동되지 않습니다**.
   > 반드시 키를 먼저 넣은 뒤 배포하세요. Firebase 콘솔 → 프로젝트 설정 → 서비스 계정 →
   > "새 비공개 키 생성"으로 받은 JSON 내용을 그대로 값에 붙여넣으면 됩니다.
4. **Apply / Deploy** → 몇 분 뒤 `https://ppm-danga.onrender.com` 같은 URL 생성 → 동료에게 공유.

## 꼭 아시고 계실 점 (무료 플랜)
- **15분 미사용 시 잠자기** → 다음 첫 접속이 30~60초 걸립니다(깨어나는 시간). 쓰다 보면 빨라집니다.
- **디스크 휘발성** → 재배포/재시작 시 `ppm.db` 초기화:
  - 조회 **이력**은 사라집니다(설계 8절, 추후 Persistent Disk/Firestore로 보완).
  - **인증키**는 위 `SERVICE_KEY` 환경변수로 고정되니 문제 없음.
  - **지정 공급처 메모**(전북서부레미콘조합 등)는 시작 시 자동 재생성됨.
- 로컬 `ppm.db`에 넣었던 키는 Render로 **넘어가지 않습니다** — 반드시 `SERVICE_KEY` 환경변수로 넣으세요.

## 코드 수정 후 재배포
```bash
git add -A && git commit -m "수정 내용"
git push
```
→ Render가 자동으로 다시 배포합니다.

## 나중에: 실사용 캡처(F9 원본 이미지)까지 쓰려면
Render에 Playwright/Chromium 설치가 필요합니다(빌드 무거워짐). 지금은 F9가 자체 렌더링 카드로 폴백하니 공유엔 지장 없습니다. 필요 시 `requirements.txt`의 `playwright` 주석 해제 + 빌드 커맨드에 `python -m playwright install --with-deps chromium` 추가.
