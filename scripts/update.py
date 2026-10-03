#!/usr/bin/env python3
"""
AlphaEdge Free Daily Scanner — news-first edition.

FREE / NO PAID API KEY REQUIRED
- Market data: Yahoo Finance public chart endpoint (daily OHLCV).
- Company news: Yahoo Finance public RSS + Google News RSS.
- Global event discovery: Google News RSS across multiple editions.
- Output: data/picks.json + data/history.json.

Important:
This is not a literal crawl of every article on the internet. Free public RSS
feeds cannot guarantee complete global coverage. The scanner therefore combines
multiple global Google News searches with company-specific searches and then
uses technical data as confirmation rather than as the main driver.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import email.utils
import json
import math
import re
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TODAY = DATA / "picks.json"
HISTORY = DATA / "history.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/154.0 Safari/537.36 AlphaEdgeDaily/2.0"
)

# High-beta / catalyst-sensitive US universe.
# The daily selection can change completely from one session to the next.
UNIVERSE = """
AMD NVDA AVGO SMCI MU ARM TSM INTC ASML AMAT LRCX KLAC ON MCHP MRVL
PLTR CRWD PANW DDOG NET SNOW MDB SHOP UBER DASH HOOD COIN MSTR MARA RIOT CLSK
RKLB ASTS LUNR SPCE SOUN BBAI AI PATH IONQ RGTI QBTS TEM HIMS CELH DKNG
RIVN LCID TSLA NIO XPEV CVNA AFRM UPST SOFI NU TMDX ENPH FSLR OKLO VST
GEV CAVA RBLX APP DUOL AUR ACHR JOBY TSMC
""".split()
UNIVERSE = [s for s in dict.fromkeys(UNIVERSE) if re.fullmatch(r"[A-Z]{1,6}", s)]

# Positive and negative material-event vocabulary.
POS_KWS = [
    # Biotech / pharma / healthcare
    "fda approval", "fda approves", "fda clears", "ema approval", "ema approves",
    "phase 3", "phase iii", "positive phase 3", "positive clinical trial",
    "clinical trial success", "trial meets primary endpoint", "vaccine approved",
    "drug approved", "breakthrough therapy", "priority review", "fast track",
    "orphan drug", "label expansion", "positive data", "positive results",

    # Corporate / commercial
    "acquisition", "acquires", "merger", "takeover", "strategic partnership",
    "major contract", "government contract", "defense contract", "military contract",
    "new contract", "record orders", "record revenue", "record sales", "backlog growth",
    "partnership", "deal signed", "agreement signed", "supply agreement",

    # Earnings / guidance
    "earnings beat", "beats estimates", "revenue beat", "raises guidance",
    "raised guidance", "strong guidance", "profit beat", "record earnings",
    "positive outlook", "strong outlook",

    # Products / technology
    "product launch", "major launch", "new product", "breakthrough",
    "production begins", "commercial launch", "regulatory clearance",

    # Capital allocation
    "share buyback", "buyback", "dividend increase", "insider buying",

    # Macro/regulation benefiting a company/sector
    "government approval", "regulatory approval", "subsidy", "grant awarded",
    "tax credit", "export license"
]

NEG_KWS = [
    # Pharma / clinical
    "trial failed", "clinical trial failure", "phase 3 failed", "phase iii failed",
    "fda rejection", "fda rejects", "drug rejected", "clinical hold", "safety concern",
    "adverse event", "failed endpoint", "negative trial", "negative data",

    # Business / legal
    "lawsuit", "investigation", "subpoena", "fraud", "regulatory investigation",
    "antitrust", "bankruptcy", "default", "recall", "production halt",

    # Earnings
    "earnings miss", "misses estimates", "revenue miss", "cuts guidance",
    "cut guidance", "lowered guidance", "profit warning", "weak outlook",

    # Capital / dilution
    "share offering", "secondary offering", "dilution", "capital raise",
    "convertible notes",

    # Operations / geopolitics
    "factory shutdown", "layoffs", "supply shortage", "export ban", "ban",
    "sanctions", "government restriction", "data breach"
]

MAJOR_EVENT_TERMS = [
    "fda", "ema", "phase 3", "phase iii", "approval", "approved", "acquisition",
    "merger", "takeover", "contract", "earnings", "guidance", "recall", "lawsuit",
    "investigation", "offering", "bankruptcy", "clinical trial", "endpoint",
    "partnership", "regulatory", "government", "military"
]

POSITIVE_FAST = [
    "approved", "approval", "beat", "beats", "raises guidance", "raised guidance",
    "contract", "acquisition", "partnership", "record", "positive", "breakthrough",
    "orders", "launch", "grant", "license", "clearance"
]
NEGATIVE_FAST = [
    "rejected", "rejection", "failed", "miss", "misses", "cuts guidance", "cut guidance",
    "lawsuit", "investigation", "recall", "offering", "dilution", "bankruptcy",
    "safety concern", "negative", "ban", "sanctions", "halt", "warning"
]

# Google News editions used for broader international coverage.
GOOGLE_EDITIONS = [
    ("en-US", "US", "US:en"),
    ("en-GB", "GB", "GB:en"),
    ("en-IN", "IN", "IN:en"),
    ("es-ES", "ES", "ES:es"),
]

GLOBAL_EVENT_QUERIES = [
    '"FDA" approval stock when:1d',
    '"FDA" approves drug when:1d',
    '"phase 3" clinical trial company when:1d',
    '"earnings" "raises guidance" stock when:1d',
    '"beats estimates" company stock when:1d',
    '"major contract" company stock when:1d',
    '"government contract" company stock when:1d',
    '"acquisition" company stock when:1d',
    '"merger" company stock when:1d',
    '"product launch" company stock when:1d',
    '"regulatory approval" company stock when:1d',
    '"lawsuit" company stock when:1d',
    '"recall" company stock when:1d',
    '"dilution" company stock when:1d',
    '"export ban" company stock when:1d',
]

STOPWORDS = {
    "inc", "inc.", "corp", "corp.", "corporation", "company", "co", "co.",
    "holdings", "holding", "plc", "ltd", "ltd.", "class", "common", "shares",
    "ordinary", "the", "and", "of", "group", "technologies", "technology",
    "systems", "international", "global", "health", "financial", "finance"
}


def http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
        },
    )
    last = None
    for wait in (0, 1, 2):
        try:
            if wait:
                time.sleep(wait)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
    raise last or RuntimeError("request failed")


def safe_float(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def yahoo_chart(symbol: str):
    q = urllib.parse.urlencode(
        {
            "range": "6mo",
            "interval": "1d",
            "includePrePost": "true",
            "events": "div,splits",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{q}"
    raw = json.loads(http_get(url))
    result = raw.get("chart", {}).get("result", [None])[0]
    if not result:
        raise RuntimeError("no Yahoo chart result")

    timestamps = result.get("timestamp") or []
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    bars = []

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    for i, timestamp in enumerate(timestamps):
        try:
            o = safe_float(opens[i])
            h = safe_float(highs[i])
            l = safe_float(lows[i])
            c = safe_float(closes[i])
            v = safe_float(volumes[i]) or 0.0
            if None not in (o, h, l, c):
                bars.append({"t": int(timestamp), "o": o, "h": h, "l": l, "c": c, "v": v})
        except Exception:
            continue

    return bars, result.get("meta", {})


def rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-period - 1 : -1], closes[-period:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def ema(closes, period):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    for price in closes[period:]:
        value = price * k + value * (1.0 - k)
    return value


def atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for prev, current in zip(bars[-period - 1 : -1], bars[-period:]):
        true_ranges.append(
            max(
                current["h"] - current["l"],
                abs(current["h"] - prev["c"]),
                abs(current["l"] - prev["c"]),
            )
        )
    return sum(true_ranges) / len(true_ranges)


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    return title


def parse_pubdate(value: str):
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(dt.timezone.utc)
    except Exception:
        return None


def recency_multiplier(published: str) -> float:
    parsed = parse_pubdate(published)
    if not parsed:
        return 0.8
    age_hours = max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 3600.0)
    if age_hours <= 3:
        return 1.50
    if age_hours <= 8:
        return 1.35
    if age_hours <= 16:
        return 1.20
    if age_hours <= 24:
        return 1.05
    if age_hours <= 48:
        return 0.80
    return 0.50


def google_news(query, hl="en-US", gl="US", ceid="US:en", limit=12):
    encoded = urllib.parse.quote_plus(query)
    url = (
        "https://news.google.com/rss/search?"
        f"q={encoded}&hl={hl}&gl={gl}&ceid={ceid}"
    )
    try:
        root = ET.fromstring(http_get(url, timeout=12))
    except Exception:
        return []

    items = []
    for item in root.findall(".//item")[:limit]:
        title = clean_title(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source = (item.findtext("source") or "").strip()
        if title:
            items.append(
                {
                    "title": title,
                    "url": link,
                    "published": pub,
                    "source": source or "Google News",
                }
            )
    return items


def yahoo_news(symbol, limit=8):
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(symbol)}&region=US&lang=en-US"
    try:
        root = ET.fromstring(http_get(url, timeout=12))
    except Exception:
        return []

    out = []
    for item in root.findall(".//item")[:limit]:
        title = clean_title(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            out.append({"title": title, "url": link, "published": pub, "source": "Yahoo Finance"})
    return out


def company_tokens(company_name: str):
    raw = re.findall(r"[A-Za-z0-9&'-]+", company_name or "")
    tokens = []
    for token in raw:
        low = token.lower()
        if len(low) >= 4 and low not in STOPWORDS and low not in tokens:
            tokens.append(low)
    return tokens[:5]


def make_company_query(symbol: str, company_name: str):
    tokens = company_tokens(company_name)
    if tokens:
        # Search the company as an exact phrase and keep the ticker as a second signal.
        phrase = " ".join(tokens[:3])
        return f'"{phrase}" OR "{symbol}" when:2d'
    return f'"{symbol}" when:2d'


def dedupe_news(items):
    unique = {}
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", item.get("title", "").lower()).strip()
        if key:
            # Prefer the newest item for duplicate titles.
            if key not in unique or item.get("published", "") > unique[key].get("published", ""):
                unique[key] = item
    return list(unique.values())


def relevance_to_company(item, symbol: str, company_name: str) -> float:
    title = item.get("title", "").lower()
    symbol_hit = bool(re.search(rf"\b{re.escape(symbol.lower())}\b", title))
    tokens = company_tokens(company_name)
    token_hits = sum(1 for token in tokens if token in title)

    # Exact ticker = strongest; multiple company-name tokens = strong enough.
    if symbol_hit:
        return 1.35
    if token_hits >= 3:
        return 1.25
    if token_hits == 2:
        return 1.10
    if token_hits == 1:
        return 0.85
    return 0.0


def keyword_hits(text: str, keywords):
    low = text.lower()
    return [kw for kw in keywords if kw in low]


def event_type(title: str):
    low = title.lower()
    if any(k in low for k in ["fda", "ema", "phase 3", "phase iii", "clinical trial", "vaccine", "drug"]):
        return "REGULATORY / CLINICAL"
    if any(k in low for k in ["earnings", "guidance", "revenue", "profit"]):
        return "EARNINGS / GUIDANCE"
    if any(k in low for k in ["acquisition", "acquires", "merger", "takeover"]):
        return "M&A"
    if any(k in low for k in ["contract", "orders", "agreement", "government contract", "defense contract"]):
        return "CONTRACT / ORDERS"
    if any(k in low for k in ["launch", "product", "breakthrough", "production"]):
        return "PRODUCT / TECHNOLOGY"
    if any(k in low for k in ["lawsuit", "investigation", "recall", "ban", "sanctions", "dilution", "offering"]):
        return "RISK EVENT"
    return "OTHER"


def score_one_news(item, symbol: str, company_name: str):
    title = item.get("title", "")
    low = title.lower()
    relevance = relevance_to_company(item, symbol, company_name)
    if relevance <= 0:
        return 0.0, False, "OTHER"

    pos = len(keyword_hits(low, POS_KWS))
    neg = len(keyword_hits(low, NEG_KWS))
    major = len(keyword_hits(low, MAJOR_EVENT_TERMS))
    positive_fast = len(keyword_hits(low, POSITIVE_FAST))
    negative_fast = len(keyword_hits(low, NEGATIVE_FAST))
    recency = recency_multiplier(item.get("published", ""))

    raw = (
        positive_fast * 4.0
        + major * 2.0
        + pos * 5.0
        - negative_fast * 5.5
        - neg * 7.0
    )

    # Material events deserve more weight than generic market commentary.
    if any(term in low for term in ["fda approval", "fda approves", "ema approval", "phase 3", "phase iii"]):
        raw += 10.0
    if any(term in low for term in ["raises guidance", "raised guidance", "earnings beat", "beats estimates"]):
        raw += 7.0
    if any(term in low for term in ["major contract", "government contract", "defense contract"]):
        raw += 6.0

    score = raw * relevance * recency
    return score, True, event_type(title)


def score_news(items, symbol: str, company_name: str):
    scored = []
    for item in items:
        score, relevant, kind = score_one_news(item, symbol, company_name)
        if relevant:
            enriched = dict(item)
            enriched["eventType"] = kind
            enriched["impactScore"] = round(score, 2)
            scored.append((score, enriched))

    scored.sort(key=lambda x: x[0], reverse=True)
    relevant_items = [x[1] for x in scored[:8]]

    total = sum(x[0] for x in scored[:5]) if scored else 0.0
    # Cap the news score to keep it meaningful but dominant in the full ranking.
    news_score = max(-40.0, min(50.0, total))

    pos_count = sum(1 for x in scored if x[0] > 0)
    neg_count = sum(1 for x in scored if x[0] < 0)
    major_count = sum(1 for _, x in scored if x.get("eventType") != "OTHER")

    if not relevant_items:
        note = "No material company-specific headline matched in the latest public feeds."
    else:
        lead = relevant_items[0]
        note = (
            f"{len(relevant_items)} relevant headlines; {major_count} material events; "
            f"lead event: {lead.get('eventType')}"
        )

    return round(news_score, 2), note, relevant_items


def discover_global_news():
    """Pull broad global event headlines before candidate scoring."""
    collected = []

    # Use several high-impact event searches in multiple editions, but keep the
    # request count bounded so the free scanner remains practical.
    jobs = []
    for query in GLOBAL_EVENT_QUERIES:
        for hl, gl, ceid in [("en-US", "US", "US:en"), ("en-GB", "GB", "GB:en")]:
            jobs.append((query, hl, gl, ceid))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(google_news, *job, 10) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception:
                pass

    return dedupe_news(collected)


def company_news(symbol: str, company_name: str, global_headlines):
    """Combine ticker RSS, targeted global company news and matching global event headlines."""
    items = []
    items.extend(yahoo_news(symbol, limit=8))

    query = make_company_query(symbol, company_name)
    for hl, gl, ceid in GOOGLE_EDITIONS[:2]:
        items.extend(google_news(query, hl, gl, ceid, limit=10))

    # Match broad global event headlines to this company.
    for item in global_headlines:
        rel = relevance_to_company(item, symbol, company_name)
        if rel > 0:
            item2 = dict(item)
            item2["globalDiscovery"] = True
            items.append(item2)

    return dedupe_news(items)


def technical_score(p, e20, e50, r, rv, ch5, ch20, volpct, dist_high):
    score = 0.0
    score += max(0.0, min(12.0, ch5 * 0.8))
    score += max(0.0, min(10.0, ch20 * 0.4))
    score += 8.0 if e20 and p > e20 else 0.0
    score += 5.0 if e50 and p > e50 else 0.0
    score += 7.0 if r and 52 <= r <= 72 else (3.0 if r and 45 <= r < 52 else 0.0)
    score += 8.0 if rv >= 1.5 else (5.0 if rv >= 1.1 else 0.0)
    score += max(0.0, min(5.0, volpct))
    score += 4.0 if dist_high > -5.0 else 0.0
    return max(0.0, min(50.0, score))


def build_trade_levels(price, atr_value, recent_low, recent_high):
    atrv = atr_value or price * 0.04
    # High-beta risk model: stop is volatility-aware and generally below the 20d structure.
    structure_stop = recent_low * 0.985
    atr_stop = price - 1.35 * atrv
    stop = min(atr_stop, price * 0.97)
    if structure_stop < price:
        stop = min(stop, max(structure_stop, price * 0.78))

    # Prevent nonsensical distances.
    min_stop = price * 0.70
    stop = max(min(stop, price * 0.97), min_stop)
    risk_per_share = max(0.01, price - stop)

    tp1 = price + 2.0 * risk_per_share
    tp2 = price + 3.2 * risk_per_share
    rr = (tp2 - price) / risk_per_share if risk_per_share else 0.0

    return stop, tp1, tp2, rr


def candidate_from_market(symbol, market, global_headlines):
    bars, meta = market
    if len(bars) < 55:
        return None

    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    price = closes[-1]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    r = rsi(closes, 14)
    a = atr(bars, 14)

    ch5 = (price / closes[-6] - 1.0) * 100.0
    ch20 = (price / closes[-21] - 1.0) * 100.0
    avgvol = sum(volumes[-21:-1]) / 20.0 if len(volumes) >= 21 else 0.0
    rv = volumes[-1] / avgvol if avgvol else 0.0
    volpct = (a / price * 100.0) if a and price else 0.0
    high20 = max(x["h"] for x in bars[-20:])
    low20 = min(x["l"] for x in bars[-20:])
    dist_high = (price / high20 - 1.0) * 100.0 if high20 else 0.0

    name = meta.get("longName") or meta.get("shortName") or symbol
    news_items = company_news(symbol, name, global_headlines)
    news_score, news_note, ranked_news = score_news(news_items, symbol, name)

    tech = technical_score(price, e20, e50, r, rv, ch5, ch20, volpct, dist_high)

    # News-first model. Material positive news can lift a candidate sharply;
    # technical conditions then decide whether the setup is tradeable.
    total_score = max(0.0, min(100.0, 50.0 + news_score + tech * 0.95))

    # A clearly adverse material headline should strongly suppress a long setup.
    adverse_material = news_score <= -12.0
    if adverse_material:
        total_score *= 0.45

    stop, tp1, tp2, rr = build_trade_levels(price, a, low20, high20)
    direction = "Catalyst + momentum" if news_score > 10 and ch20 > 0 else (
        "Catalyst setup" if news_score > 10 else "Technical confirmation"
    )

    rationale = []
    if ranked_news:
        lead = ranked_news[0]
        rationale.append(f"{lead['eventType']}: {lead['title']}")
    if ch5 > 2:
        rationale.append(f"5D momentum +{ch5:.1f}%")
    if rv >= 1.3:
        rationale.append(f"relative volume {rv:.1f}x")
    if e20 and price > e20:
        rationale.append("price above EMA20")
    if r and 52 <= r <= 72:
        rationale.append(f"RSI {r:.0f} confirms momentum")
    if not rationale:
        rationale.append("No single technical factor dominates; multi-factor setup.")

    # Confidence = quality of evidence, not probability of profit.
    confidence = 50.0
    if ranked_news:
        confidence += min(18.0, max(0.0, news_score) * 0.30)
    confidence += min(12.0, tech * 0.18)
    if adverse_material:
        confidence -= 15.0
    confidence = max(45.0, min(88.0, confidence))

    risks = [
        "High volatility may create gaps through the modeled stop.",
        "A strong headline can already be priced in before the market opens.",
        "Sector or market reversal can overwhelm the company-specific catalyst.",
    ]
    if adverse_material:
        risks.insert(0, "Latest relevant headlines contain material adverse language.")

    return {
        "ticker": symbol,
        "name": name,
        "price": round(price, 2),
        "change5d": round(ch5, 2),
        "change20d": round(ch20, 2),
        "rsi": round(r, 1) if r is not None else None,
        "ema20": round(e20, 2) if e20 is not None else None,
        "ema50": round(e50, 2) if e50 is not None else None,
        "atr": round(a or price * 0.04, 2),
        "relVolume": round(rv, 2),
        "volatilityPct": round(volpct, 2),
        "high20": round(high20, 2),
        "low20": round(low20, 2),
        "newsScore": round(news_score, 2),
        "technicalScore": round(tech, 2),
        "score": round(total_score, 1),
        "setup": direction,
        "entry": round(price, 2),
        "stop": round(stop, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "rr": round(rr, 2),
        "confidence": round(confidence, 0),
        "whyNow": " | ".join(rationale[:4]),
        "thesis": (
            f"The setup is primarily driven by the latest company-specific/news catalyst "
            f"({event_type(ranked_news[0]['title']) if ranked_news else 'no confirmed material event'}) "
            f"with technical confirmation. This is a scenario, not a certainty."
        ),
        "catalysts": [x["title"] for x in ranked_news[:4]],
        "risks": risks,
        "invalidate": f"Close below ${stop:.2f} or a material deterioration in the catalyst/news thesis.",
        "news": ranked_news[:8],
        "source": "Yahoo Finance public chart/RSS + Google News public RSS",
        "newsNote": news_note,
    }


def load_history():
    if not HISTORY.exists():
        return []
    try:
        data = json.loads(HISTORY.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def choose_diverse(results, count=5):
    # Keep the ranking news-first while avoiding a portfolio made entirely of one theme.
    results = sorted(
        results,
        key=lambda x: (
            x.get("newsScore", 0),
            x.get("score", 0),
            x.get("change20d", 0),
        ),
        reverse=True,
    )

    picks = []
    seen_clusters = {}

    def cluster(ticker):
        tech = {
            "AMD", "NVDA", "AVGO", "SMCI", "MU", "ARM", "TSM", "INTC", "ASML", "AMAT",
            "LRCX", "KLAC", "ON", "MCHP", "MRVL", "PLTR", "CRWD", "PANW", "DDOG", "NET",
            "SNOW", "MDB", "IONQ", "RGTI", "QBTS", "AI", "SOUN", "BBAI", "PATH"
        }
        space = {"RKLB", "ASTS", "LUNR", "SPCE", "OKLO", "ACHR", "JOBY", "AUR"}
        fintech = {"HOOD", "COIN", "MSTR", "MARA", "RIOT", "CLSK", "SOFI", "NU", "UPST", "AFRM"}
        EV = {"TSLA", "RIVN", "LCID", "NIO", "XPEV", "CVNA"}
        health = {"HIMS", "TMDX"}
        if ticker in tech:
            return "technology"
        if ticker in space:
            return "space"
        if ticker in fintech:
            return "fintech/crypto"
        if ticker in EV:
            return "EV/mobility"
        if ticker in health:
            return "healthcare"
        return "other"

    for item in results:
        c = cluster(item["ticker"])
        if seen_clusters.get(c, 0) >= 2:
            continue
        picks.append(item)
        seen_clusters[c] = seen_clusters.get(c, 0) + 1
        if len(picks) == count:
            break

    # Fill any missing slots with the remaining highest-score names.
    if len(picks) < count:
        picked_tickers = {x["ticker"] for x in picks}
        for item in results:
            if item["ticker"] in picked_tickers:
                continue
            picks.append(item)
            if len(picks) == count:
                break

    return picks[:count]


def fetch_markets():
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(yahoo_chart, symbol): symbol for symbol in UNIVERSE}
        for future in concurrent.futures.as_completed(futures):
            symbol = futures[future]
            try:
                results[symbol] = future.result()
            except Exception:
                continue
    return results


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    generated = dt.datetime.now(dt.timezone.utc).isoformat()

    print(f"AlphaEdge scan started: {generated}")
    print(f"Universe: {len(UNIVERSE)} tickers")

    # Step 1: market data.
    markets = fetch_markets()
    print(f"Market data received: {len(markets)}")

    # Step 2: broad world-event discovery.
    global_headlines = discover_global_news()
    print(f"Global event headlines discovered: {len(global_headlines)}")

    # Step 3: full company-level analysis.
    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(candidate_from_market, symbol, markets[symbol], global_headlines): symbol
            for symbol in markets
            if symbol in markets
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    candidates.append(result)
            except Exception:
                continue

    # Require a meaningful news signal where possible. If public feeds fail,
    # retain the strongest technical candidates as a fallback rather than producing no output.
    news_driven = [x for x in candidates if x.get("newsScore", 0) >= 2]
    pool = news_driven if len(news_driven) >= 5 else candidates
    picks = choose_diverse(pool, count=5)

    if len(picks) < 5 and TODAY.exists():
        print("Not enough candidates; keeping previous picks.")
        return

    for rank, pick in enumerate(picks, start=1):
        pick["rank"] = rank

    payload = {
        "asOf": today,
        "generatedAt": generated,
        "mode": "FREE DAILY NEWS-FIRST SCAN",
        "universeSize": len(UNIVERSE),
        "candidatesScanned": len(candidates),
        "globalHeadlinesDiscovered": len(global_headlines),
        "newsDrivenCandidates": len(news_driven),
        "picks": picks,
        "method": (
            "Daily market data + Yahoo ticker news + Google News RSS global event discovery + "
            "company-specific multi-edition news + technical confirmation."
        ),
        "disclaimer": (
            "Automated research/ranking using free public data sources. This is not financial advice. "
            "Stop-loss and profit targets are model scenarios and may not execute at those prices, "
            "especially in volatile or gapping markets."
        ),
    }

    TODAY.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    history = load_history()
    history = [item for item in history if item.get("asOf") != today]
    history.insert(0, payload)
    HISTORY.write_text(json.dumps(history[:90], indent=2, ensure_ascii=False), encoding="utf-8")

    print("Daily picks:", [x["ticker"] for x in picks])
    print(f"Saved {TODAY} and {HISTORY}")


if __name__ == "__main__":
    main()
