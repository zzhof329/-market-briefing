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
    ("^GSPC", "S&P 500"),
    ("^DJI", "다우존스"),
    ("^SOX", "필라델피아 반도체"),
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
    ("^VIX", "VIX 변동성", "idx"),
    ("CL=F", "WTI 유가", "usd"),
    ("GC=F", "금", "usd"),
    ("BTC-USD", "비트코인", "usd"),
]

NEWS_QUERIES = {
    "us": "나스닥 OR 뉴욕증시 when:1d",
    "kr": "코스피 OR 코스닥 when:1d",
}


# ── 시세 ─────────────────────────────────────────────────────────
def fetch_quotes(table: list[tuple]) -> list[dict]:
    tickers = [row[0] for row in table]
    df = yf.download(
        tickers, period="1mo", interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )
    out = []
    for row in table:
        symbol, name = row[0], row[1]
        try:
            closes = df[symbol]["Close"].dropna()
        except KeyError:
            print(f"[skip] {symbol}: 데이터 없음")
            continue
        if len(closes) < 2:
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
반드시 아래 JSON만 출력하세요. 다른 텍스트나 마크다운 코드블록 금지.
{
  "headline": "오늘 시장을 한 문장으로 (40자 내외)",
  "us": "미국 시장 코멘트 3~4문장",
  "kr": "한국 시장 코멘트 3~4문장",
  "macro": "환율·금리·원자재 코멘트 2~3문장",
  "watch": ["오늘/다음 거래일 체크포인트 3개"],
  "tags": ["아카이브 분류용 주제 태그 3~5개. 짧은 명사, 예: 반도체, 환율, 금리, 실적, 2차전지, 유가"],
  "news_notes": [{"i": 뉴스 번호, "note": "시장 관점 한줄평"}]  // 중요한 뉴스 최대 6개
}"""


def write_with_claude(payload: dict) -> dict | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    from anthropic import Anthropic

    client = Anthropic()
    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5",
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("[claude] JSON 파싱 실패, 기본 요약 사용")
        return None


def fallback_briefing(indices: list[dict]) -> dict:
    by = {i["symbol"]: i for i in indices}
    def line(sym):
        i = by.get(sym)
        return f"{i['name']} {i['pct']:+.2f}%" if i else ""
    return {
        "headline": f"{line('^IXIC')}, {line('^KS11')}",
        "us": f"{line('^IXIC')}, {line('^GSPC')}, {line('^SOX')}로 마감했다.",
        "kr": f"{line('^KS11')}, {line('^KQ11')}로 마감했다.",
        "macro": "",
        "watch": [],
        "tags": [],
        "news_notes": [],
    }


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
