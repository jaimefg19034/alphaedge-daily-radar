
#!/usr/bin/env python3
"""AlphaEdge — free pre-market catalyst / intraday scanner.

Replaces scripts/update.py. Standard-library only; no paid API keys.

Design goals:
- Discover fresh corporate catalysts from broad Google News searches across regions,
  Yahoo Finance ticker RSS, FDA public RSS feeds, and SEC current 8-K/6-K filing feeds.
- Match discovered headlines to listed tickers using a public SEC ticker/company map,
  with a fallback to the configured US watchlist.
- Treat the news catalyst as the primary input. Daily technicals and pre-market price
  action are confirmation, not the primary reason to select a name.
- Publish five ranked names each scan, but explicitly mark non-qualified ideas as
  WATCH / NO TRADE instead of pretending every day has five valid entries.
- Generate conditional same-day breakout levels from the current pre-market range;
  never silently present yesterday's close as a live entry.

Free sources do not cover every news database or guarantee real-time completeness.
Yahoo Finance's public chart/RSS endpoints are unofficial and may be delayed, throttled,
or changed. This is a rule-based screener, not a predictive AI model.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as XML
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TODAY_FILE = DATA / "picks.json"
HISTORY_FILE = DATA / "history.json"
UTC = dt.timezone.utc
NY_TZ = ZoneInfo("America/New_York")
UA = os.getenv(
    "ALPHAEDGE_USER_AGENT",
    "Mozilla/5.0 (compatible; AlphaEdgeDaily/4.0; GitHubActions; +https://github.com/)"
)

# Broad high-beta/catalyst watchlist; event discovery can add US-listed names from SEC's map.
UNIVERSE = """
AAPL MSFT NVDA AMD AVGO TSM ASML ARM SMCI MU MRVL QCOM TXN INTC AMAT LRCX KLAC ON MCHP
PLTR CRWD PANW DDOG NET SNOW MDB SHOP UBER DASH HOOD COIN MSTR MARA RIOT CLSK APP RBLX
RKLB ASTS LUNR SPCE SOUN BBAI AI PATH IONQ RGTI QBTS TEM HIMS CELH DKNG TSLA RIVN LCID
NIO XPEV CVNA AFRM UPST SOFI NU TMDX ENPH FSLR OKLO VST GEV CAVA DUOL AUR ACHR JOBY
MRNA BNTX PFE LLY NVO ABBV REGN AMGN GILD VRTX BIIB ALNY SRPT CRSP NTLA BEAM EDIT VKTX
ALT SANA IOVA IMVT RXRX RARE ARWR INSM EXAS CORT AXSM NBIX LQDA APLS CYTK PRTA SAVA
JNJ MRK BMY AZN NVS SNY GSK BABA BIDU PDD JD TME SE GRAB MELI WING ELF ULTA LULU NKE
COST WMT TGT DG DLTR BBY CAT DE CMI ETN HON GE RTX LMT NOC BA FDX UPS DAL UAL AAL
XOM CVX OXY SLB XLE GLD SLV FCX AA NEM UUUU CCJ URA WDC STX DELL HPE IBM ORCL CRM NOW
ADBE INTU SQ XYZ PYPL GS JPM BAC C WFC RBLX HOOD DKNG PENN MGM WYNN MAR ABNB EXPE BKNG
""".split()
UNIVERSE = list(dict.fromkeys(s for s in UNIVERSE if re.fullmatch(r"[A-Z]{1,5}", s)))

# Fresh catalyst/event search queries. Google News is an aggregator, not every publisher/database.
GLOBAL_EVENT_QUERIES = [
    'FDA approves drug biotech shares',
    'FDA rejects drug clinical hold biotech stock',
    'phase 3 trial positive topline results company shares',
    'phase 3 trial failed misses primary endpoint biotech',
    'EMA approves drug company stock',
    'earnings beat raises full year guidance stock jumps',
    'earnings miss cuts guidance stock falls',
    'preliminary results company raises outlook shares',
    'company wins major contract stock',
    'government defense contract award company shares',
    'merger acquisition take private buyout offer shares',
    'activist investor 13D stake company stock',
    'product recall safety warning company shares',
    'company announces secondary offering dilution shares',
    'antitrust investigation company shares regulator',
    'cyberattack data breach company stock',
    'production halt factory shutdown company shares',
    'regulatory approval product clearance company stock',
    'patent ruling court verdict company shares',
    'bankruptcy restructuring going concern public company',
    'company signs multibillion dollar supply agreement shares',
    'analyst upgrade downgrade price target shares premarket',
    'FDA panel votes positive drug company shares',
    'clinical trial meets primary endpoint biotechnology company',
    'stock surges after company announcement premarket',
]

# International queries supplement the English-edition coverage.
LOCAL_EVENT_QUERIES = [
    ("es-ES", "ES", "ES:es", 'aprobación FDA resultados fase 3 farmacéutica acciones'),
    ("es-ES", "ES", "ES:es", 'empresa contrato millonario resultados eleva previsiones bolsa'),
    ("fr-FR", "FR", "FR:fr", 'approbation FDA résultats phase 3 entreprise actions'),
    ("fr-FR", "FR", "FR:fr", 'contrat majeur résultats relève prévisions action'),
    ("de-DE", "DE", "DE:de", 'FDA Zulassung Phase 3 Studienergebnisse Aktie Unternehmen'),
    ("de-DE", "DE", "DE:de", 'Großauftrag Prognose erhöht Aktie Unternehmen'),
    ("ja-JP", "JP", "JP:ja", 'FDA 承認 臨床試験 フェーズ3 株価 企業'),
]

GOOGLE_EDITIONS = [
    ("en-US", "US", "US:en"),
    ("en-GB", "GB", "GB:en"),
    ("en-CA", "CA", "CA:en"),
    ("en-AU", "AU", "AU:en"),
    ("en-IN", "IN", "IN:en"),
]

FDA_FEEDS = [
    ("FDA Drug Updates", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml"),
    ("FDA Biologics / Vaccines", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/biologics/rss.xml"),
    ("FDA Press Releases", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"),
    ("FDA MedWatch", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml"),
]
SEC_FEEDS = [
    ("SEC EDGAR 8-K", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom"),
    ("SEC EDGAR 6-K", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=6-K&company=&dateb=&owner=include&count=100&output=atom"),
    ("SEC EDGAR SC 13D", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC%2013D&company=&dateb=&owner=include&count=100&output=atom"),
]
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# High-severity material events. Clinical phase alone is NOT bullish; the result matters.
POSITIVE_PATTERNS = [
    (r"\b(fda|ema)\s+(approves|approved|authorizes|authorized|clears|cleared)\b", 96, "REGULATORY APPROVAL"),
    (r"\b(positive|successful)\s+(phase\s*(iii|3)|clinical trial|top.?line)\b", 90, "POSITIVE CLINICAL DATA"),
    (r"\b(met|meets|achieved|achieves)\s+(its?\s+)?primary endpoint\b", 92, "CLINICAL ENDPOINT MET"),
    (r"\b(positive top.?line|statistically significant benefit|primary endpoint achieved)\b", 90, "POSITIVE CLINICAL DATA"),
    (r"\b(raises|raised|increases|increased)\s+(full.?year |fy\s*)?(guidance|outlook|forecast)\b", 82, "GUIDANCE RAISE"),
    (r"\b(beats?|beat)\s+(wall street|analyst|earnings|eps|revenue|estimates?)\b", 68, "EARNINGS BEAT"),
    (r"\b(reports|reported) record (revenue|sales|orders|backlog|earnings)\b", 62, "RECORD RESULTS"),
    (r"\b(wins?|awarded|awarded a|selected for)\b.{0,60}\b(contract|award|program)\b", 70, "CONTRACT / AWARD"),
    (r"\b(contract|order)\b.{0,60}\b(awarded|worth \$|valued at \$|selected)\b", 65, "CONTRACT / AWARD"),
    (r"\b(agrees to be acquired|to be acquired|acquired by|buyout offer|takeover offer)\b", 78, "ACQUISITION / TAKEOVER TARGET"),
    (r"\b(activist investor|takes stake|strategic review|exploring strategic alternatives)\b", 38, "CORPORATE ACTION"),
    (r"\b(positive advisory committee vote|panel votes in favor|priority review granted|breakthrough therapy designation)\b", 54, "REGULATORY CATALYST"),
    (r"\b(large order win|commercial launch|launches new product|major product launch)\b", 34, "PRODUCT / COMMERCIAL CATALYST"),
]
NEGATIVE_PATTERNS = [
    (r"\b(phase\s*(iii|3).{0,55}(failed|fails|failure|missed|did not meet|does not meet|fails to meet|didn't meet)|failed.{0,55}(phase\s*(iii|3)|primary endpoint)|fails? to meet (its? )?primary endpoint|did not meet (its? )?primary endpoint|does not meet (its? )?primary endpoint)\b", -98, "CLINICAL FAILURE"),
    (r"\b(fda|ema)\s+(rejects|rejected|refuses|refused)|\bcomplete response letter\b|\bcrl from fda\b", -96, "REGULATORY REJECTION"),
    (r"\bclinical hold\b|\btrial halted\b|\btrial suspended\b|\bserious safety signal\b", -92, "CLINICAL HOLD / SAFETY"),
    (r"\b(cuts|cut|lowers|lowered|reduces|reduced)\s+(full.?year |fy\s*)?(guidance|outlook|forecast)\b", -84, "GUIDANCE CUT"),
    (r"\b(misses?|missed)\s+(wall street|analyst|earnings|eps|revenue|estimates?)\b", -70, "EARNINGS MISS"),
    (r"\b(share offering|secondary offering|public offering|convertible notes|dilutive offering|capital raise)\b", -62, "DILUTION / CAPITAL RAISE"),
    (r"\b(bankruptcy|chapter 11|going concern warning|defaulted on debt)\b", -96, "SOLVENCY RISK"),
    (r"\b(accounting fraud|fraud investigation|sec investigation|criminal investigation)\b", -82, "INVESTIGATION / FRAUD"),
    (r"\b(product recall|safety recall|data breach|cyberattack|production halt|factory shutdown)\b", -63, "OPERATIONAL / SAFETY RISK"),
    (r"\b(lawsuit|antitrust probe|regulatory probe|subpoena|government investigation)\b", -34, "LEGAL / REGULATORY RISK"),
    (r"\b(analyst downgrade|downgraded to sell|price target cut)\b", -22, "ANALYST DOWNGRADE"),
]

AMBIGUOUS_SHORT_TICKERS = {
    "AI", "ON", "ARM", "NET", "PATH", "SO", "C", "A", "T", "X", "F", "GAP", "ALL", "LOVE", "REAL", "GOOD", "LIFE", "FAST", "RUN", "PLAY", "OPEN", "KEY", "PLUG", "BEAT", "ALLY", "PAY", "DAY", "YOU", "NOW", "MOON", "WAVE", "WORK", "NOTE", "SNAP", "BOX", "APP", "BOWL", "BIRD", "CAVA", "LEAP"
}
LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "holdings", "holding", "group", "class", "common", "shares", "ordinary", "lp", "llc", "sa", "ag", "nv", "se", "the"
}
GENERIC_ALIAS_WORDS = {
    "technology", "technologies", "systems", "international", "global", "health", "healthcare", "financial", "finance", "energy", "services", "solutions", "industries", "industrial", "industrials", "pharmaceutical", "pharmaceuticals", "biotechnology", "bio", "capital", "partners", "resources", "software", "media", "communications", "electronics", "holdings", "company", "group", "incorporated", "corporation"
}


def http_get(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/json, application/xml, text/xml, */*;q=0.8",
    })
    last_exc = None
    for delay in (0.0, 0.5):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_exc = exc
    raise last_exc or RuntimeError("HTTP request failed")


def safe_float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def request_json(url: str, timeout: int = 12):
    return json.loads(http_get(url, timeout=timeout).decode("utf-8", errors="replace"))


def local_dt(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, tz=UTC).astimezone(NY_TZ)


def parse_pubdate(value: str | None):
    if not value:
        return None
    raw = html.unescape(str(value)).strip()
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        pass
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return None


def scan_lookback_days(now_et: dt.datetime) -> int:
    # Monday pre-market needs to include Friday after-close / weekend releases.
    return 3 if now_et.weekday() == 0 else 2


def allowed_age_hours(now_utc: dt.datetime, published: str | None) -> float | None:
    parsed = parse_pubdate(published)
    if not parsed:
        return None
    age = (now_utc - parsed).total_seconds() / 3600.0
    if age < -1.0:  # future-dated or timezone-corrupt items must not rank
        return None
    now_et = now_utc.astimezone(NY_TZ)
    max_hours = 84 if now_et.weekday() == 0 else 42
    if now_et.weekday() == 1 and now_et.hour < 10:
        max_hours = 66
    if age > max_hours:
        return None
    return max(0.0, age)


def recency_weight(age_hours: float | None) -> float:
    if age_hours is None:
        return 0.0
    if age_hours <= 3:
        return 1.0
    if age_hours <= 8:
        return 0.92
    if age_hours <= 16:
        return 0.82
    if age_hours <= 24:
        return 0.72
    if age_hours <= 42:
        return 0.58
    return 0.42


def parse_feed(url: str, source: str, limit: int = 35):
    try:
        root = XML.fromstring(http_get(url, timeout=12))
    except Exception as exc:
        print(f"Feed unavailable ({source}): {type(exc).__name__}")
        return []
    nodes = root.findall(".//item")
    if not nodes:
        nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "entry"]
    out = []
    for node in nodes[:limit]:
        fields = {}
        for child in list(node):
            tag = child.tag.rsplit("}", 1)[-1].lower()
            fields.setdefault(tag, child)
        title_node = fields.get("title")
        title = "" if title_node is None else " ".join("".join(title_node.itertext()).split())
        link = ""
        if "link" in fields:
            link_node = fields["link"]
            link = link_node.attrib.get("href", "") or (link_node.text or "")
        published = ""
        for key in ("pubdate", "published", "updated", "date"):
            if key in fields and (fields[key].text or "").strip():
                published = (fields[key].text or "").strip()
                break
        description = ""
        for key in ("description", "summary", "content", "subtitle"):
            if key in fields and fields[key] is not None:
                description = " ".join("".join(fields[key].itertext()).split())[:1500]
                if description:
                    break
        source_name = source
        if "source" in fields and fields["source"] is not None:
            maybe_source = " ".join("".join(fields["source"].itertext()).split())
            if maybe_source:
                source_name = maybe_source
        title = html.unescape(title)
        description = html.unescape(re.sub(r"<[^>]+>", " ", description))
        if title:
            out.append({"title": title, "url": link, "published": published,
                        "source": source_name, "description": description})
    return out


def google_news(query: str, edition=("en-US", "US", "US:en"), lookback_days=2, limit=10):
    hl, gl, ceid = edition
    q = f"{query} when:{lookback_days}d"
    params = urllib.parse.urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid})
    url = f"https://news.google.com/rss/search?{params}"
    return parse_feed(url, f"Google News {gl}", limit=limit)


def yahoo_chart(symbol: str):
    params = urllib.parse.urlencode({"range": "6mo", "interval": "1d", "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"
    raw = request_json(url)
    result = raw.get("chart", {}).get("result", [None])[0]
    if not result:
        raise RuntimeError("Yahoo daily chart missing")
    timestamps = result.get("timestamp") or []
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    bars = []
    opens, highs, lows, closes, volumes = [quote.get(k, []) for k in ("open", "high", "low", "close", "volume")]
    scan_date = dt.datetime.now(NY_TZ).date()
    for i, stamp in enumerate(timestamps):
        try:
            # Keep only fully completed prior sessions; don't use a partial current-day bar.
            if local_dt(int(stamp)).date() >= scan_date:
                continue
            o, h, l, c = [safe_float(vals[i]) for vals in (opens, highs, lows, closes)]
            v = safe_float(volumes[i]) or 0.0
            if None not in (o, h, l, c):
                bars.append({"t": int(stamp), "o": o, "h": h, "l": l, "c": c, "v": v})
        except Exception:
            continue
    return bars, result.get("meta", {})


def yahoo_intraday(symbol: str):
    params = urllib.parse.urlencode({"range": "5d", "interval": "5m", "includePrePost": "true", "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"
    raw = request_json(url)
    result = raw.get("chart", {}).get("result", [None])[0]
    if not result:
        return [], {}
    timestamps = result.get("timestamp") or []
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    bars = []
    opens, highs, lows, closes, volumes = [quote.get(k, []) for k in ("open", "high", "low", "close", "volume")]
    for i, stamp in enumerate(timestamps):
        try:
            values = [safe_float(arr[i]) for arr in (opens, highs, lows, closes)]
            volume = safe_float(volumes[i]) or 0.0
            if None not in values:
                bars.append({"t": int(stamp), "o": values[0], "h": values[1], "l": values[2], "c": values[3], "v": volume})
        except Exception:
            continue
    return bars, result.get("meta", {})


def yahoo_news(symbol: str, limit=12):
    query = urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"})
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?{query}"
    items = parse_feed(url, f"Yahoo Finance ({symbol})", limit=limit)
    for item in items:
        # The endpoint is requested for one ticker, so preserve this scope even if
        # the RSS item has a publisher tag (e.g. Reuters) as its source label.
        item["publisherSource"] = item.get("source", "Yahoo Finance")
        item["source"] = f"Yahoo Finance ({symbol})"
        item["tickerScoped"] = True
    return items


def rsi(closes, period=14):
    if len(closes) <= period:
        return None
    deltas = [b - a for a, b in zip(closes[-period - 1:-1], closes[-period:])]
    gains = [max(x, 0.0) for x in deltas]
    losses = [max(-x, 0.0) for x in deltas]
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def ema(closes, period):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    current = sum(closes[:period]) / period
    for price in closes[period:]:
        current = price * k + current * (1.0 - k)
    return current


def atr_daily(bars, period=14):
    if len(bars) < period + 1:
        return None
    tr = []
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        tr.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    return sum(tr) / len(tr)


def atr_intraday(bars, period=14):
    if len(bars) < period + 1:
        return None
    tr = []
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        tr.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    return sum(tr) / len(tr)


def normalize_text(value: str) -> str:
    value = html.unescape(value or "").lower()
    value = re.sub(r"&amp;|&", " and ", value)
    value = re.sub(r"[^a-z0-9$]+", " ", value)
    return " ".join(value.split())


def fetch_sec_ticker_map():
    """Public SEC company/ticker map; use only for discovering issuer names, with fallback on failure."""
    try:
        raw = request_json(SEC_TICKER_MAP_URL, timeout=15)
        values = raw.values() if isinstance(raw, dict) else []
        mapping = {}
        for row in values:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker", "")).upper().strip()
            title = str(row.get("title", "")).strip()
            if re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", ticker) and title:
                mapping[ticker] = title
        print(f"SEC ticker/name map: {len(mapping)} companies")
        return mapping
    except Exception as exc:
        print(f"SEC ticker map unavailable; using watchlist only ({type(exc).__name__})")
        return {}


def clean_company_name(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    while words and words[-1].lower() in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def company_aliases(name: str):
    clean = clean_company_name(name)
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", clean)]
    aliases = set()
    if clean and len(clean) >= 5:
        aliases.add(normalize_text(clean))
    # Single distinctive words help match headlines such as "Moderna shares jump".
    for word in words:
        if len(word) >= 6 and word not in GENERIC_ALIAS_WORDS:
            aliases.add(word)
        elif word in {"meta", "uber", "lyft", "roku", "visa", "sony", "nokia", "dell", "nike", "tesla", "moderna"}:
            aliases.add(word)
    # Proper two-word phrases for names such as Walt Disney or Eli Lilly.
    for i in range(len(words) - 1):
        phrase = " ".join(words[i:i + 2])
        if all(len(w) >= 3 and w not in GENERIC_ALIAS_WORDS for w in words[i:i + 2]):
            aliases.add(phrase)
    return {a for a in aliases if len(a) >= 4}


def build_alias_index(ticker_names: dict[str, str]):
    index: dict[str, set[str]] = {}
    for ticker, name in ticker_names.items():
        for alias in company_aliases(name):
            index.setdefault(alias, set()).add(ticker)
    # Longest aliases are checked first to reduce noisy overlapping matches.
    return sorted(index.items(), key=lambda item: len(item[0]), reverse=True)


def discover_global_news(now_et: dt.datetime):
    lookback = scan_lookback_days(now_et)
    collected = []
    tasks = []
    for query in GLOBAL_EVENT_QUERIES:
        for edition in GOOGLE_EDITIONS:
            tasks.append((query, edition, lookback))
    for hl, gl, ceid, query in LOCAL_EVENT_QUERIES:
        tasks.append((query, (hl, gl, ceid), lookback))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(google_news, query, edition, days, 10) for query, edition, days in tasks]
        for future in concurrent.futures.as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception:
                pass

    # High-signal primary-source feeds, when reachable.
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futures = [pool.submit(parse_feed, url, name, 40) for name, url in (FDA_FEEDS + SEC_FEEDS)]
        for future in concurrent.futures.as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception:
                pass

    return dedupe_news([item for item in collected if allowed_age_hours(dt.datetime.now(UTC), item.get("published")) is not None])


def dedupe_news(items):
    unique = {}
    for item in items:
        title = normalize_text(item.get("title", ""))
        if not title:
            continue
        # News aggregators often syndicate the exact same story; don't score copies repeatedly.
        key = re.sub(r"\b(reuters|associated press|ap news|marketwatch|cnbc|yahoo finance|benzinga|bloomberg)\b", "", title).strip()
        existing = unique.get(key)
        if existing is None:
            unique[key] = dict(item)
        else:
            sources = set(existing.get("sources", [existing.get("source", "source")]))
            sources.add(item.get("source", "source"))
            existing["sources"] = sorted(sources)
            existing_age = parse_pubdate(existing.get("published"))
            new_age = parse_pubdate(item.get("published"))
            if new_age and (not existing_age or new_age > existing_age):
                existing["published"] = item.get("published")
            if not existing.get("description") and item.get("description"):
                existing["description"] = item["description"]
            if not existing.get("url") and item.get("url"):
                existing["url"] = item["url"]
    return list(unique.values())


def event_classifier(title: str, description: str = ""):
    text = f"{title} {description}".lower()
    # Negative material events must override generic words such as "phase 3" or "FDA".
    for pattern, score, label in NEGATIVE_PATTERNS:
        if re.search(pattern, text, re.I):
            return score, label, True
    # Clinical news needs a reported positive outcome, not merely "Phase 3 started".
    if re.search(r"\bphase\s*(iii|3)\b.{0,100}\b(positive|successful|met|meets|achieved|achieves|statistically significant|primary endpoint)\b", text, re.I) or re.search(r"\b(positive|successful|statistically significant)\b.{0,100}\bphase\s*(iii|3)\b", text, re.I):
        return 90, "POSITIVE CLINICAL DATA", True
    for pattern, score, label in POSITIVE_PATTERNS:
        if re.search(pattern, text, re.I):
            # A clinical stage announcement alone isn't a positive readout.
            if label == "POSITIVE CLINICAL DATA" and not re.search(r"\b(positive|successful|met|meets|achieved|achieves|statistically significant)\b", text):
                continue
            return score, label, True
    if re.search(r"\b(phase\s*(iii|3)|clinical trial|top.?line results|pdufa|adcom|advisory committee)\b", text, re.I):
        return 15, "CLINICAL / REGULATORY WATCH", False
    if re.search(r"\b(earnings|quarterly results|revenue results|guidance|forecast)\b", text, re.I):
        return 18, "EARNINGS WATCH", False
    if re.search(r"\b(merger|acquisition|acquires|to acquire|joint venture)\b", text, re.I):
        return 18, "M&A / STRATEGIC WATCH", False
    if re.search(r"\b(contract|award|partnership|supply agreement)\b", text, re.I):
        return 20, "COMMERCIAL WATCH", False
    return 0, "GENERAL NEWS", False


def generic_event_strength(item):
    score, _, material = event_classifier(item.get("title", ""), item.get("description", ""))
    if material:
        return abs(score)
    low = (item.get("title", "") + " " + item.get("description", "")).lower()
    if any(term in low for term in ("stock", "shares", "company", "earnings", "guidance", "trial", "contract", "fda")):
        return 12
    return 0


def symbol_mentions(text: str, ticker_names: dict[str, str], alias_index):
    normalized = normalize_text(text)
    padded = f" {normalized} "
    found: dict[str, float] = {}
    # Ticker mentions with explicit market markers or $TICKER are strong evidence.
    for match in re.finditer(r"\$([A-Z]{1,5})\b|\b(?:NASDAQ|NYSE|AMEX|NASDAQGS|NASDAQGM|NYSEAMERICAN)\s*[:/-]\s*([A-Z]{1,5})\b", text, re.I):
        ticker = (match.group(1) or match.group(2) or "").upper()
        if ticker in ticker_names:
            found[ticker] = max(found.get(ticker, 0), 1.4)
    # Known watchlist ticker in plain uppercase, except highly ambiguous short symbols.
    uppercase_tickers = set(re.findall(r"\b[A-Z]{1,5}\b", text))
    for ticker in uppercase_tickers:
        if ticker not in ticker_names:
            continue
        if ticker in AMBIGUOUS_SHORT_TICKERS and len(ticker) <= 3:
            continue
        if len(ticker) >= 2:
            found[ticker] = max(found.get(ticker, 0), 1.05 if len(ticker) >= 4 else 0.75)
    # SEC company-name aliases can discover listed issuers not in the hard-coded watchlist.
    # Run this on eventful headlines only; aliases are matched as whole normalized phrases.
    for alias, tickers in alias_index:
        if f" {alias} " not in padded:
            continue
        for ticker in tickers:
            found[ticker] = max(found.get(ticker, 0), 1.25 if len(alias.split()) > 1 else 0.95)
    return found


def headline_age_score(item, now_utc):
    age = allowed_age_hours(now_utc, item.get("published"))
    return age, recency_weight(age)


def relevant_news_for_ticker(ticker, company_name, global_items, ticker_names):
    aliases = company_aliases(company_name)
    words = set(normalize_text(company_name).split())
    words -= LEGAL_SUFFIXES
    items = []
    for raw in global_items:
        text = normalize_text(raw.get("title", "") + " " + raw.get("description", ""))
        if ticker.lower() in AMBIGUOUS_SHORT_TICKERS and len(ticker) <= 3:
            ticker_hit = bool(re.search(rf"\${re.escape(ticker)}\b|\b(?:nasdaq|nyse|amex)\s*[:/-]\s*{re.escape(ticker.lower())}\b", text, re.I))
        else:
            ticker_hit = bool(re.search(rf"\b{re.escape(ticker.lower())}\b", text))
        company_hit = any(f" {alias} " in f" {text} " for alias in aliases)
        if ticker_hit or company_hit:
            item = dict(raw)
            item["matchedToTicker"] = True
            item["scope"] = "global headline match"
            items.append(item)
    return items


def score_news(items, ticker, company_name, now_utc):
    enriched = []
    for raw in dedupe_news(items):
        age, freshness = headline_age_score(raw, now_utc)
        if age is None or freshness <= 0:
            continue
        # Yahoo ticker RSS is already scoped to the requested symbol; global sources must match name/ticker.
        scoped_yahoo = bool(raw.get("tickerScoped")) or str(raw.get("source", "")).startswith("Yahoo Finance (")
        is_matched = bool(raw.get("matchedToTicker")) or scoped_yahoo
        if not is_matched:
            continue
        score, kind, material = event_classifier(raw.get("title", ""), raw.get("description", ""))
        source = raw.get("source", "News")
        if source.startswith("SEC EDGAR") or source.startswith("FDA"):
            score = max(-100, min(100, score * 1.12))
        weighted = score * freshness
        item = dict(raw)
        item.update({"eventType": kind, "impactScore": round(weighted, 1), "ageHours": round(age, 1), "materialEvent": material})
        enriched.append(item)

    enriched.sort(key=lambda x: abs(x.get("impactScore", 0)), reverse=True)
    positives = [x for x in enriched if x.get("impactScore", 0) > 0]
    negatives = [x for x in enriched if x.get("impactScore", 0) < 0]
    best_positive = max((x["impactScore"] for x in positives), default=0.0)
    best_negative = max((-x["impactScore"] for x in negatives), default=0.0)
    # One lead story dominates; independent additional positive news adds only modest corroboration.
    other_positive = sorted((x["impactScore"] for x in positives if x["impactScore"] < best_positive), reverse=True)
    corroboration = min(14.0, sum(other_positive[:2]) * 0.12)
    # A material adverse item should overpower weaker positive chatter.
    net_score = best_positive + corroboration - best_negative * (1.25 if best_negative >= 55 else 0.8)
    net_score = max(-100.0, min(100.0, net_score))
    strongest_positive = next((x for x in positives if x.get("materialEvent") and x.get("impactScore", 0) >= 35), None)
    note = (
        f"{len(enriched)} fresh matched headlines; lead={enriched[0]['eventType']}"
        if enriched else "No verified fresh company-specific headline matched"
    )
    return round(net_score, 1), note, enriched[:10], strongest_positive


def technical_metrics(bars):
    if len(bars) < 55:
        return None
    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    price = closes[-1]
    e20, e50, r, a = ema(closes, 20), ema(closes, 50), rsi(closes, 14), atr_daily(bars, 14)
    ch5 = (price / closes[-6] - 1.0) * 100.0 if closes[-6] else 0.0
    ch20 = (price / closes[-21] - 1.0) * 100.0 if closes[-21] else 0.0
    avgvol = sum(volumes[-21:-1]) / 20.0 if len(volumes) >= 21 else 0.0
    relvol = volumes[-1] / avgvol if avgvol else 0.0
    high20 = max(b["h"] for b in bars[-20:])
    low20 = min(b["l"] for b in bars[-20:])
    volatility_pct = a / price * 100.0 if a and price else 0.0
    tech = 0.0
    tech += 8 if e20 and price > e20 else 0
    tech += 5 if e50 and price > e50 else 0
    tech += 7 if r is not None and 48 <= r <= 75 else 0
    tech += 7 if ch5 > 0 else 0
    tech += 6 if ch20 > 0 else 0
    tech += 6 if relvol >= 1.0 else 0
    tech += min(5, max(0, volatility_pct))
    return {
        "previousClose": price,
        "ema20": e20, "ema50": e50, "rsi": r, "atr": a,
        "change5d": ch5, "change20d": ch20, "relVolume": relvol,
        "high20": high20, "low20": low20, "volatilityPct": volatility_pct,
        "avgVolume20": avgvol, "technicalScore": min(50.0, tech),
    }


def premarket_metrics(intraday_bars, now_et):
    today = now_et.date()
    pm = []
    for bar in intraday_bars:
        local = local_dt(bar["t"])
        if local.date() == today and dt.time(4, 0) <= local.time() < dt.time(9, 30):
            pm.append(bar)
    pm.sort(key=lambda x: x["t"])
    if len(pm) < 3:
        return {"available": False, "bars": pm, "reason": "No reliable current-session premarket bars"}
    pm_open = pm[0]["o"]
    latest = pm[-1]["c"]
    high = max(x["h"] for x in pm)
    low = min(x["l"] for x in pm)
    volume = sum(x["v"] for x in pm)
    dollar_volume = sum(x["v"] * x["c"] for x in pm)
    pm_atr = atr_intraday(pm, 14) or None
    return {
        "available": True, "bars": pm, "open": pm_open, "last": latest,
        "high": high, "low": low, "volume": volume, "dollarVolume": dollar_volume,
        "changePct": None, "pmAtr": pm_atr,
        "lastTimestamp": dt.datetime.fromtimestamp(pm[-1]["t"], UTC).isoformat(),
        "barsCount": len(pm),
    }


def build_intraday_levels(pm, daily_atr, prior_close):
    if not pm.get("available"):
        return None
    price = pm["last"]
    atr5 = pm.get("pmAtr") or (daily_atr / 78.0 if daily_atr else price * 0.0025)
    atr5 = max(atr5, price * 0.0008)
    # Conditional long entry: a break above premarket high, not a blind market buy.
    entry = pm["high"] + max(price * 0.0005, atr5 * 0.08)
    stop = min(entry - 1.25 * atr5, pm["low"] - 0.08 * atr5)
    risk = entry - stop
    if not all(math.isfinite(x) for x in (entry, stop, risk)) or risk <= 0 or entry <= 0:
        return None
    tp1 = entry + 1.5 * risk
    tp2 = entry + 2.2 * risk
    risk_pct = risk / entry * 100.0
    rr = (tp2 - entry) / risk
    return {
        "entry": round(entry, 4), "stop": round(stop, 4),
        "tp1": round(tp1, 4), "tp2": round(tp2, 4),
        "rr": round(rr, 2), "riskPct": round(risk_pct, 2),
        "entryLogic": "Only consider a 5-minute breakout and hold above the premarket high after the regular session opens; do not enter premarket solely from this level.",
        "stopLogic": "Below the premarket low / 5-minute volatility buffer; gaps and slippage can exceed this level.",
        "tp1Logic": "1.5R intraday target.",
        "tp2Logic": "2.2R intraday target; close any remaining position before 15:55 ET.",
    }


def event_direction_ok(news_score, lead_event):
    return news_score >= 38 and bool(lead_event) and lead_event.get("impactScore", 0) >= 32


def premarket_score(pm, prior_close, avg_volume):
    if not pm.get("available") or not prior_close:
        return 0.0
    gap = (pm["last"] / prior_close - 1.0) * 100.0
    score = 0.0
    if 1.0 <= gap <= 12.0:
        score += 22.0
    elif 0.5 <= gap < 1.0:
        score += 11.0
    elif 12.0 < gap <= 18.0:
        score += 8.0
    elif gap > 18.0:
        score -= 15.0
    elif gap < -1.0:
        score -= 15.0
    if pm.get("last", 0) >= pm.get("open", 0):
        score += 6.0
    high = pm.get("high") or pm["last"]
    distance_to_high = (pm["last"] / high - 1.0) * 100.0 if high else -100
    if distance_to_high >= -1.25:
        score += 7.0
    dollars = pm.get("dollarVolume", 0.0)
    if dollars >= 1_000_000:
        score += 15.0
    elif dollars >= 300_000:
        score += 12.0
    elif dollars >= 100_000:
        score += 8.0
    elif dollars >= 50_000:
        score += 4.0
    if avg_volume and pm.get("volume", 0.0) / avg_volume >= 0.03:
        score += 5.0
    return max(0.0, min(50.0, score))


def make_candidate(ticker, company_name, daily_bars, global_headlines, now_utc, now_et, yahoo_items, market_meta, intraday_bars=None):
    metrics = technical_metrics(daily_bars)
    if not metrics:
        return None
    matched_global = relevant_news_for_ticker(ticker, company_name, global_headlines, {})
    merged_news = dedupe_news(matched_global + yahoo_items)
    news_score, news_note, ranked_news, lead_event = score_news(merged_news, ticker, company_name, now_utc)
    pm = premarket_metrics(intraday_bars or [], now_et)
    previous_close = metrics["previousClose"]
    if pm.get("available"):
        pm["changePct"] = (pm["last"] / previous_close - 1.0) * 100.0 if previous_close else None
        price = pm["last"]
    else:
        price = previous_close
    pm_score = premarket_score(pm, previous_close, metrics["avgVolume20"])
    tech = metrics["technicalScore"]
    # Catalyst is the primary driver; price reaction is the day-trade confirmation.
    score = max(0.0, min(100.0, max(0.0, news_score) * 0.58 + pm_score * 0.27 + tech * 0.30))
    if news_score < 0:
        score = max(0.0, score + news_score * 0.35)

    levels = build_intraday_levels(pm, metrics["atr"], previous_close)
    scan_before_open = dt.time(4, 0) <= now_et.time() < dt.time(9, 30) and now_et.weekday() < 5
    avgvol = metrics["avgVolume20"] or 0
    pm_gap = pm.get("changePct") if pm.get("available") else None
    status = "NO TRADE"
    status_reason = ""
    if not scan_before_open:
        status_reason = "This scan did not run during the current premarket; wait for the next scheduled pre-open scan."
    elif not event_direction_ok(news_score, lead_event):
        status_reason = "No strong, fresh, positive company-specific catalyst verified; technical strength alone is not a day-trade signal."
    elif not pm.get("available"):
        status_reason = "No reliable current-session premarket bars; entry levels are unavailable."
    elif pm_gap is None or pm_gap < 0.5:
        status_reason = "Positive headline lacks a meaningful positive premarket reaction; wait for confirmation or stand aside."
    elif pm_gap > 18.0:
        status_reason = "Premarket gap is extremely extended; chase risk is high. No long entry suggested."
    elif pm.get("dollarVolume", 0) < 100_000:
        status_reason = "Premarket dollar volume is too thin for a robust intraday setup."
    elif avgvol < 400_000:
        status_reason = "Average daily share volume is too low for this scanner's liquidity filter."
    elif not levels:
        status_reason = "Could not compute valid premarket breakout levels."
    elif levels["riskPct"] > 3.5:
        status_reason = f"Modeled stop distance is too wide ({levels['riskPct']:.1f}%); risk/reward is not suitable for the intraday filter."
    elif pm_gap < 1.0:
        status_reason = "Catalyst detected, but the premarket price reaction is weak; watch only until the opening range confirms."
    else:
        status = "QUALIFIED — INTRADAY LONG WATCH"
        status_reason = "Fresh positive catalyst + positive premarket reaction + liquidity/risk filters. Entry remains conditional on a confirmed breakout after the open."

    # No live premarket quote => don't print fictitious numerical entry levels.
    if not pm.get("available"):
        levels_out = {"entry": None, "stop": None, "tp1": None, "tp2": None, "rr": None,
                      "entryLogic": "Unavailable: no valid premarket price/range returned.",
                      "stopLogic": "No order until current-session data is available.",
                      "tp1Logic": "Unavailable", "tp2Logic": "Unavailable"}
    else:
        levels_out = levels or {"entry": None, "stop": None, "tp1": None, "tp2": None, "rr": None,
                                "entryLogic": "No valid entry", "stopLogic": "No valid stop", "tp1Logic": "Unavailable", "tp2Logic": "Unavailable"}

    top_news = ranked_news[:8]
    catalyst_line = "No strong positive catalyst"
    if lead_event:
        catalyst_line = f"{lead_event['eventType']}: {lead_event['title']}"
    pm_line = f"Premarket {pm_gap:+.2f}%" if pm_gap is not None else "Premarket quote unavailable"
    why_now = f"{catalyst_line} | {pm_line} | {status}"
    if not pm.get("available"):
        setup = "NO TRADE — PREMARKET DATA MISSING"
    elif status.startswith("QUALIFIED"):
        setup = "INTRADAY LONG — PM HIGH BREAKOUT"
    elif event_direction_ok(news_score, lead_event):
        setup = "WATCH ONLY — WAIT FOR OPEN"
    else:
        setup = "NO TRADE — NO STRONG CATALYST"

    # This is evidence-quality scoring, not probability of profit.
    confidence = 42.0
    if lead_event:
        confidence += 15.0 if lead_event.get("source", "").startswith(("SEC EDGAR", "FDA")) else 10.0
        confidence += min(14.0, max(0.0, lead_event.get("impactScore", 0)) * 0.12)
    if pm.get("available"):
        confidence += 8.0
    if len({n.get("source", "") for n in top_news}) >= 2:
        confidence += 5.0
    confidence = max(35.0, min(82.0, confidence))

    risks = [
        "These are one-session scenarios; close any open position before 15:55 ET and never carry the trade overnight.",
        "A premarket gap can reverse at the open; wait for a 5-minute breakout/hold, not just the headline.",
        "Public free quotes may be delayed/incomplete; gaps, spread and slippage can exceed the modeled stop.",
    ]
    if news_score < 0:
        risks.insert(0, "The latest matched news mix is net adverse; do not treat this symbol as a long trade.")
    if pm.get("available") and pm.get("dollarVolume", 0) < 100_000:
        risks.insert(0, "Premarket trading appears thin; price levels may be unreliable.")

    return {
        # Fields consumed by the existing static frontend are preserved.
        "ticker": ticker,
        "name": company_name or market_meta.get("longName") or market_meta.get("shortName") or ticker,
        "price": round(price, 4) if price is not None else None,
        "change5d": round(metrics["change5d"], 2),
        "change20d": round(metrics["change20d"], 2),
        "rsi": round(metrics["rsi"], 1) if metrics["rsi"] is not None else None,
        "ema20": round(metrics["ema20"], 4) if metrics["ema20"] is not None else None,
        "ema50": round(metrics["ema50"], 4) if metrics["ema50"] is not None else None,
        "atr": round(metrics["atr"] or 0, 4),
        "relVolume": round(metrics["relVolume"], 2),
        "volatilityPct": round(metrics["volatilityPct"], 2),
        "high20": round(metrics["high20"], 4),
        "low20": round(metrics["low20"], 4),
        "newsScore": round(news_score, 1),
        "technicalScore": round(tech, 1),
        "premarketScore": round(pm_score, 1),
        "score": round(score, 1),
        "setup": setup,
        "tradeStatus": status,
        "tradeStatusReason": status_reason,
        "entry": levels_out.get("entry"),
        "stop": levels_out.get("stop"),
        "tp1": levels_out.get("tp1"),
        "tp2": levels_out.get("tp2"),
        "rr": levels_out.get("rr"),
        "entryLogic": levels_out.get("entryLogic"),
        "stopLogic": levels_out.get("stopLogic"),
        "tp1Logic": levels_out.get("tp1Logic"),
        "tp2Logic": levels_out.get("tp2Logic"),
        "confidence": round(confidence),
        "confidenceNote": "Quality/recency/corroboration of evidence only; not the probability of a profitable trade.",
        "whyNow": why_now,
        "thesis": (
            f"Catalyst-first intraday screen. {status_reason} The setup is conditional and should be rechecked against "
            f"the opening range and live liquidity; a news headline alone does not imply the share price must rise."
        ),
        "catalysts": [n.get("title", "") for n in top_news[:4]],
        "risks": risks,
        "invalidate": (
            f"Cancel the setup if price fails to break/hold above the premarket high, loses the modeled stop (${levels_out['stop']:.4f}) after entry, "
            "or the catalyst is contradicted. Exit all intraday positions before 15:55 ET."
            if levels_out.get("stop") is not None else "No trade: wait for a fresh premarket quote and a verified catalyst before defining an entry."
        ),
        "news": [{k: n.get(k) for k in ("title", "url", "published", "source", "eventType", "impactScore", "ageHours", "materialEvent")} for n in top_news],
        "source": "Google News RSS (regional editions) + Yahoo Finance public RSS/chart + FDA RSS + SEC EDGAR current filings",
        "newsNote": news_note,
        "premarket": {
            "available": bool(pm.get("available")),
            "last": round(pm["last"], 4) if pm.get("available") else None,
            "open": round(pm["open"], 4) if pm.get("available") else None,
            "high": round(pm["high"], 4) if pm.get("available") else None,
            "low": round(pm["low"], 4) if pm.get("available") else None,
            "changePct": round(pm_gap, 2) if pm_gap is not None else None,
            "volume": int(pm.get("volume", 0)) if pm.get("available") else None,
            "dollarVolume": round(pm.get("dollarVolume", 0), 0) if pm.get("available") else None,
            "bars": int(pm.get("barsCount", 0)) if pm.get("available") else 0,
            "lastTimestamp": pm.get("lastTimestamp"),
        },
        "previousClose": round(previous_close, 4),
        "sessionPlan": "DAY TRADE ONLY — conditional entry after opening bell; flatten before 15:55 America/New_York.",
    }


def ticker_discovery_score(headline):
    score, label, material = event_classifier(headline.get("title", ""), headline.get("description", ""))
    return abs(score) if material else generic_event_strength(headline)


def make_name_map(sec_map, static_tickers):
    names = dict(sec_map)
    for symbol in static_tickers:
        names.setdefault(symbol, symbol)
    return names


def discover_tickers_from_headlines(headlines, ticker_names, alias_index):
    counts = {}
    max_signal = {}
    now_utc = dt.datetime.now(UTC)
    for item in headlines:
        age, _ = headline_age_score(item, now_utc)
        if age is None or ticker_discovery_score(item) < 12:
            continue
        combined = f"{item.get('title', '')} {item.get('description', '')}"
        found = symbol_mentions(combined, ticker_names, alias_index)
        for ticker, relevance in found.items():
            counts[ticker] = counts.get(ticker, 0) + relevance
            max_signal[ticker] = max(max_signal.get(ticker, 0), ticker_discovery_score(item) * relevance)
    dynamic = [t for t in counts if t not in UNIVERSE]
    dynamic.sort(key=lambda t: (max_signal.get(t, 0), counts[t]), reverse=True)
    return dynamic[:35], max_signal


def fetch_markets(tickers):
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
        futures = {pool.submit(yahoo_chart, symbol): symbol for symbol in tickers}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                bars, meta = future.result()
                if len(bars) >= 55:
                    result[ticker] = (bars, meta)
            except Exception:
                continue
    return result


def fetch_yahoo_news_bulk(tickers):
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
        futures = {pool.submit(yahoo_news, ticker, 12): ticker for ticker in tickers}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                result[ticker] = future.result()
            except Exception:
                result[ticker] = []
    return result


def fetch_intraday_bulk(tickers):
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(yahoo_intraday, ticker): ticker for ticker in tickers}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                bars, meta = future.result()
                result[ticker] = bars
            except Exception:
                result[ticker] = []
    return result


def choose_five(candidates):
    # Only high-quality trades first; remaining slots are explicitly WATCH/NO TRADE.
    ranked = sorted(candidates, key=lambda x: (
        x.get("tradeStatus", "").startswith("QUALIFIED"),
        x.get("newsScore", 0), x.get("score", 0), x.get("premarketScore", 0)
    ), reverse=True)
    picks = []
    chosen = set()
    for item in ranked:
        if item["ticker"] in chosen:
            continue
        # Cap obvious sector concentration in first pass.
        cluster = item.get("sectorCluster", "")
        if cluster and sum(1 for x in picks if x.get("sectorCluster") == cluster) >= 2:
            continue
        picks.append(item)
        chosen.add(item["ticker"])
        if len(picks) == 5:
            return picks
    for item in ranked:
        if item["ticker"] not in chosen:
            picks.append(item)
            chosen.add(item["ticker"])
            if len(picks) == 5:
                break
    return picks


def sector_cluster(ticker):
    groups = {
        "semiconductors": set("NVDA AMD AVGO TSM ASML ARM SMCI MU MRVL QCOM TXN INTC AMAT LRCX KLAC ON MCHP WDC STX".split()),
        "software/AI": set("PLTR CRWD PANW DDOG NET SNOW MDB APP PATH AI SOUN BBAI IONQ RGTI QBTS TEM ORCL CRM NOW ADBE".split()),
        "biotech/pharma": set("MRNA BNTX PFE LLY NVO ABBV REGN AMGN GILD VRTX BIIB ALNY SRPT CRSP NTLA BEAM EDIT VKTX ALT SANA IOVA IMVT RXRX RARE ARWR INSM EXAS CORT AXSM NBIX LQDA APLS CYTK PRTA SAVA JNJ MRK BMY AZN NVS SNY GSK".split()),
        "space/defense": set("RKLB ASTS LUNR SPCE ACHR JOBY AUR RTX LMT NOC BA".split()),
        "crypto/fintech": set("HOOD COIN MSTR MARA RIOT CLSK AFRM UPST SOFI NU SQ XYZ PYPL".split()),
        "EV/mobility": set("TSLA RIVN LCID NIO XPEV CVNA UBER DASH LYFT".split()),
        "energy/materials": set("XOM CVX OXY SLB OKLO VST GEV ENPH FSLR FCX AA NEM UUUU CCJ".split()),
    }
    for label, tickers in groups.items():
        if ticker in tickers:
            return label
    return "other"


def load_history():
    try:
        parsed = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    now_utc = dt.datetime.now(UTC)
    now_et = now_utc.astimezone(NY_TZ)
    scan_date = now_et.date().isoformat()
    lookback_days = scan_lookback_days(now_et)
    print(f"AlphaEdge premarket day-trade scan: {now_et.isoformat()}")
    print(f"Market session: {now_et.strftime('%A %H:%M %Z')} | lookback={lookback_days}d")

    # 1) Discover catalysts from broad international news queries and official feeds.
    global_headlines = discover_global_news(now_et)
    print(f"Fresh world/event headlines: {len(global_headlines)}")

    # 2) Download public issuer/ticker map and discover listed companies mentioned in headlines.
    sec_map = fetch_sec_ticker_map()
    ticker_names = make_name_map(sec_map, UNIVERSE)
    alias_index = build_alias_index(ticker_names)
    dynamic_tickers, dynamic_signal = discover_tickers_from_headlines(global_headlines, ticker_names, alias_index)
    symbols = list(dict.fromkeys(UNIVERSE + dynamic_tickers))
    print(f"Symbols considered: {len(symbols)} (dynamic discoveries: {len(dynamic_tickers)})")

    # 3) Fetch recent daily OHLCV. It is only used for context/liquidity, not standalone buy signals.
    markets = fetch_markets(symbols)
    print(f"Daily market histories received: {len(markets)}")
    if not markets:
        raise RuntimeError("No market data returned; refusing to overwrite the previous picks file.")

    # 4) Per-symbol public news RSS. Bulk fetch once; broad news is matched by company name/ticker.
    news_by_ticker = fetch_yahoo_news_bulk(list(markets.keys()))
    company_names = {}
    for ticker, (bars, meta) in markets.items():
        company_names[ticker] = meta.get("longName") or meta.get("shortName") or ticker_names.get(ticker) or ticker

    initial_candidates = []
    for ticker, (bars, meta) in markets.items():
        name = company_names[ticker]
        matches = relevant_news_for_ticker(ticker, name, global_headlines, ticker_names)
        merged_news = dedupe_news(matches + news_by_ticker.get(ticker, []))
        nscore, nnotes, nitems, lead = score_news(merged_news, ticker, name, now_utc)
        metrics = technical_metrics(bars)
        if not metrics:
            continue
        # Don't spend intraday requests on companies with no plausible fresh event.
        if nscore >= 18 or lead or (ticker in dynamic_tickers and dynamic_signal.get(ticker, 0) >= 25):
            initial_candidates.append({
                "ticker": ticker, "name": name, "bars": bars, "meta": meta,
                "news": merged_news, "initialNewsScore": nscore, "metrics": metrics,
            })

    # 5) Ask the quote source for current-session 5-minute premarket bars for the likely catalyst set.
    initial_candidates.sort(key=lambda x: (x["initialNewsScore"], dynamic_signal.get(x["ticker"], 0)), reverse=True)
    intraday_symbols = [x["ticker"] for x in initial_candidates[:70]]
    intraday_data = fetch_intraday_bulk(intraday_symbols)

    # 6) Build candidates with conditional intraday levels and strict news + reaction filters.
    all_candidates = []
    for row in initial_candidates:
        ticker, name = row["ticker"], row["name"]
        cand = make_candidate(
            ticker=ticker,
            company_name=name,
            daily_bars=row["bars"],
            global_headlines=global_headlines,
            now_utc=now_utc,
            now_et=now_et,
            yahoo_items=news_by_ticker.get(ticker, []),
            market_meta=row["meta"],
            intraday_bars=intraday_data.get(ticker, []),
        )
        if cand:
            cand["sectorCluster"] = sector_cluster(ticker)
            # SEC mapping may not expose a human-friendly name; Yahoo metadata is preferred.
            all_candidates.append(cand)

    # If fewer than five names have real catalysts, add explicit NO-TRADE placeholders from the universe.
    # These are displayed for transparency and are not presented as executable suggestions.
    if len(all_candidates) < 5:
        already = {x["ticker"] for x in all_candidates}
        fallback_rows = sorted(
            (r for r in markets.items() if r[0] not in already),
            key=lambda pair: (technical_metrics(pair[1][0]) or {}).get("technicalScore", 0),
            reverse=True,
        )
        for ticker, (bars, meta) in fallback_rows:
            metrics = technical_metrics(bars)
            if not metrics:
                continue
            name = meta.get("longName") or meta.get("shortName") or ticker_names.get(ticker) or ticker
            previous = metrics["previousClose"]
            all_candidates.append({
                "ticker": ticker, "name": name,
                "price": round(previous, 4),
                "change5d": round(metrics["change5d"], 2), "change20d": round(metrics["change20d"], 2),
                "rsi": round(metrics["rsi"], 1) if metrics["rsi"] is not None else None,
                "ema20": round(metrics["ema20"], 4) if metrics["ema20"] is not None else None,
                "ema50": round(metrics["ema50"], 4) if metrics["ema50"] is not None else None,
                "atr": round(metrics["atr"] or 0, 4), "relVolume": round(metrics["relVolume"], 2),
                "volatilityPct": round(metrics["volatilityPct"], 2), "high20": round(metrics["high20"], 4),
                "low20": round(metrics["low20"], 4), "newsScore": 0, "technicalScore": round(metrics["technicalScore"], 1),
                "premarketScore": 0, "score": round(metrics["technicalScore"] * 0.3, 1),
                "setup": "NO TRADE — NO VERIFIED CATALYST", "tradeStatus": "NO TRADE",
                "tradeStatusReason": "Fallback display only: no strong fresh positive catalyst was verified. Do not trade this row based on technicals alone.",
                "entry": None, "stop": None, "tp1": None, "tp2": None, "rr": None,
                "entryLogic": "Unavailable: no verified catalyst/premarket setup.", "stopLogic": "Unavailable",
                "tp1Logic": "Unavailable", "tp2Logic": "Unavailable", "confidence": 35,
                "confidenceNote": "Low evidence quality; not a probability of profit.",
                "whyNow": "NO TRADE | No strong current positive catalyst verified.",
                "thesis": "Shown only to keep the dashboard format complete. No actionable trade is recommended by the scanner for this company today.",
                "catalysts": [], "risks": ["No verified fresh positive catalyst.", "Technical momentum alone is not sufficient for this news-driven day-trade system."],
                "invalidate": "No trade setup: wait for a fresh catalyst and current premarket confirmation.", "news": [],
                "source": "Public market history only; no qualifying fresh catalyst", "newsNote": "No qualifying fresh catalyst",
                "premarket": {"available": False, "last": None, "open": None, "high": None, "low": None, "changePct": None,
                              "volume": None, "dollarVolume": None, "bars": 0, "lastTimestamp": None},
                "previousClose": round(previous, 4), "sessionPlan": "NO TRADE — no verified catalyst.",
                "sectorCluster": sector_cluster(ticker),
            })
            if len(all_candidates) >= 5:
                break

    if not all_candidates:
        raise RuntimeError("No candidates could be constructed; refusing to overwrite previous data.")

    picks = choose_five(all_candidates)
    # If feed outages left fewer than five, don't fabricate stock facts; keep old file when possible.
    if len(picks) < 5:
        if TODAY_FILE.exists():
            print("Fewer than five usable candidates. Keeping previous picks file.")
            return
        picks = picks[:5]

    for rank, pick in enumerate(picks, start=1):
        pick["rank"] = rank
        pick.pop("sectorCluster", None)
        # Avoid rendering a false live quote time when data is unavailable.
        pick["generatedAt"] = now_utc.isoformat()

    payload = {
        "asOf": scan_date,
        "generatedAt": now_utc.isoformat(),
        "marketTimezone": "America/New_York",
        "marketSession": "PREMARKET" if dt.time(4, 0) <= now_et.time() < dt.time(9, 30) else ("REGULAR_OR_AFTER_HOURS" if now_et.time() >= dt.time(9, 30) else "CLOSED_OR_EARLY"),
        "mode": "FREE PREMARKET CATALYST DAY-TRADE SCAN",
        "strategy": "Fresh positive news catalyst + premarket price/volume confirmation + conditional 5-minute breakout; one session only.",
        "universeSize": len(symbols),
        "candidatesScanned": len(all_candidates),
        "globalHeadlinesDiscovered": len(global_headlines),
        "qualifiedTradeSetups": sum(1 for x in picks if str(x.get("tradeStatus", "")).startswith("QUALIFIED")),
        "sourceCoverage": ["Google News RSS: US, UK, Canada, Australia, India, Spain, France, Germany, Japan queries",
                           "Yahoo Finance public ticker RSS + chart endpoint", "FDA public RSS feeds", "SEC EDGAR current 8-K/6-K/13D feeds"],
        "picks": picks,
        "method": (
            "News-first rule engine. Current positive company-specific catalysts are ranked ahead of technical metrics; "
            "pre-market price reaction, pre-market dollar volume, daily liquidity and 5-minute range confirmation determine whether a row is QUALIFIED or NO TRADE."
        ),
        "disclaimer": (
            "Free public feeds do not cover every news database or guarantee complete real-time coverage. "
            "These are conditional intraday scenarios, not a guarantee that a stock will rise. Never enter solely on a headline; "
            "wait for the stated opening-breakout confirmation, account for slippage, and close before 15:55 ET."
        ),
    }

    TODAY_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    history = load_history()
    history = [item for item in history if item.get("asOf") != scan_date]
    history.insert(0, payload)
    HISTORY_FILE.write_text(json.dumps(history[:90], indent=2, ensure_ascii=False), encoding="utf-8")
    print("Today's five:")
    for pick in picks:
        print(f"  #{pick['rank']} {pick['ticker']}: {pick.get('tradeStatus')} | news={pick.get('newsScore')} | score={pick.get('score')}")
    print(f"Saved {TODAY_FILE} and {HISTORY_FILE}")


if __name__ == "__main__":
    main()
