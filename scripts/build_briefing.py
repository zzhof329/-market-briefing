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

# 시가총액 상위 10개를 매번 자동으로 고르기 위한 후보군 (순위가 바뀌어도 따라감)
# 한국은 우선주(삼성전자우 등)를 빼고 보통주 기준
TOP_N = 10
US_POOL = [
    ("NVDA", "엔비디아"), ("MSFT", "마이크로소프트"), ("AAPL", "애플"), ("GOOGL", "알파벳"),
    ("AMZN", "아마존"), ("META", "메타"), ("AVGO", "브로드컴"), ("TSLA", "테슬라"),
    ("BRK-B", "버크셔 해서웨이"), ("LLY", "일라이 릴리"), ("JPM", "JP모건"), ("WMT", "월마트"),
    ("ORCL", "오라클"), ("V", "비자"), ("MA", "마스터카드"), ("NFLX", "넷플릭스"),
    ("XOM", "엑슨모빌"), ("COST", "코스트코"), ("PLTR", "팔란티어"), ("AMD", "AMD"),
    ("UNH", "유나이티드헬스"), ("JNJ", "존슨앤드존슨"),
]
KR_POOL = [
    ("005930.KS", "삼성전자"), ("000660.KS", "SK하이닉스"), ("373220.KS", "LG에너지솔루션"),
    ("207940.KS", "삼성바이오로직스"), ("005380.KS", "현대차"), ("012450.KS", "한화에어로스페이스"),
    ("105560.KS", "KB금융"), ("000270.KS", "기아"), ("068270.KS", "셀트리온"),
    ("035420.KS", "NAVER"), ("329180.KS", "HD현대중공업"), ("055550.KS", "신한지주"),
    ("034020.KS", "두산에너빌리티"), ("012330.KS", "현대모비스"), ("028260.KS", "삼성물산"),
    ("042660.KS", "한화오션"), ("009540.KS", "HD한국조선해양"), ("005490.KS", "POSCO홀딩스"),
    ("006400.KS", "삼성SDI"), ("032830.KS", "삼성생명"), ("402340.KS", "SK스퀘어"),
    ("086790.KS", "하나금융지주"), ("035720.KS", "카카오"), ("010130.KS", "고려아연"),
]


def top_by_market_cap(pool: list[tuple], n: int = TOP_N) -> tuple[list[tuple], dict]:
    """후보군의 시가총액을 조회해 상위 n개를 고른다. 조회가 안 되면 후보군 순서대로."""
    caps = {}
    for symbol, _ in pool:
        for attempt in range(2):
            try:
                cap = yf.Ticker(symbol).fast_info["market_cap"]
                if cap and cap > 0:
                    caps[symbol] = float(cap)
                break
            except Exception as e:  # noqa: BLE001
                if attempt:
                    print(f"[cap] {symbol}: {e}")
                time.sleep(1)
    if len(caps) < n:
        print(f"[cap] 시가총액을 {len(caps)}개만 받아 일부는 후보군 순서로 채웁니다")
    ranked = sorted(pool, key=lambda r: -caps.get(r[0], -pool.index(r)))
    return ranked[:n], caps


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
# ── 오늘의 상승·하락 상위 ────────────────────────────────────────
def us_movers(kind: str, n: int = 10) -> list[dict]:
    """Yahoo Finance 기본 스크리너(day_gainers / day_losers): 시총 20억 달러 이상 미국 종목"""
    quotes = []
    try:
        if hasattr(yf, "screen"):
            res = yf.screen(kind, count=25)
        else:  # 예전 yfinance
            sc = yf.Screener()
            sc.set_predefined_body(kind)
            res = sc.response
        quotes = (res or {}).get("quotes", [])
    except Exception as e:  # noqa: BLE001
        print(f"[movers] 미국 {kind} 실패: {e}")
    out = []
    for q in quotes:
        pct = q.get("regularMarketChangePercent")
        if pct is None or not q.get("symbol"):
            continue
        out.append({"symbol": q["symbol"], "name": q.get("shortName") or q.get("longName") or q["symbol"],
                    "price": q.get("regularMarketPrice"), "pct": round(float(pct), 2), "cap": q.get("marketCap")})
    out.sort(key=lambda r: -r["pct"] if kind == "day_gainers" else r["pct"])
    return out[:n]


KR_SKIP = re.compile(r"스팩|ETN|레버리지|인버스|(우|우B|우C|\d우B?)$")


def kr_movers(kind: str, sosok: int, n: int = 10) -> list[dict]:
    """네이버 증권 상승/하락 순위 (sosok 0=코스피, 1=코스닥). 우선주·스팩·ETN 제외"""
    url = f"https://finance.naver.com/sise/sise_{kind}.naver?sosok={sosok}"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "euc-kr"
        html = r.text
    except Exception as e:  # noqa: BLE001
        print(f"[movers] 네이버 {kind}/{sosok} 실패: {e}")
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m_name = re.search(r'class="tltle"[^>]*>(.*?)</a>', tr)
        m_code = re.search(r"code=(\d{6})", tr)
        if not (m_name and m_code):
            continue
        name = re.sub(r"<[^>]+>", "", m_name.group(1)).strip()
        if KR_SKIP.search(name):
            continue
        cells = [re.sub(r"<[^>]+>|\s", "", c) for c in re.findall(r'<td class="number"[^>]*>(.*?)</td>', tr, re.S)]
        pct_cell = next((c for c in cells if c.endswith("%")), None)
        try:
            price = float(cells[0].replace(",", ""))
            pct = float(pct_cell.replace("%", "").replace("+", ""))
        except Exception:  # noqa: BLE001
            continue
        out.append({"symbol": m_code.group(1), "name": name, "price": price, "pct": pct})
        if len(out) >= n:
            break
    if not out:
        print(f"[movers] 네이버 {kind}/{sosok}: 표를 읽지 못했습니다 (페이지 구조 변경 가능)")
    return out


KOSDAQ_POOL = [
    ("247540.KQ", "에코프로비엠"), ("086520.KQ", "에코프로"), ("196170.KQ", "알테오젠"), ("028300.KQ", "HLB"),
    ("277810.KQ", "레인보우로보틱스"), ("141080.KQ", "리가켐바이오"), ("298380.KQ", "에이비엘바이오"),
    ("145020.KQ", "휴젤"), ("214150.KQ", "클래시스"), ("058470.KQ", "리노공업"), ("039030.KQ", "이오테크닉스"),
    ("263750.KQ", "펄어비스"), ("357780.KQ", "솔브레인"), ("403870.KQ", "HPSP"), ("095340.KQ", "ISC"),
    ("240810.KQ", "원익IPS"), ("293490.KQ", "카카오게임즈"), ("035900.KQ", "JYP Ent."),
]


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("+", ""))
    except Exception:  # noqa: BLE001
        return None


def kr_movers_api(kind: str, market: str, n: int = 10) -> list[dict]:
    """네이버 증권 모바일 데이터 (kind: up/down, market: KOSPI/KOSDAQ)"""
    url = f"https://m.stock.naver.com/api/stocks/{kind}/{market}?page=1&pageSize=40"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"})
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[movers] 네이버 API {kind}/{market} 실패: {e}")
        return []
    rows = data.get("stocks") if isinstance(data, dict) else data
    out = []
    for it in rows or []:
        name = it.get("stockName") or it.get("itemName") or ""
        code = it.get("itemCode") or it.get("reutersCode") or ""
        pct, price = _num(it.get("fluctuationsRatio")), _num(it.get("closePrice"))
        if not name or pct is None or KR_SKIP.search(name) or str(it.get("stockEndType", "stock")) not in ("stock", ""):
            continue
        out.append({"symbol": str(code)[:6], "name": name, "price": price, "pct": pct})
        if len(out) >= n:
            break
    if not out:
        print(f"[movers] 네이버 API {kind}/{market}: 받은 종목이 없습니다")
    return out


def pool_movers(pool: list[tuple], n: int = 10) -> dict:
    """대체 수단: 대형주 후보군 안에서 등락률 순으로 정렬"""
    rows = fetch_quotes(pool)
    rows = [{"symbol": r["symbol"][:6], "name": r["name"], "price": r["price"], "pct": r["pct"]} for r in rows]
    up = sorted([r for r in rows if r["pct"] > 0], key=lambda r: -r["pct"])[:n]
    down = sorted([r for r in rows if r["pct"] < 0], key=lambda r: r["pct"])[:n]
    return {"up": up, "down": down, "src": "pool"}


def fetch_movers() -> dict:
    result = {"us": {"up": us_movers("day_gainers"), "down": us_movers("day_losers"), "src": "yahoo"}}
    for key, market, sosok, pool in (("kospi", "KOSPI", 0, KR_POOL), ("kosdaq", "KOSDAQ", 1, KOSDAQ_POOL)):
        up, down, src = kr_movers_api("up", market), kr_movers_api("down", market), "naver"
        if not up and not down:
            up, down = kr_movers("rise", sosok), kr_movers("fall", sosok)
        if not up and not down:
            print(f"[movers] {market}: 네이버에서 받지 못해 대형주 기준으로 대체합니다")
            result[key] = pool_movers(pool)
            continue
        result[key] = {"up": up, "down": down, "src": src}
    for k, v in result.items():
        print(f"[movers] {k}: 상승 {len(v['up'])}개, 하락 {len(v['down'])}개 ({v.get('src')})")
    return result


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
시장 이름은 정확하게 쓰세요. '뉴욕 증시', '뉴욕장', '월가', '서울 증시', '서울장', '여의도' 같은 표현 대신
'미국 증시'·'나스닥'·'S&P 500'·'다우', '한국 증시'·'코스피'·'코스닥'처럼 시장·지수 이름을 쓰세요.
반드시 아래 형식의 JSON 객체 하나만 출력하세요. 설명 문장, 마크다운 코드블록, 주석(//)은 넣지 마세요.
{
  "headline": "오늘 시장을 한 문장으로 (40자 내외)",
  "us": "미국 시장 코멘트 3~4문장 (300자 이내)",
  "kr": "한국 시장 코멘트 3~4문장 (300자 이내)",
  "macro": "환율·금리·원자재 코멘트 2~3문장 (200자 이내)",
  "watch": ["오늘/다음 거래일 체크포인트 3개. upcoming_events가 있으면 날짜와 함께 반영"],
  "tags": ["아카이브 분류용 주제 태그 3~5개. 짧은 명사, 예: 반도체, 환율, 금리, 실적, 2차전지, 유가"],
  "ko_names": {"티커": "us_movers_names에 있는 미국 종목의 한국어 회사명. 국내 언론 표기 기준, 짧게(예: GAP→갭, GPN→글로벌 페이먼츠)"},
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
{"events": [{"date": "YYYY-MM-DD", "time": "21:30", "country": "us 또는 kr", "title": "짧은 일정명", "short": "달력 칸용 2~6글자 약칭(예: CPI, 고용, FOMC, 금통위, 엔비디아)", "importance": 1~3 숫자(3=시장 영향 큼), "note": "한 줄 설명(선택)"}]}"""

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
                     "title": str(e["title"])[:40], "short": str(e.get("short", ""))[:8], "importance": int(e.get("importance", 1) or 1),
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
    stocks = {}
    for mk, pool in (("us", US_POOL), ("kr", KR_POOL)):
        top, caps = top_by_market_cap(pool)
        rows = fetch_quotes(top)
        for rank, r in enumerate(sorted(rows, key=lambda r: -caps.get(r["symbol"], 0)), 1):
            r["cap"] = caps.get(r["symbol"])
            r["rank"] = rank
        stocks[mk] = sorted(rows, key=lambda r: r["rank"])
    sectors = {"us": fetch_quotes(US_SECTORS), "kr": fetch_quotes(KR_SECTORS)}
    macro = fetch_quotes(MACRO)
    if kr3y := fetch_ecos_kr3y():
        macro.insert(3, kr3y)
    news = fetch_news()
    try:
        movers = fetch_movers()
    except Exception as e:  # noqa: BLE001
        print(f"[movers] 실패: {e}")
        movers = {}

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
        "session": "미국 증시 마감 직후" if session == "morning" else "한국 증시 마감 직후",
        "indices": lite(indices),
        "stocks": {k: lite(v) for k, v in stocks.items()},
        "sectors": {k: lite(v) for k, v in sectors.items()},
        "macro": lite(macro),
        "news": [{"i": n, "market": x["market"], "title": x["title"]} for n, x in enumerate(news)],
        "us_movers_names": [{"symbol": r["symbol"], "name": r["name"]}
                            for r in movers.get("us", {}).get("up", []) + movers.get("us", {}).get("down", [])],
        "movers_top5": {k: {d: [f"{r['name']} {r['pct']:+.1f}%" for r in v.get(d, [])[:5]] for d in ("up", "down")}
                        for k, v in movers.items()},
        "upcoming_events": upcoming,  # watch 항목 작성에 참고
    }
    brief = write_with_claude(payload) or fallback_briefing(indices)

    ko = brief.get("ko_names") or {}
    for d in ("up", "down"):
        for r in movers.get("us", {}).get(d, []):
            if isinstance(ko.get(r["symbol"]), str) and ko[r["symbol"]].strip():
                r["name_en"], r["name"] = r["name"], ko[r["symbol"]].strip()[:20]

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
        "indices": indices, "stocks": stocks, "sectors": sectors, "macro": macro, "movers": movers,
        "news": news,
    }
    save(doc)
    print(f"저장 완료: {doc['date']} {session} / {doc['headline']}")


if __name__ == "__main__":
    main()
