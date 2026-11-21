# 두 시장 브리핑 · 나스닥·코스피 데일리

매일 두 번(한국시간 07:10 뉴욕 마감 / 15:50 서울 마감) 시세·뉴스를 모아 Claude가 브리핑을 쓰고, GitHub Pages로 자동 게시하는 사이트입니다. 서버 비용 없이 GitHub만으로 돌아갑니다.

## 구조
```
scripts/build_briefing.py      데이터 수집 + Claude 브리핑 → docs/data/*.json
docs/index.html                JSON을 읽어 그리는 정적 페이지
docs/data/latest.json          최신 브리핑 (처음엔 예시 데이터)
docs/data/archive/             지난 브리핑 보관 (최대 180개)
.github/workflows/daily-briefing.yml   매일 자동 실행
```

## 배포 (10분)
1. GitHub에 새 저장소를 만들고 이 폴더 전체를 올립니다.
2. Settings → Secrets and variables → Actions → New repository secret
   - `ANTHROPIC_API_KEY` : console.anthropic.com 에서 발급 (없으면 코멘트 없이 숫자만 요약)
   - `ECOS_API_KEY` (선택) : ecos.bok.or.kr 에서 발급, 국고채 3년 금리 추가
3. Settings → Pages → Source: `Deploy from a branch`, Branch: `main` / 폴더 `/docs`
4. Actions 탭 → Daily market briefing → Run workflow 로 첫 실행
5. `https://<아이디>.github.io/<저장소명>/` 접속

## 로컬 테스트
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
python scripts/build_briefing.py
cd docs && python -m http.server 8000   # http://localhost:8000
```

## 수정 포인트
- 종목·섹터·지표: `build_briefing.py` 상단의 `US_STOCKS`, `KR_STOCKS`, `US_SECTORS`, `KR_SECTORS`, `MACRO` 목록
- 브리핑 문체·분량: `SYSTEM` 프롬프트
- 모델: 저장소 Variables에 `CLAUDE_MODEL` 지정 (기본 `claude-sonnet-5-5`)
- 실행 시각: 워크플로의 `cron` (UTC 기준, 한국시간 −9시간)

## 알아둘 점
- yfinance는 Yahoo Finance 비공식 라이브러리라 가끔 막히거나 바뀝니다. 안정성이 중요해지면 한국투자증권 Open API, KRX 정보데이터시스템, Polygon 등으로 교체하세요.
- 한국 섹터는 KODEX 섹터 ETF로 대신 보여줍니다. 티커가 바뀌거나 상장폐지될 수 있으니 처음 실행 로그의 `[skip]` 메시지를 확인하세요.
- 미국 서머타임 기간에는 뉴욕 마감이 한국시간 05:00이라 07:10 실행에 여유가 있고, 겨울엔 06:00 마감입니다.
- GitHub cron은 수 분~수십 분 늦게 실행될 수 있습니다. 한국 공휴일·미국 휴장일에도 실행되지만 직전 거래일 데이터로 브리핑합니다.
- 자동 생성 콘텐츠이므로 투자 권유가 아니라는 고지를 유지하세요.
