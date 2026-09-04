"""
collector.py
------------
Global Daily News 대시보드용 데이터 수집기.

수집 항목
  1) USD/KRW 환율 (실시간 + 최근 12개월 월별 종가)
  2) 미국 정치/경제/환경 주요 뉴스 (Local Headlines)
  3) 한국타이어 관련 산업/HR 동향 (테네시 공장, 미국 노동법, 공급망 등)
  4) 국가 기본 프로필 (World Bank 공개 지표, 실패 시 캐시/기본값 사용)

결과는 data/latest.json 으로 저장되며, build_site.py 가 이를 읽어
template.html 에 주입해 docs/index.html 을 생성한다.

모든 외부 호출은 개별적으로 try/except 처리하여, 일부 소스가 실패해도
전체 파이프라인이 중단되지 않고 이전 캐시 값으로 대체되도록 설계했다.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta

import requests
import feedparser
from dateutil import parser as dtparser
import pytz

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("collector")

KST = pytz.timezone("Asia/Seoul")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CACHE_PATH = os.path.join(DATA_DIR, "latest.json")

# 한국수출입은행 Open API (선택, 무료 키 필요: https://www.koreaexim.go.kr/ir/HPHKIR019M01)
EXIM_API_KEY = os.environ.get("KOREAEXIM_API_KEY", "")
# NewsAPI.org 키 (선택, 없으면 Google News RSS 만 사용)
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

MONTH_KR = ["1월", "2월", "3월", "4월", "5월", "6월",
            "7월", "8월", "9월", "10월", "11월", "12월"]


# ---------------------------------------------------------------------------
# 0. 캐시 유틸
# ---------------------------------------------------------------------------
def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"캐시 로드 실패: {e}")
    return {}


def save_cache(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 1. 환율 (USD/KRW)
# ---------------------------------------------------------------------------
def get_exchange_rate(cache: dict) -> dict:
    """
    1순위: 한국수출입은행 Open API (KOREAEXIM_API_KEY 존재 시)
    2순위: Yahoo Finance (yfinance, 'KRW=X')
    3순위: 이전 캐시값
    """
    result = None

    # --- 1순위: 한국수출입은행 ---
    if EXIM_API_KEY:
        try:
            today = datetime.now(KST).strftime("%Y%m%d")
            url = (
                "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
                f"?authkey={EXIM_API_KEY}&searchdate={today}&data=AP01"
            )
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            rows = resp.json()
            usd_row = next(r for r in rows if r.get("cur_unit") == "USD")
            rate = float(usd_row["deal_bas_r"].replace(",", ""))
            result = {"current_rate": rate, "source": "한국수출입은행"}
        except Exception as e:
            log.warning(f"한국수출입은행 API 실패, Yahoo Finance로 대체: {e}")

    # --- 2순위: Yahoo Finance ---
    history_labels, history_values = [], []
    try:
        import yfinance as yf

        ticker = yf.Ticker("KRW=X")

        if result is None:
            fast = ticker.fast_info
            rate = float(fast["lastPrice"])
            result = {"current_rate": rate, "source": "Yahoo Finance"}

        # 최근 13개월 월봉을 받아 종가 12개 산출 (전일 대비 등락률용 여유분 포함)
        hist = ticker.history(period="13mo", interval="1mo")
        hist = hist.dropna(subset=["Close"]).tail(12)
        for idx, row in hist.iterrows():
            history_labels.append(MONTH_KR[idx.month - 1])
            history_values.append(round(float(row["Close"]), 2))

        # 전일 대비 등락률 (일봉 2개)
        daily = ticker.history(period="5d", interval="1d").dropna(subset=["Close"])
        if len(daily) >= 2:
            prev_close = float(daily["Close"].iloc[-2])
            change_pct = round((result["current_rate"] - prev_close) / prev_close * 100, 2)
        else:
            change_pct = 0.0
        result["change_pct"] = change_pct

    except Exception as e:
        log.warning(f"Yahoo Finance 조회 실패: {e}")

    # --- 3순위: 캐시 폴백 ---
    if result is None:
        cached_rate = cache.get("exchange_rate")
        if cached_rate:
            log.warning("환율 실시간 수집 전체 실패, 캐시값 사용")
            return cached_rate
        # 완전 초기 실행 & 모든 소스 실패 시 안전한 기본값
        return {
            "current_rate": 1380.00,
            "change_pct": 0.0,
            "source": "기본값(수집 실패)",
            "history_labels": MONTH_KR,
            "history_values": [1380.0] * 12,
        }

    if not history_labels:
        # 히스토리만 실패한 경우 캐시의 히스토리로 대체
        cached_rate = cache.get("exchange_rate", {})
        history_labels = cached_rate.get("history_labels", MONTH_KR)
        history_values = cached_rate.get("history_values", [result["current_rate"]] * 12)

    result["history_labels"] = history_labels
    result["history_values"] = history_values
    result.setdefault("change_pct", 0.0)
    return result


# ---------------------------------------------------------------------------
# 2. 현지 주요 뉴스 (미국 정치/경제/환경/사회)
# ---------------------------------------------------------------------------
NEWS_QUERIES = [
    {"category": "POLITICS", "label": "정치", "query": "United States politics", "color": "blue"},
    {"category": "ECONOMY", "label": "경제", "query": "United States economy inflation", "color": "green"},
    {"category": "ENVIRON", "label": "환경", "query": "United States environment climate policy", "color": "orange"},
    {"category": "SOCIETY", "label": "사회", "query": "United States society labor", "color": "purple"},
]


def _fetch_google_news_rss(query: str, limit: int = 3):
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:limit]:
        source = ""
        if hasattr(entry, "source"):
            source = getattr(entry.source, "title", "") or ""
        elif " - " in entry.title:
            source = entry.title.split(" - ")[-1]
        items.append({
            "title": entry.title,
            "link": entry.link,
            "source": source or "Google News",
            "published": entry.get("published", ""),
        })
    return items


def get_local_headlines(cache: dict, per_category: int = 1) -> list:
    headlines = []
    for spec in NEWS_QUERIES[:3]:  # 카드에는 3개 노출 (정치/경제/환경)
        try:
            items = _fetch_google_news_rss(spec["query"], limit=per_category)
            if not items:
                raise ValueError("빈 결과")
            item = items[0]
            headlines.append({
                "category": spec["category"],
                "color": spec["color"],
                "source": item["source"],
                "title": item["title"],
                "link": item["link"],
            })
        except Exception as e:
            log.warning(f"뉴스 수집 실패 ({spec['category']}): {e}")
            # 캐시에서 동일 카테고리 항목 재사용
            cached = next(
                (h for h in cache.get("headlines", []) if h.get("category") == spec["category"]),
                None,
            )
            if cached:
                headlines.append(cached)
    return headlines


# ---------------------------------------------------------------------------
# 3. 산업 및 HR 동향 (한국타이어 / 미국 노동법 / 테네시 공장 / 공급망)
# ---------------------------------------------------------------------------
HR_TREND_QUERIES = [
    {"tag": "투자-KR", "color": "red", "query": "한국타이어 테네시 공장"},
    {"tag": "노동·제도", "color": "indigo", "query": "미국 노동법 개정 2026"},
    {"tag": "지역 노동시장", "color": "teal", "query": "Tennessee unemployment manufacturing"},
    {"tag": "공급망·비용", "color": "yellow", "query": "미국 관세 비관세 장벽 공급망 타이어"},
]


def _first_sentence(text: str, max_len: int = 60) -> str:
    """뉴스 제목/요약에서 핵심 한 줄만 추출 (단순 절단 방식)."""
    text = text.strip().replace("\n", " ")
    for sep in [". ", "다. ", "|", " - "]:
        if sep in text:
            text = text.split(sep)[0]
            break
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def get_industry_hr_trends(cache: dict) -> list:
    trends = []
    for spec in HR_TREND_QUERIES:
        try:
            items = _fetch_google_news_rss(spec["query"], limit=1)
            if not items:
                raise ValueError("빈 결과")
            item = items[0]
            trends.append({
                "tag": spec["tag"],
                "color": spec["color"],
                "title": _first_sentence(item["title"], 24),
                "desc": _first_sentence(item["title"], 60),
                "link": item["link"],
            })
        except Exception as e:
            log.warning(f"HR 동향 수집 실패 ({spec['tag']}): {e}")
            cached = next(
                (t for t in cache.get("hr_trends", []) if t.get("tag") == spec["tag"]),
                None,
            )
            if cached:
                trends.append(cached)
    return trends


# ---------------------------------------------------------------------------
# 4. 국가 기본 프로필 (World Bank 공개 API, 연 단위 지표라 자주 안 변함)
# ---------------------------------------------------------------------------
WB_INDICATORS = {
    "population": "SP.POP.TOTL",
    "gdp": "NY.GDP.MKTP.CD",
    "inflation": "FP.CPI.TOTL.ZG",
    "unemployment": "SL.UEM.TOTL.ZS",
}


def _wb_latest_value(indicator: str):
    url = f"https://api.worldbank.org/v2/country/US/indicator/{indicator}?format=json&per_page=5"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    for row in payload[1]:
        if row.get("value") is not None:
            return row["value"], row["date"]
    raise ValueError("유효 데이터 없음")


def get_country_profile(cache: dict) -> dict:
    cached = cache.get("country_profile", {})
    profile = {
        "capital": "Washington, D.C.",
        "min_wage": "$7.25/h",
    }
    try:
        pop, _ = _wb_latest_value(WB_INDICATORS["population"])
        profile["population"] = f"약 {pop / 1e8:.2f}억 명"
    except Exception as e:
        log.warning(f"인구 지표 수집 실패: {e}")
        profile["population"] = cached.get("population", "약 3.42억 명")

    try:
        gdp, _ = _wb_latest_value(WB_INDICATORS["gdp"])
        profile["gdp"] = f"약 {gdp / 1e12:.1f}조 USD"
    except Exception as e:
        log.warning(f"GDP 지표 수집 실패: {e}")
        profile["gdp"] = cached.get("gdp", "약 30.5조 USD")

    try:
        infl, _ = _wb_latest_value(WB_INDICATORS["inflation"])
        profile["inflation"] = f"{infl:.1f}%"
    except Exception as e:
        log.warning(f"인플레이션 지표 수집 실패: {e}")
        profile["inflation"] = cached.get("inflation", "3.2%")

    try:
        unemp, _ = _wb_latest_value(WB_INDICATORS["unemployment"])
        profile["unemployment"] = f"{unemp:.1f}%"
    except Exception as e:
        log.warning(f"실업률 지표 수집 실패: {e}")
        profile["unemployment"] = cached.get("unemployment", "4.3%")

    return profile


# ---------------------------------------------------------------------------
# 메인 실행
# ---------------------------------------------------------------------------
def main():
    cache = load_cache()

    log.info("환율 데이터 수집 중...")
    exchange_rate = get_exchange_rate(cache)

    log.info("현지 주요 뉴스 수집 중...")
    headlines = get_local_headlines(cache)

    log.info("산업/HR 동향 수집 중...")
    hr_trends = get_industry_hr_trends(cache)

    log.info("국가 기본 프로필 수집 중...")
    country_profile = get_country_profile(cache)

    now_kst = datetime.now(KST)
    data = {
        "generated_at": now_kst.isoformat(),
        "generated_at_display": now_kst.strftime("%Y-%m-%d %H:%M KST"),
        "exchange_rate": exchange_rate,
        "headlines": headlines,
        "hr_trends": hr_trends,
        "country_profile": country_profile,
    }

    save_cache(data)
    log.info(f"수집 완료 -> {CACHE_PATH}")
    return data


if __name__ == "__main__":
    main()
