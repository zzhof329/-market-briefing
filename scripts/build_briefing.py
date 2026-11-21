"""
나스닥·코스피 데일리 브리핑 생성 스크립트

1) yfinance로 지수·종목·섹터·매크로 시세 수집
2) Google News RSS로 시장 뉴스 수집
3) (선택) 한국은행 ECOS API로 국고채 3년 금리 수집
4) Claude API로 헤드라인·시장 코멘트·뉴스 한줄평 작성
5) docs/data/latest.json + archive/ 에 저장 → GitHub Pages가 그대로 서비스

환경변수
  ANTHROPIC_API_KEY  (없으면 규칙 기반 요약으로 대체)
  CLAUDE_MODEL       (기본값: claude-sonnet-5-5)
  ECOS_API_KEY       (선택)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "docs" / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
KST = ZoneInfo("Asia/Seoul")

# ── 수집 대상 (원하는 대로 수정하세요) ─────────────────────────────
INDICES = [
    ("^IXIC", "나스닥 종합"),
    ("^NDX", "나스닥 100"),
    ("^GSPC", "S&P 500"),
    ("^DJI", "다우존스"),
    ("^SOX", "필라델피아 반도체"),
    ("^VIX", "VIX 변동성"),
    ("^KS11", "코스피"),
    ("^KQ11", "코스닥"),
]

US_STOCKS = [
    ("NVDA", "엔비디아"), ("AAPL", "애플"), ("MSFT", "마이크로소프트"),
    ("AMZN", "아마존"), ("GOOGL", "알파벳"), ("META", "메타"),
    ("TSLA", "테슬라"), ("AVGO", "브로드컴"),
]

KR_STOCKS = [
    ("005930.KS", "삼성전자"), ("000660.KS", "SK하이닉스"),
    ("373220.KS", "LG에너지솔루션"), ("207940.KS", "삼성바이오로직스"),
    ("005380.KS", "현대차"), ("035420.KS", "NAVER"),
    ("105560.KS", "KB금융"), ("012450.KS", "한화에어로스페이스"),
]

# 미국: SPDR 섹터 ETF / 한국: KODEX 섹터 ETF (섹터 흐름의 대용치)
US_SECTORS = [
    ("XLK", "기술"), ("XLC", "커뮤니케이션"), ("XLY", "임의소비재"),
    ("XLF", "금융"), ("XLV", "헬스케어"), ("XLI", "산업재"),
    ("XLE", "에너지"), ("XLP", "필수소비재"), ("XLU", "유틸리티"),
]
KR_SECTORS = [
    ("091160.KS", "반도체"), ("305720.KS", "2차전지"), ("091180.KS", "자동차"),
    ("091170.KS", "은행"), ("266420.KS", "헬스케어"), ("117700.KS", "건설"),
    ("117460.KS", "에너지화학"),
]

# unit: 화면 표시 방식 (krw, pct=금리, usd, idx)
MACRO = [
    ("KRW=X", "원/달러 환율", "krw"),
    ("DX-Y.NYB", "달러 인덱스", "idx"),
    ("^TNX", "미국채 10년", "pct"),
    ("^IRX", "미국채 3개월", "pct"),
    ("CL=F", "WTI 유가", "usd"),
    ("GC=F", "금", "usd"),
    ("BTC-USD", "비트코인", "usd"),
]

NEWS_QUERIES = {
    "us": "나스닥 OR 뉴욕증시 when:1d",
    "kr": "코스피 OR 코스닥 when:1d",
}


# ── 시세 ─────────────────────────────────────────────────────────
def _closes_one(symbol: str):
    """한 종목만 따로 받아오기 (일괄 다운로드에서 빠졌을 때 재시도용)"""
    for attempt in range(3):
        try:
            h = yf.Ticker(symbol).history(period="1mo", interval="1d", auto_adjust=False)
            closes = h["Close"].dropna()
            if len(closes) >= 2:
                return closes
        except Exception as e:  # noqa: BLE001
            print(f"[retry {attempt + 1}] {symbol}: {e}")
        time.sleep(2 + attempt * 3)
    return None


def fetch_quotes(table: list[tuple]) -> list[dict]:
    tickers = [row[0] for row in table]
    try:
        df = yf.download(
            tickers, period="1mo", interval="1d", group_by="ticker",
            auto_adjust=False, progress=False, threads=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[download] 일괄 다운로드 실패: {e}")
        df = None
    out = []
    for row in table:
        symbol, name = row[0], row[1]
        closes = None
        try:
            closes = df[symbol]["Close"].dropna()
        except Exception:  # noqa: BLE001
            pass
        if closes is None or len(closes) < 2:
            print(f"[retry] {symbol}: 일괄 다운로드에서 빠져 따로 받아옵니다")
            closes = _closes_one(symbol)
        if closes is None:
            print(f"[skip] {symbol}: 데이터 없음")
            continue
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        item = {
            "symbol": symbol,
            "name": name,
            "price": round(last, 4),
            "change": round(last - prev, 4),
            "pct": round((last / prev - 1) * 100, 2),
            "date": closes.index[-1].strftime("%Y-%m-%d"),
            "spark": [round(float(v), 4) for v in closes.tail(20)],
        }
        if len(row) > 2:
            item["unit"] = row[2]
        out.append(item)
    return out


def fetch_ecos_kr3y() -> dict | None:
    """한국은행 ECOS: 국고채 3년 (통계표 817Y002 / 항목 010200000)"""
    key = os.getenv("ECOS_API_KEY")
    if not key:
        return None
    end = dt.datetime.now(KST).strftime("%Y%m%d")
    start = (dt.datetime.now(KST) - dt.timedelta(days=20)).strftime("%Y%m%d")
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/30/"
           f"817Y002/D/{start}/{end}/010200000")
    try:
        rows = requests.get(url, timeout=15).json()["StatisticSearch"]["row"]
        vals = [float(r["DATA_VALUE"]) for r in rows]
        if len(vals) < 2:
            return None
        return {
            "symbol": "KR3Y", "name": "국고채 3년", "unit": "pct",
            "price": vals[-1], "change": round(vals[-1] - vals[-2], 3),
            "pct": round((vals[-1] / vals[-2] - 1) * 100, 2),
            "date": dt.datetime.strptime(rows[-1]["TIME"], "%Y%m%d").strftime("%Y-%m-%d"),
            "spark": vals[-20:],
        }
    except Exception as e:  # noqa: BLE001
        print(f"[ecos] 실패: {e}")
        return None


# ── 뉴스 ─────────────────────────────────────────────────────────
def fetch_news(limit: int = 7) -> list[dict]:
    items, seen = [], set()
    for market, q in NEWS_QUERIES.items():
        url = f"https://news.google.com/rss/search?q={quote(q)}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)
        for e in feed.entries[:limit]:
            title = re.sub(r"\s+-\s+[^-]+$", "", e.title).strip()  # 끝의 " - 언론사" 제거
            if title in seen:
                continue
            seen.add(title)
            source = getattr(getattr(e, "source", None), "title", "") or ""
            items.append({"market": market, "title": title, "link": e.link,
                          "source": source, "published": e.get("published", "")})
    return items


# ── Claude 브리핑 ────────────────────────────────────────────────
SYSTEM = """당신은 한국 개인투자자를 위한 증권사 리서치센터의 데일리 마켓 브리핑 작성자입니다.
주어진 시세·뉴스 데이터만 근거로 쓰고, 데이터에 없는 수치나 사실은 만들지 마세요.
매수·매도 권유는 하지 않습니다. 문체는 간결한 '~했다/~이다' 체.
반드시 아래 형식의 JSON 객체 하나만 출력하세요. 설명 문장, 마크다운 코드블록, 주석(//)은 넣지 마세요.
{
  "headline": "오늘 시장을 한 문장으로 (40자 내외)",
  "us": "미국 시장 코멘트 3~4문장 (300자 이내)",
  "kr": "한국 시장 코멘트 3~4문장 (300자 이내)",
  "macro": "환율·금리·원자재 코멘트 2~3문장 (200자 이내)",
  "watch": ["오늘/다음 거래일 체크포인트 3개. upcoming_events가 있으면 날짜와 함께 반영"],
  "tags": ["아카이브 분류용 주제 태그 3~5개. 짧은 명사, 예: 반도체, 환율, 금리, 실적, 2차전지, 유가"],
  "news_notes": [{"i": 0, "note": "중요한 뉴스 최대 6개에 대한 시장 관점 한줄평. i는 뉴스 번호(숫자)"}]
}"""


def write_with_claude(payload: dict) -> dict | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    from anthropic import Anthropic

    client = Anthropic()
    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5",
        max_tokens=8000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    if msg.stop_reason == "max_tokens":
        print("[claude] 답이 길어서 중간에 잘렸습니다. max_tokens를 늘리거나 분량을 줄이세요.")
    return parse_json_loose(text)


def parse_json_loose(text: str) -> dict | None:
    cleaned = re.sub(r"```(?:json)?", "", text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        print(f"[claude] JSON을 찾지 못함. 응답 앞부분: {text[:300]!r}")
        return None
    body = cleaned[start:end + 1]
    for candidate in (body, re.sub(r"//[^\n\"]*$", "", body, flags=re.M), re.sub(r",\s*([}\]])", r"\1", body)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    print(f"[claude] JSON 파싱 실패, 기본 요약 사용. 응답 앞부분: {text[:300]!r}")
    return None


def fallback_briefing(indices: list[dict]) -> dict:
    by = {i["symbol"]: i for i in indices}

    def join(*syms):
        return ", ".join(f"{by[s]['name']} {by[s]['pct']:+.2f}%" for s in syms if s in by)

    us, kr = join("^IXIC", "^GSPC", "^SOX"), join("^KS11", "^KQ11")
    return {
        "headline": join("^IXIC", "^KS11") or join("^GSPC", "^KS11") or "오늘의 시장 브리핑",
        "us": f"{us}로 마감했다." if us else "",
        "kr": f"{kr}로 마감했다." if kr else "",
        "macro": "",
        "watch": [],
        "tags": [],
        "news_notes": [],
    }



# ── 역대 월별 등락 (시즌성) ──────────────────────────────────────
SEASON_INDICES = [("^IXIC", "나스닥 종합"), ("^NDX", "나스닥 100"), ("^KS11", "코스피"), ("^KQ11", "코스닥")]


def build_seasonality() -> None:
    """지수별 월간 수익률을 연도×월 표로 저장 → docs/data/seasonality.json"""
    out = []
    now = dt.datetime.now(KST)
    for symbol, name in SEASON_INDICES:
        try:
            h = yf.Ticker(symbol).history(period="max", interval="1mo", auto_adjust=False)
            closes = h["Close"].dropna()
            closes = closes[~closes.index.duplicated(keep="last")]
            rets = closes.pct_change().dropna() * 100
        except Exception as e:  # noqa: BLE001
            print(f"[season] {symbol} 실패: {e}")
            continue
        years: dict[str, list] = {}
        for ts, r in rets.items():
            y, m = str(ts.year), ts.month
            years.setdefault(y, [None] * 12)[m - 1] = round(float(r), 2)
        # 진행 중인 이번 달은 통계에서 제외
        cur_y, cur_m = str(now.year), now.month
        stats = []
        for m in range(12):
            vals = [row[m] for y, row in years.items() if row[m] is not None and not (y == cur_y and m + 1 == cur_m)]
            if not vals:
                stats.append(None)
                continue
            vals_sorted = sorted(vals)
            mid = len(vals_sorted) // 2
            median = vals_sorted[mid] if len(vals_sorted) % 2 else (vals_sorted[mid - 1] + vals_sorted[mid]) / 2
            stats.append({
                "avg": round(sum(vals) / len(vals), 2),
                "median": round(median, 2),
                "up": round(sum(v > 0 for v in vals) / len(vals) * 100),
                "n": len(vals),
                "best": round(max(vals), 2),
                "worst": round(min(vals), 2),
            })
        out.append({"symbol": symbol, "name": name, "current": f"{cur_y}-{cur_m:02d}",
                    "years": dict(sorted(years.items())), "stats": stats})
        print(f"[season] {name}: {min(years)}~{max(years)}년")
    if out:
        doc = {"updated": now.isoformat(timespec="minutes"), "indices": out}
        (DATA_DIR / "seasonality.json").write_text(json.dumps(doc, ensure_ascii=False), "utf-8")


# ── 경제 일정 달력 ──────────────────────────────────────────────
CAL_SYSTEM = """당신은 한국 개인투자자를 위한 경제 일정 편집자입니다.
웹 검색으로 확인된 공식 일정만 넣고, 확인되지 않은 날짜는 넣지 마세요. 추측 금지.
시간은 한국시간(KST) 24시간제 "HH:MM"으로, 모르면 빈 문자열.
반드시 아래 형식의 JSON 객체 하나만 출력하세요. 설명 문장, 코드블록, 주석 금지.
{"events": [{"date": "YYYY-MM-DD", "time": "21:30", "country": "us 또는 kr", "title": "짧은 일정명", "importance": 1~3 숫자(3=시장 영향 큼), "note": "한 줄 설명(선택)"}]}"""

CAL_PROMPT = """{month} 한 달 동안의 주요 경제 일정을 찾아 정리해 주세요.
포함할 것:
- 미국: FOMC 회의·의사록, 소비자물가(CPI), 생산자물가(PPI), 고용보고서, 개인소비지출(PCE), GDP, 소매판매, ISM·S&P글로벌 PMI, 미시간대·콘퍼런스보드 소비자심리, 주택지표, 주요 빅테크 실적 발표, 옵션 만기일
- 한국: 한국은행 금융통화위원회, 수출입 동향, 소비자물가, 선물옵션 동시만기일, 주요 대형주 실적 발표
importance 3은 FOMC 금리 결정, CPI, 고용보고서, PCE, 금통위처럼 시장을 크게 움직이는 일정에만 주세요.
미국 발표는 한국시간으로 바꿔서 적어 주세요(서머타임 반영)."""


def build_calendar() -> list[dict]:
    """이번 달·다음 달 일정을 일주일에 한 번 Claude 웹 검색으로 갱신 → docs/data/calendar.json"""
    path = DATA_DIR / "calendar.json"
    cal = json.loads(path.read_text("utf-8")) if path.exists() else {"updated": {}, "events": []}
    now = dt.datetime.now(KST)
    first_next = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    months = [now.strftime("%Y-%m"), first_next.strftime("%Y-%m")]

    if os.getenv("ANTHROPIC_API_KEY"):
        from anthropic import Anthropic
        client = Anthropic()
        for month in months:
            last = cal["updated"].get(month)
            if last and (now - dt.datetime.fromisoformat(last)).days < 7:
                continue
            try:
                msg = client.messages.create(
                    model=os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5",
                    max_tokens=8000,
                    system=CAL_SYSTEM,
                    tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
                    messages=[{"role": "user", "content": CAL_PROMPT.format(month=month)}],
                )
                text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                data = parse_json_loose(text[text.find('{"events"'):] if '{"events"' in text else text)
                events = [e for e in (data or {}).get("events", [])
                          if str(e.get("date", "")).startswith(month) and e.get("title")]
                if not events:
                    print(f"[calendar] {month}: 일정을 받지 못해 기존 데이터 유지")
                    continue
                cal["events"] = [e for e in cal["events"] if not e["date"].startswith(month)] + [
                    {"date": e["date"], "time": str(e.get("time", ""))[:5], "country": e.get("country", "us"),
                     "title": str(e["title"])[:40], "importance": int(e.get("importance", 1) or 1),
                     "note": str(e.get("note", ""))[:80]} for e in events]
                cal["updated"][month] = now.isoformat(timespec="minutes")
                print(f"[calendar] {month}: {len(events)}건 갱신")
            except Exception as e:  # noqa: BLE001
                print(f"[calendar] {month} 실패: {e}")

    # 석 달 지난 일정은 정리
    cutoff = (now.replace(day=1) - dt.timedelta(days=95)).strftime("%Y-%m")
    cal["events"] = sorted([e for e in cal["events"] if e["date"][:7] >= cutoff],
                           key=lambda e: (e["date"], e.get("time") or "99:99"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cal, ensure_ascii=False, indent=1), "utf-8")
    return cal["events"]


# ── 저장 ─────────────────────────────────────────────────────────
def save(doc: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(doc, ensure_ascii=False, indent=1)
    (DATA_DIR / "latest.json").write_text(blob, encoding="utf-8")

    key = f"{doc['date']}-{doc['session']}"
    (ARCHIVE_DIR / f"{key}.json").write_text(blob, encoding="utf-8")

    index_path = ARCHIVE_DIR / "index.json"
    index = json.loads(index_path.read_text("utf-8")) if index_path.exists() else []
    def pct_of(sym):
        return next((i["pct"] for i in doc["indices"] if i["symbol"] == sym), None)

    # 아카이브 검색용: 그날 크게 움직인 종목 이름도 함께 저장
    movers = sorted(doc["stocks"]["us"] + doc["stocks"]["kr"], key=lambda s: -abs(s["pct"]))
    entry = {
        "key": key, "date": doc["date"], "session": doc["session"],
        "headline": doc["headline"], "tags": doc.get("tags", []),
        "us_pct": pct_of("^IXIC"), "kr_pct": pct_of("^KS11"),
        "summary": " ".join(doc["commentary"].values())[:300],
        "keywords": [s["name"] for s in movers[:4]],
    }
    index = [e for e in index if e["key"] != key]
    index.insert(0, entry)
    index.sort(key=lambda e: (e["date"], e["session"] == "afternoon"), reverse=True)
    index_path.write_text(json.dumps(index[:500], ensure_ascii=False, indent=1), "utf-8")


def main() -> None:
    now = dt.datetime.now(KST)
    session = "morning" if now.hour < 12 else "afternoon"  # 오전=뉴욕 마감, 오후=서울 마감

    indices = fetch_quotes(INDICES)
    stocks = {"us": fetch_quotes(US_STOCKS), "kr": fetch_quotes(KR_STOCKS)}
    sectors = {"us": fetch_quotes(US_SECTORS), "kr": fetch_quotes(KR_SECTORS)}
    macro = fetch_quotes(MACRO)
    if kr3y := fetch_ecos_kr3y():
        macro.insert(3, kr3y)
    news = fetch_news()

    for job in (build_seasonality, build_calendar):
        try:
            job()
        except Exception as e:  # noqa: BLE001
            print(f"[{job.__name__}] 실패: {e}")
    upcoming = []
    try:
        cal = json.loads((DATA_DIR / "calendar.json").read_text("utf-8"))
        today, week = now.strftime("%Y-%m-%d"), (now + dt.timedelta(days=7)).strftime("%Y-%m-%d")
        upcoming = [e for e in cal["events"] if today <= e["date"] <= week]
    except Exception:  # noqa: BLE001
        pass

    # 토큰 절약: Claude에는 스파크라인을 빼고 전달
    def lite(rows):
        return [{k: v for k, v in r.items() if k != "spark"} for r in rows]

    payload = {
        "now_kst": now.strftime("%Y-%m-%d %H:%M"),
        "session": "뉴욕 마감 직후" if session == "morning" else "서울 마감 직후",
        "indices": lite(indices),
        "stocks": {k: lite(v) for k, v in stocks.items()},
        "sectors": {k: lite(v) for k, v in sectors.items()},
        "macro": lite(macro),
        "news": [{"i": n, "market": x["market"], "title": x["title"]} for n, x in enumerate(news)],
        "upcoming_events": upcoming,  # watch 항목 작성에 참고
    }
    brief = write_with_claude(payload) or fallback_briefing(indices)

    notes = {n["i"]: n["note"] for n in brief.get("news_notes", []) if "i" in n}
    for n, item in enumerate(news):
        if n in notes:
            item["note"] = notes[n]

    doc = {
        "generated_at": now.isoformat(timespec="minutes"),
        "date": now.strftime("%Y-%m-%d"),
        "session": session,
        "sample": False,
        "headline": brief.get("headline", ""),
        "commentary": {k: brief.get(k, "") for k in ("us", "kr", "macro")},
        "watch": brief.get("watch", []),
        "tags": [t.lstrip("#").strip() for t in brief.get("tags", []) if t.strip()][:5],
        "indices": indices, "stocks": stocks, "sectors": sectors, "macro": macro,
        "news": news,
    }
    save(doc)
    print(f"저장 완료: {doc['date']} {session} / {doc['headline']}")


if __name__ == "__main__":
    main()
