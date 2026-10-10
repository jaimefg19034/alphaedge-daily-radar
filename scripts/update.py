#!/usr/bin/env python3
"""AlphaEdge — free, rule-based premarket catalyst/day-trade scanner.

Standard library only. Public feeds are incomplete and some quote/news endpoints are
unofficial; the output is a screened watchlist, not a prediction or an order signal.
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
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PICKS_FILE = DATA / "picks.json"
HISTORY_FILE = DATA / "history.json"
UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
APP_VERSION = "5.1"
CONTACT_EMAIL = os.getenv("ALPHAEDGE_CONTACT_EMAIL", "").strip()
UA = f"AlphaEdge/{APP_VERSION} (public-market-research; contact: {CONTACT_EMAIL or 'not-configured'})"

UNIVERSE = list(dict.fromkeys("""AAPL MSFT NVDA AMD AVGO TSM ASML ARM SMCI MU MRVL QCOM TXN INTC AMAT LRCX KLAC ON MCHP
PLTR CRWD PANW DDOG NET SNOW MDB SHOP UBER DASH HOOD COIN MSTR MARA RIOT CLSK APP RBLX
RKLB ASTS LUNR SPCE SOUN BBAI AI PATH IONQ RGTI QBTS TEM HIMS CELH DKNG TSLA RIVN LCID
NIO XPEV CVNA AFRM UPST SOFI NU TMDX ENPH FSLR OKLO VST GEV CAVA DUOL AUR ACHR JOBY
MRNA BNTX PFE LLY NVO ABBV REGN AMGN GILD VRTX BIIB ALNY SRPT CRSP NTLA BEAM EDIT VKTX
ALT SANA IOVA IMVT RXRX RARE ARWR INSM CORT AXSM NBIX LQDA CYTK PRTA FLNA
JNJ MRK BMY AZN NVS SNY GSK BABA BIDU PDD JD TME SE GRAB MELI WING ELF ULTA LULU NKE
COST WMT TGT DG DLTR BBY CAT DE CMI ETN HON GE RTX LMT NOC BA FDX UPS DAL UAL AAL
XOM CVX OXY SLB XLE GLD SLV FCX AA NEM UUUU CCJ URA WDC STX DELL HPE IBM ORCL CRM NOW
ADBE INTU XYZ PYPL GS JPM BAC C WFC RBLX HOOD DKNG PENN MGM WYNN MAR ABNB EXPE BKNG""".split()))

# These old symbols must not re-enter through stale headlines or ticker maps.
# EXAS was acquired by Abbott, APLS by Biogen, SAVA became FLNA, and SQ became XYZ.
INACTIVE_OR_RENAMED_TICKERS = {"EXAS", "APLS", "SAVA", "SQ"}

GLOBAL_QUERIES = [
    "FDA approves drug biotech shares", "FDA rejects drug clinical hold biotech stock",
    "phase 3 trial positive topline results company shares", "phase 3 trial failed misses primary endpoint biotech",
    "EMA approves drug company stock", "earnings beat raises full year guidance stock jumps",
    "earnings miss cuts guidance stock falls", "preliminary results company raises outlook shares",
    "company wins major contract stock", "government defense contract award company shares",
    "merger acquisition take private buyout offer shares", "activist investor 13D stake company stock",
    "product recall safety warning company shares", "company announces secondary offering dilution shares",
    "antitrust investigation company shares regulator", "cyberattack data breach company stock",
    "production halt factory shutdown company shares", "regulatory approval product clearance company stock",
    "patent ruling court verdict company shares", "bankruptcy restructuring going concern public company",
    "company signs multibillion dollar supply agreement shares", "analyst upgrade downgrade price target shares premarket",
    "FDA panel votes positive drug company shares", "clinical trial meets primary endpoint biotechnology company",
    "stock surges after company announcement premarket",
]
EDITIONS = [("en-US", "US", "US:en"), ("en-GB", "GB", "GB:en"),
            ("en-CA", "CA", "CA:en"), ("en-AU", "AU", "AU:en"), ("en-IN", "IN", "IN:en")]
LOCAL_QUERIES = [
    (("es-ES", "ES", "ES:es"), "aprobación FDA resultados fase 3 farmacéutica acciones"),
    (("es-ES", "ES", "ES:es"), "empresa contrato millonario resultados eleva previsiones bolsa"),
    (("fr-FR", "FR", "FR:fr"), "approbation FDA résultats phase 3 entreprise actions"),
    (("fr-FR", "FR", "FR:fr"), "contrat majeur résultats relève prévisions action"),
    (("de-DE", "DE", "DE:de"), "FDA Zulassung Phase 3 Studienergebnisse Aktie Unternehmen"),
    (("de-DE", "DE", "DE:de"), "Großauftrag Prognose erhöht Aktie Unternehmen"),
    (("ja-JP", "JP", "JP:ja"), "FDA 承認 臨床試験 フェーズ3 株価 企業"),
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
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
NASDAQ_SYMBOLS = [
    ("Nasdaq listed symbols", "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol", "Security Name"),
    ("Other listed symbols", "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol", "Security Name"),
]

POSITIVE_PATTERNS = [
    (r"\b(fda|ema)\s+(approves|approved|authorizes|authorized|clears|cleared)\b", 96, "REGULATORY APPROVAL"),
    (r"\b(positive|successful)\s+(phase\s*(iii|3)|clinical trial|top.?line)\b", 90, "POSITIVE CLINICAL DATA"),
    (r"\b(met|meets|achieved|achieves)\s+(its?\s+)?primary endpoint\b", 92, "CLINICAL ENDPOINT MET"),
    (r"\b(positive top.?line|statistically significant benefit|primary endpoint achieved)\b", 90, "POSITIVE CLINICAL DATA"),
    (r"\b(raises|raised|increases|increased)\s+(full.?year |fy\s*)?(guidance|outlook|forecast)\b", 82, "GUIDANCE RAISE"),
    (r"\b(beats?|beat)\s+(wall street|analyst|earnings|eps|revenue|estimates?)\b", 68, "EARNINGS BEAT"),
    (r"\b(reports|reported) record (revenue|sales|orders|backlog|earnings)\b", 62, "RECORD RESULTS"),
    (r"\b(wins?|awarded|selected for)\b.{0,60}\b(contract|award|program)\b", 70, "CONTRACT / AWARD"),
    (r"\b(contract|order)\b.{0,60}\b(awarded|worth \$|valued at \$|selected)\b", 65, "CONTRACT / AWARD"),
    (r"\b(agrees to be acquired|to be acquired|acquired by|buyout offer|takeover offer)\b", 78, "ACQUISITION / TAKEOVER TARGET"),
    (r"\b(activist investor|takes stake|strategic review|exploring strategic alternatives)\b", 38, "CORPORATE ACTION"),
    (r"\b(positive advisory committee vote|panel votes in favor|priority review granted|breakthrough therapy designation)\b", 54, "REGULATORY CATALYST"),
    (r"\b(large order win|commercial launch|launches new product|major product launch)\b", 34, "PRODUCT / COMMERCIAL CATALYST"),
]
NEGATIVE_PATTERNS = [
    (r"\b(phase\s*(iii|3).{0,55}(failed|fails|failure|missed|did not meet|does not meet|fails to meet|didn't meet)|failed.{0,55}(phase\s*(iii|3)|primary endpoint)|fails? to meet (its? )?primary endpoint|did not meet (its? )?primary endpoint|does not meet (its? )?primary endpoint)\b", -98, "CLINICAL FAILURE"),
    (r"\b(fda|ema)\s+(rejects|rejected|refuses|refused)\b|\bcomplete response letter\b|\bcrl from fda\b", -96, "REGULATORY REJECTION"),
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
AMBIGUOUS_SHORT_TICKERS = {"AI", "ON", "ARM", "NET", "PATH", "SO", "C", "A", "T", "X", "F", "GAP", "ALL", "LOVE", "REAL", "GOOD", "LIFE", "FAST", "RUN", "PLAY", "OPEN", "KEY", "PLUG", "BEAT", "ALLY", "PAY", "DAY", "YOU", "MOON", "WAVE", "WORK", "NOTE", "SNAP", "BOX", "APP", "BOWL", "BIRD", "CAVA", "LEAP"}
LEGAL_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "holdings", "holding", "group", "class", "common", "shares", "ordinary", "lp", "llc", "sa", "ag", "nv", "se", "the"}
GENERIC_WORDS = {"technology", "technologies", "systems", "international", "global", "health", "healthcare", "financial", "finance", "energy", "services", "solutions", "industries", "industrial", "pharmaceutical", "pharmaceuticals", "biotechnology", "bio", "capital", "partners", "resources", "software", "media", "communications", "electronics", "holdings", "company", "group", "incorporated", "corporation"}


def http_get(url: str, timeout: int = 12, sec: bool = False) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/json, application/xml, text/plain, */*;q=0.8"}
    if sec and not CONTACT_EMAIL:
        raise RuntimeError("SEC contact email not configured; set ALPHAEDGE_CONTACT_EMAIL in GitHub Actions repository variables")
    request = urllib.request.Request(url, headers=headers)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            code = exc.code
            # A 403 is not fixed by retrying; expose the actual SEC/HTTP status in logs.
            if code not in (408, 425, 429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {code} for {urllib.parse.urlparse(url).netloc}{urllib.parse.urlparse(url).path}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        if attempt < 2:
            time.sleep(1.0 * (2 ** attempt))
    raise RuntimeError(f"request failed after 3 attempts: {last_exc}") from last_exc


def safe_float(x):
    try:
        n = float(x)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def request_json(url: str, timeout: int = 12, sec: bool = False):
    return json.loads(http_get(url, timeout=timeout, sec=sec).decode("utf-8", "replace"))


def parse_date(value):
    if not value:
        return None
    raw = html.unescape(str(value)).strip()
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except Exception:
            return None


def parse_feed(url: str, source: str, limit: int = 35, sec: bool = False):
    try:
        root = ET.fromstring(http_get(url, timeout=15, sec=sec))
    except Exception as exc:
        print(f"Feed unavailable ({source}): {exc}")
        return []
    nodes = root.findall(".//item")
    if not nodes:
        nodes = [n for n in root.iter() if n.tag.rsplit("}", 1)[-1].lower() == "entry"]
    out = []
    for node in nodes[:limit]:
        fields = {}
        for child in list(node):
            fields.setdefault(child.tag.rsplit("}", 1)[-1].lower(), child)
        title_node = fields.get("title")
        title = "" if title_node is None else " ".join("".join(title_node.itertext()).split())
        link = ""
        link_node = fields.get("link")
        if link_node is not None:
            link = link_node.attrib.get("href", "") or (link_node.text or "").strip()
        published = ""
        for key in ("pubdate", "published", "updated", "date"):
            node_date = fields.get(key)
            if node_date is not None and (node_date.text or "").strip():
                published = (node_date.text or "").strip()
                break
        desc = ""
        for key in ("description", "summary", "content", "subtitle"):
            node_desc = fields.get(key)
            if node_desc is not None:
                desc = " ".join("".join(node_desc.itertext()).split())[:1500]
                if desc:
                    break
        source_name = source
        if fields.get("source") is not None:
            candidate_source = " ".join("".join(fields["source"].itertext()).split())
            if candidate_source:
                source_name = candidate_source
        title = html.unescape(title)
        desc = html.unescape(re.sub(r"<[^>]+>", " ", desc))
        if title:
            out.append({"title": title, "url": link, "published": published, "source": source_name, "description": desc})
    return out


def google_news(query, edition, lookback, limit=10):
    hl, gl, ceid = edition
    params = urllib.parse.urlencode({"q": f"{query} when:{lookback}d", "hl": hl, "gl": gl, "ceid": ceid})
    return parse_feed(f"https://news.google.com/rss/search?{params}", f"Google News {gl}", limit)


def local_time(epoch):
    return dt.datetime.fromtimestamp(epoch, tz=UTC).astimezone(NY)


def lookback_days(now_et):
    return 3 if now_et.weekday() == 0 else 2


def age_hours(now_utc, published):
    published_dt = parse_date(published)
    if not published_dt:
        return None
    age = (now_utc - published_dt).total_seconds() / 3600
    if age < -1:
        return None
    now_et = now_utc.astimezone(NY)
    maximum = 84 if now_et.weekday() == 0 else 42
    if now_et.weekday() == 1 and now_et.hour < 10:
        maximum = 66
    return max(0.0, age) if age <= maximum else None


def freshness(age):
    if age is None: return 0.0
    if age <= 3: return 1.0
    if age <= 8: return .92
    if age <= 16: return .82
    if age <= 24: return .72
    if age <= 42: return .58
    return .42


def normalize(text):
    text = html.unescape(text or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9$]+", " ", text).split())


def dedupe_news(items):
    unique = {}
    for item in items:
        title = normalize(item.get("title", ""))
        if not title:
            continue
        key = re.sub(r"\b(reuters|associated press|ap news|marketwatch|cnbc|yahoo finance|benzinga|bloomberg)\b", "", title).strip()
        old = unique.get(key)
        if old is None:
            unique[key] = dict(item)
            continue
        sources = set(old.get("sources", [old.get("source", "source")]))
        sources.add(item.get("source", "source"))
        old["sources"] = sorted(sources)
        old_dt, new_dt = parse_date(old.get("published")), parse_date(item.get("published"))
        if new_dt and (not old_dt or new_dt > old_dt):
            old["published"] = item.get("published")
        for field in ("description", "url"):
            if not old.get(field) and item.get(field):
                old[field] = item[field]
    return list(unique.values())


def discover_global_news(now_et):
    lookback = lookback_days(now_et)
    tasks = [(q, e, lookback) for q in GLOBAL_QUERIES for e in EDITIONS]
    tasks += [(q, edition, lookback) for edition, q in LOCAL_QUERIES]
    collected = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        future_map = [pool.submit(google_news, q, edition, days, 10) for q, edition, days in tasks]
        for fut in concurrent.futures.as_completed(future_map):
            try: collected.extend(fut.result())
            except Exception as exc: print(f"Google News query failed: {exc}")
    official_tasks = [(name, url, False) for name, url in FDA_FEEDS]
    if CONTACT_EMAIL:
        official_tasks += [(name, url, True) for name, url in SEC_FEEDS]
    else:
        print("SEC filing feeds skipped: configure the real contact email in ALPHAEDGE_CONTACT_EMAIL")
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        future_map = [pool.submit(parse_feed, url, name, 40, is_sec) for name, url, is_sec in official_tasks]
        for fut in concurrent.futures.as_completed(future_map):
            try: collected.extend(fut.result())
            except Exception as exc: print(f"Official feed failed: {exc}")
    now_utc = dt.datetime.now(UTC)
    return dedupe_news([n for n in collected if age_hours(now_utc, n.get("published")) is not None])


def fetch_nasdaq_symbol_map():
    mapping = {}
    for label, url, symbol_key, name_key in NASDAQ_SYMBOLS:
        try:
            body = http_get(url, timeout=15).decode("utf-8", "replace")
            lines = body.splitlines()
            if not lines:
                continue
            headers = lines[0].split("|")
            try: si, ni = headers.index(symbol_key), headers.index(name_key)
            except ValueError:
                print(f"Ticker fallback has unexpected columns: {label}")
                continue
            for line in lines[1:]:
                if line.startswith("File Creation Time") or not line:
                    continue
                fields = line.split("|")
                if len(fields) <= max(si, ni):
                    continue
                ticker = fields[si].strip().upper().replace("$", "")
                name = fields[ni].strip()
                if re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", ticker) and name and name.lower() not in {"test issue", "test instrument"}:
                    mapping.setdefault(ticker, name)
            print(f"Ticker fallback ({label}): map now has {len(mapping)} symbols")
        except Exception as exc:
            print(f"Ticker fallback unavailable ({label}): {exc}")
    return mapping


def fetch_sec_ticker_map():
    if not CONTACT_EMAIL:
        print("SEC ticker map skipped: ALPHAEDGE_CONTACT_EMAIL is not configured")
        return {}
    try:
        raw = request_json(SEC_TICKER_MAP_URL, timeout=15, sec=True)
        result = {}
        # company_tickers_exchange.json is columnar: {"fields": [...], "data": [[...], ...]}.
        # Do not treat it as the older company_tickers.json dictionary format.
        if isinstance(raw, dict) and isinstance(raw.get("fields"), list) and isinstance(raw.get("data"), list):
            fields = raw["fields"]
            try:
                name_idx, ticker_idx, exchange_idx = fields.index("name"), fields.index("ticker"), fields.index("exchange")
            except ValueError:
                raise RuntimeError(f"unexpected SEC ticker-map columns: {fields}")
            for row in raw["data"]:
                if not isinstance(row, (list, tuple)) or len(row) <= max(name_idx, ticker_idx, exchange_idx):
                    continue
                name, ticker, exchange = str(row[name_idx]).strip(), str(row[ticker_idx]).upper().strip(), str(row[exchange_idx]).strip().lower()
                # Keep exchange-listed US equities; OTC names create excessive false candidates.
                if exchange not in {"nasdaq", "nyse", "nyse american", "nyse arca", "nyse texas", "cboe bzx"}:
                    continue
                if re.fullmatch(r"[A-Z]{1,6}(?:\.[A-Z])?", ticker) and name:
                    result[ticker] = name
        elif isinstance(raw, dict):  # Compatibility with the older keyed SEC map.
            for row in raw.values():
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker", "")).upper().strip()
                name = str(row.get("title", row.get("name", ""))).strip()
                if re.fullmatch(r"[A-Z]{1,6}(?:\.[A-Z])?", ticker) and name:
                    result[ticker] = name
        if result:
            print(f"SEC ticker/name map: {len(result)} exchange-listed companies")
        else:
            print("SEC ticker map returned zero compatible exchange-listed names; using NasdaqTrader fallback")
        return result
    except Exception as exc:
        print(f"SEC ticker map unavailable: {exc}")
        return {}


def company_aliases(name):
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    while words and words[-1].lower() in LEGAL_SUFFIXES: words.pop()
    tokens = [w.lower() for w in words]
    aliases = set()
    clean = normalize(" ".join(words))
    if len(clean) >= 5: aliases.add(clean)
    for word in tokens:
        if len(word) >= 6 and word not in GENERIC_WORDS: aliases.add(word)
    for i in range(len(tokens) - 1):
        if all(len(w) >= 3 and w not in GENERIC_WORDS for w in tokens[i:i+2]):
            aliases.add(" ".join(tokens[i:i+2]))
    return {x for x in aliases if len(x) >= 4}


def build_alias_index(ticker_names):
    index = {}
    for ticker, name in ticker_names.items():
        for alias in company_aliases(name): index.setdefault(alias, set()).add(ticker)
    return sorted(index.items(), key=lambda item: len(item[0]), reverse=True)


def event_classifier(title, description=""):
    text = f"{title} {description}".lower()
    for pattern, score, label in NEGATIVE_PATTERNS:
        if re.search(pattern, text, re.I): return score, label, True
    if re.search(r"\bphase\s*(iii|3)\b.{0,100}\b(positive|successful|met|meets|achieved|achieves|statistically significant|primary endpoint)\b", text, re.I) or re.search(r"\b(positive|successful|statistically significant)\b.{0,100}\bphase\s*(iii|3)\b", text, re.I):
        return 90, "POSITIVE CLINICAL DATA", True
    for pattern, score, label in POSITIVE_PATTERNS:
        if re.search(pattern, text, re.I):
            if label == "POSITIVE CLINICAL DATA" and not re.search(r"\b(positive|successful|met|meets|achieved|achieves|statistically significant)\b", text): continue
            return score, label, True
    if re.search(r"\b(phase\s*(iii|3)|clinical trial|top.?line results|pdufa|adcom|advisory committee)\b", text, re.I): return 15, "CLINICAL / REGULATORY WATCH", False
    if re.search(r"\b(earnings|quarterly results|revenue results|guidance|forecast)\b", text, re.I): return 18, "EARNINGS WATCH", False
    if re.search(r"\b(merger|acquisition|acquires|to acquire|joint venture)\b", text, re.I): return 18, "M&A / STRATEGIC WATCH", False
    if re.search(r"\b(contract|award|partnership|supply agreement)\b", text, re.I): return 20, "COMMERCIAL WATCH", False
    return 0, "GENERAL NEWS", False


def generic_event_strength(item):
    score, _, material = event_classifier(item.get("title", ""), item.get("description", ""))
    if material: return abs(score)
    text = (item.get("title", "") + " " + item.get("description", "")).lower()
    return 12 if any(t in text for t in ("stock", "shares", "company", "earnings", "guidance", "trial", "contract", "fda")) else 0


def symbol_mentions(text, ticker_names, alias_index):
    found = {}
    for match in re.finditer(r"\$([A-Z]{1,5})\b|\b(?:NASDAQ|NYSE|AMEX|NASDAQGS|NASDAQGM|NYSEAMERICAN)\s*[:/-]\s*([A-Z]{1,5})\b", text, re.I):
        ticker = (match.group(1) or match.group(2) or "").upper()
        if ticker in ticker_names: found[ticker] = max(found.get(ticker, 0), 1.4)
    for ticker in set(re.findall(r"\b[A-Z]{1,5}\b", text)):
        if ticker not in ticker_names: continue
        if ticker in AMBIGUOUS_SHORT_TICKERS and len(ticker) <= 3: continue
        if len(ticker) >= 2: found[ticker] = max(found.get(ticker, 0), 1.05 if len(ticker) >= 4 else .75)
    padded = f" {normalize(text)} "
    for alias, tickers in alias_index:
        if f" {alias} " in padded:
            for ticker in tickers: found[ticker] = max(found.get(ticker, 0), 1.25 if " " in alias else .95)
    return found


def discover_tickers(headlines, ticker_names, alias_index):
    counts, max_signal = {}, {}
    now = dt.datetime.now(UTC)
    for item in headlines:
        if age_hours(now, item.get("published")) is None: continue
        strength = generic_event_strength(item)
        if strength < 12: continue
        found = symbol_mentions(item.get("title", "") + " " + item.get("description", ""), ticker_names, alias_index)
        for ticker, relevance in found.items():
            counts[ticker] = counts.get(ticker, 0) + relevance
            max_signal[ticker] = max(max_signal.get(ticker, 0), strength * relevance)
    extra = [ticker for ticker in counts if ticker not in UNIVERSE and ticker not in INACTIVE_OR_RENAMED_TICKERS]
    extra.sort(key=lambda ticker: (max_signal.get(ticker, 0), counts[ticker]), reverse=True)
    return extra[:35], max_signal


def relevant_news(ticker, company, headlines):
    aliases = company_aliases(company)
    out = []
    # Match a ticker token in its original uppercase form. Matching normalized lowercase
    # text against short symbols such as CAT, ALL, ON or A can create many false catalysts.
    ticker_pattern = re.compile(rf"(?<![A-Za-z0-9])(?:\$|(?:NASDAQ|NYSE|AMEX|NASDAQGS|NASDAQGM|NYSEAMERICAN)\s*[:/-]\s*)?{re.escape(ticker)}(?![A-Za-z0-9])")
    for item in headlines:
        raw_text = item.get("title", "") + " " + item.get("description", "")
        text = normalize(raw_text)
        ticker_hit = bool(ticker_pattern.search(raw_text))  # case-sensitive by design
        # For extremely ambiguous short names, require the ticker in uppercase or a company-name match.
        hit = ticker_hit or any(f" {alias} " in f" {text} " for alias in aliases)
        if hit:
            copy = dict(item); copy["matchedToTicker"] = True; copy["scope"] = "global headline match"; out.append(copy)
    return out


def score_news(items, now_utc):
    enriched = []
    for raw in dedupe_news(items):
        age = age_hours(now_utc, raw.get("published")); weight = freshness(age)
        if age is None or weight <= 0: continue
        source = raw.get("source", "News")
        scoped = bool(raw.get("tickerScoped")) or source.startswith("Yahoo Finance (")
        if not raw.get("matchedToTicker") and not scoped: continue
        score, kind, material = event_classifier(raw.get("title", ""), raw.get("description", ""))
        if source.startswith(("SEC EDGAR", "FDA")): score = max(-100, min(100, score * 1.12))
        row = dict(raw); row.update({"eventType": kind, "impactScore": round(score * weight, 1), "ageHours": round(age, 1), "materialEvent": material})
        enriched.append(row)
    enriched.sort(key=lambda x: abs(x.get("impactScore", 0)), reverse=True)
    pos = [x for x in enriched if x["impactScore"] > 0]; neg = [x for x in enriched if x["impactScore"] < 0]
    best_pos = max((x["impactScore"] for x in pos), default=0.0)
    best_neg = max((-x["impactScore"] for x in neg), default=0.0)
    corroboration = min(14.0, sum(sorted((x["impactScore"] for x in pos if x["impactScore"] < best_pos), reverse=True)[:2]) * .12)
    net = max(-100.0, min(100.0, best_pos + corroboration - best_neg * (1.25 if best_neg >= 55 else .8)))
    lead = next((x for x in pos if x.get("materialEvent") and x.get("impactScore", 0) >= 35), None)
    note = f"{len(enriched)} fresh matched headlines; lead={enriched[0]['eventType']}" if enriched else "No verified fresh company-specific headline matched"
    return round(net, 1), note, enriched[:10], lead


def yahoo_chart(ticker, range_="6mo", interval="1d"):
    params = urllib.parse.urlencode({"range": range_, "interval": interval, "events": "div,splits", **({"includePrePost": "true"} if interval != "1d" else {})})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?{params}"
    raw = request_json(url, timeout=15)
    result = raw.get("chart", {}).get("result", [None])[0]
    if not result: raise RuntimeError(raw.get("chart", {}).get("error") or "Yahoo chart has no result")
    timestamps = result.get("timestamp") or []
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    opens, highs, lows, closes, volumes = [quote.get(k, []) for k in ("open", "high", "low", "close", "volume")]
    bars = []
    for i, stamp in enumerate(timestamps):
        try:
            o, h, l, c = [safe_float(arr[i]) for arr in (opens, highs, lows, closes)]
            if None in (o, h, l, c): continue
            v = safe_float(volumes[i]) or 0.0
            if interval == "1d" and local_time(int(stamp)).date() >= dt.datetime.now(NY).date(): continue
            bars.append({"t": int(stamp), "o": o, "h": h, "l": l, "c": c, "v": v})
        except (IndexError, TypeError, ValueError): continue
    return bars, result.get("meta", {})


def yahoo_news(ticker, limit=10):
    params = urllib.parse.urlencode({"s": ticker, "region": "US", "lang": "en-US"})
    rows = parse_feed(f"https://feeds.finance.yahoo.com/rss/2.0/headline?{params}", f"Yahoo Finance ({ticker})", limit)
    for item in rows:
        item["tickerScoped"] = True
    return rows


def fetch_bulk(tickers, fn, workers=8):
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, ticker): ticker for ticker in tickers}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try: result[ticker] = future.result()
            except Exception as exc:
                # Log failures instead of silently making data availability look perfect.
                print(f"Data unavailable ({ticker}): {exc}")
                result[ticker] = None
    return result


def ema(closes, period):
    if len(closes) < period: return None
    k = 2 / (period + 1); value = sum(closes[:period]) / period
    for price in closes[period:]: value = price * k + value * (1 - k)
    return value


def rsi(closes, period=14):
    if len(closes) <= period: return None
    deltas = [b-a for a, b in zip(closes[-period-1:-1], closes[-period:])]
    gain = sum(max(x, 0) for x in deltas) / period
    loss = sum(max(-x, 0) for x in deltas) / period
    if loss == 0: return 100.0
    return 100 - (100 / (1 + gain/loss))


def atr(bars, period=14):
    if len(bars) < period + 1: return None
    trs = []
    for prev, cur in zip(bars[-period-1:-1], bars[-period:]):
        trs.append(max(cur["h"]-cur["l"], abs(cur["h"]-prev["c"]), abs(cur["l"]-prev["c"])))
    return sum(trs) / len(trs)


def technical_metrics(bars):
    if not bars or len(bars) < 55: return None
    close = [b["c"] for b in bars]; volume = [b["v"] for b in bars]; price = close[-1]
    e20, e50, r, a = ema(close, 20), ema(close, 50), rsi(close), atr(bars)
    change5 = (price / close[-6] - 1) * 100 if close[-6] else 0.0
    change20 = (price / close[-21] - 1) * 100 if close[-21] else 0.0
    avgvol = sum(volume[-21:-1]) / 20 if len(volume) >= 21 else 0.0
    relvol = volume[-1] / avgvol if avgvol else 0.0
    vol_pct = a / price * 100 if a and price else 0.0
    score = (8 if e20 and price > e20 else 0) + (5 if e50 and price > e50 else 0) + (7 if r is not None and 48 <= r <= 75 else 0)
    score += (7 if change5 > 0 else 0) + (6 if change20 > 0 else 0) + (6 if relvol >= 1 else 0) + min(5, max(0, vol_pct))
    return {"previousClose": price, "ema20": e20, "ema50": e50, "rsi": r, "atr": a, "change5d": change5, "change20d": change20,
            "relVolume": relvol, "high20": max(x["h"] for x in bars[-20:]), "low20": min(x["l"] for x in bars[-20:]),
            "volatilityPct": vol_pct, "avgVolume20": avgvol, "technicalScore": min(50.0, score)}


def premarket_metrics(bars, now_et):
    today = now_et.date(); pm = [b for b in (bars or []) if local_time(b["t"]).date() == today and dt.time(4) <= local_time(b["t"]).time() < dt.time(9, 30)]
    pm.sort(key=lambda b: b["t"])
    if len(pm) < 3: return {"available": False, "bars": pm, "reason": "No reliable current-session premarket bars"}
    latest = pm[-1]["c"]
    return {"available": True, "bars": pm, "open": pm[0]["o"], "last": latest, "high": max(b["h"] for b in pm), "low": min(b["l"] for b in pm),
            "volume": sum(b["v"] for b in pm), "dollarVolume": sum(b["v"]*b["c"] for b in pm), "pmAtr": atr(pm),
            "lastTimestamp": dt.datetime.fromtimestamp(pm[-1]["t"], UTC).isoformat(), "barsCount": len(pm)}


def intraday_levels(pm, daily_atr):
    if not pm.get("available"): return None
    price = pm["last"]; atr5 = pm.get("pmAtr") or (daily_atr / 78 if daily_atr else price * .0025)
    atr5 = max(atr5, price * .0008)
    entry = pm["high"] + max(price * .0005, atr5 * .08)
    stop = min(entry - 1.25 * atr5, pm["low"] - .08 * atr5)
    risk = entry - stop
    if not all(math.isfinite(x) for x in (entry, stop, risk)) or risk <= 0 or entry <= 0: return None
    return {"entry": round(entry, 4), "stop": round(stop, 4), "tp1": round(entry+1.5*risk, 4), "tp2": round(entry+2.2*risk, 4), "rr": 2.2,
            "riskPct": round(risk/entry*100, 2), "entryLogic": "Conditional 5-minute breakout and hold above the premarket high after the regular session opens; not a premarket market order.",
            "stopLogic": "Below premarket low / volatility buffer; gaps and slippage can exceed this level.", "tp1Logic": "1.5R intraday target.",
            "tp2Logic": "2.2R target; exit remaining position before 15:55 ET."}


def premarket_score(pm, previous_close, avg_volume):
    if not pm.get("available") or not previous_close: return 0.0
    gap = (pm["last"]/previous_close - 1) * 100; score = 0.0
    if 1 <= gap <= 12: score += 22
    elif .5 <= gap < 1: score += 11
    elif 12 < gap <= 18: score += 8
    elif gap > 18 or gap < -1: score -= 15
    if pm["last"] >= pm["open"]: score += 6
    if (pm["last"]/(pm["high"] or pm["last"])-1)*100 >= -1.25: score += 7
    dollars = pm.get("dollarVolume", 0)
    score += 15 if dollars >= 1_000_000 else 12 if dollars >= 300_000 else 8 if dollars >= 100_000 else 4 if dollars >= 50_000 else 0
    if avg_volume and pm.get("volume", 0)/avg_volume >= .03: score += 5
    return max(0.0, min(50.0, score))


def make_candidate(ticker, name, bars, meta, headlines, scoped_news, intraday, now_utc, now_et):
    metrics = technical_metrics(bars)
    if not metrics: return None
    match = relevant_news(ticker, name, headlines)
    news_score, news_note, ranked_news, lead = score_news(match + (scoped_news or []), now_utc)
    pm = premarket_metrics(intraday or [], now_et)
    previous = metrics["previousClose"]
    pm_gap = (pm["last"]/previous-1)*100 if pm.get("available") and previous else None
    levels = intraday_levels(pm, metrics["atr"])
    pms = premarket_score(pm, previous, metrics["avgVolume20"])
    score = max(0.0, min(100.0, max(0.0, news_score)*.58 + pms*.27 + metrics["technicalScore"]*.30))
    if news_score < 0: score = max(0.0, score + news_score*.35)
    before_open = now_et.weekday() < 5 and dt.time(4) <= now_et.time() < dt.time(9, 30)
    avgvol = metrics["avgVolume20"] or 0
    status, reason = "NO TRADE", ""
    positive_lead = bool(lead and lead.get("impactScore", 0) >= 32)
    if not before_open: reason = "Outside the valid premarket scan window; no live trade setup is being recommended."
    elif news_score < 38 or not positive_lead: reason = "No strong, fresh positive company-specific catalyst verified; technical strength alone is insufficient."
    elif not pm.get("available"): reason = "No reliable current-session premarket bars; numerical entry levels are unavailable."
    elif pm_gap is None or pm_gap < .5: reason = "Positive headline without meaningful positive premarket reaction; wait or stand aside."
    elif pm_gap > 18: reason = "Premarket gap is extremely extended; chase risk is high."
    elif pm.get("dollarVolume", 0) < 100_000: reason = "Premarket dollar volume is too thin for a robust intraday setup."
    elif avgvol < 400_000: reason = "Average daily share volume is below the liquidity filter."
    elif not levels: reason = "Could not compute valid conditional breakout levels."
    elif levels["riskPct"] > 3.5: reason = f"Modeled stop distance is too wide ({levels['riskPct']:.1f}%)."
    elif pm_gap < 1: reason = "Catalyst found, but the premarket reaction is weak; wait for the opening range."
    else:
        status = "QUALIFIED — INTRADAY LONG WATCH"
        reason = "Fresh positive catalyst, positive premarket reaction and liquidity/risk filters pass. Entry is conditional on a confirmed breakout after the open."
    blank_levels = {"entry": None, "stop": None, "tp1": None, "tp2": None, "rr": None, "entryLogic": "Unavailable without current-session premarket data.", "stopLogic": "No order until live price/range is available.", "tp1Logic": "Unavailable", "tp2Logic": "Unavailable"}
    out_levels = levels or blank_levels
    catalyst_line = f"{lead['eventType']}: {lead['title']}" if lead else "No strong positive catalyst"
    top_news = ranked_news[:8]
    confidence = 42 + (15 if lead and lead.get("source", "").startswith(("SEC EDGAR", "FDA")) else 10 if lead else 0)
    if lead: confidence += min(14, max(0, lead.get("impactScore", 0))*.12)
    if pm.get("available"): confidence += 8
    if len({x.get("source", "") for x in top_news}) >= 2: confidence += 5
    confidence = int(max(35, min(82, confidence)))
    risk_list = ["Day-trade scenario only; never carry overnight.", "Premarket gaps can reverse; wait for breakout/hold confirmation.", "Free public feeds can be delayed/incomplete; slippage may exceed the modeled stop."]
    if news_score < 0: risk_list.insert(0, "The recent matched news is net adverse; do not treat this as a long setup.")
    return {
        "ticker": ticker, "name": name or meta.get("longName") or meta.get("shortName") or ticker,
        "price": round(pm["last"] if pm.get("available") else previous, 4), "change5d": round(metrics["change5d"], 2), "change20d": round(metrics["change20d"], 2),
        "rsi": round(metrics["rsi"], 1) if metrics["rsi"] is not None else None, "ema20": round(metrics["ema20"], 4) if metrics["ema20"] is not None else None,
        "ema50": round(metrics["ema50"], 4) if metrics["ema50"] is not None else None, "atr": round(metrics["atr"] or 0, 4), "relVolume": round(metrics["relVolume"], 2),
        "volatilityPct": round(metrics["volatilityPct"], 2), "high20": round(metrics["high20"], 4), "low20": round(metrics["low20"], 4),
        "newsScore": news_score, "technicalScore": round(metrics["technicalScore"], 1), "premarketScore": round(pms, 1), "score": round(score, 1),
        "setup": "INTRADAY LONG — PM HIGH BREAKOUT" if status.startswith("QUALIFIED") else "WATCH ONLY — WAIT FOR OPEN" if positive_lead else "NO TRADE — NO STRONG CATALYST",
        "tradeStatus": status, "tradeStatusReason": reason, "entry": out_levels["entry"], "stop": out_levels["stop"], "tp1": out_levels["tp1"], "tp2": out_levels["tp2"], "rr": out_levels["rr"],
        "entryLogic": out_levels["entryLogic"], "stopLogic": out_levels["stopLogic"], "tp1Logic": out_levels["tp1Logic"], "tp2Logic": out_levels["tp2Logic"],
        "confidence": confidence, "confidenceNote": "Evidence quality/recency only; not probability of profit.",
        "whyNow": f"{catalyst_line} | {'Premarket ' + format(pm_gap, '+.2f') + '%' if pm_gap is not None else 'Premarket quote unavailable'} | {status}",
        "thesis": f"News-first intraday screen. {reason} Re-check catalyst and opening-range confirmation before any decision.",
        "catalysts": [x.get("title", "") for x in top_news[:4]], "risks": risk_list,
        "invalidate": f"Cancel if breakout/hold fails, the catalyst is contradicted, or price breaches stop ${out_levels['stop']:.4f}. Exit before 15:55 ET." if out_levels["stop"] is not None else "No trade: wait for a fresh catalyst and current-session premarket data.",
        "news": [{k: n.get(k) for k in ("title", "url", "published", "source", "eventType", "impactScore", "ageHours", "materialEvent")} for n in top_news],
        "source": "Google News RSS + Yahoo Finance public RSS/chart + FDA RSS + SEC EDGAR (when contact configured)", "newsNote": news_note,
        "premarket": {"available": bool(pm.get("available")), "last": round(pm["last"], 4) if pm.get("available") else None, "open": round(pm["open"], 4) if pm.get("available") else None,
                       "high": round(pm["high"], 4) if pm.get("available") else None, "low": round(pm["low"], 4) if pm.get("available") else None,
                       "changePct": round(pm_gap, 2) if pm_gap is not None else None, "volume": int(pm.get("volume", 0)) if pm.get("available") else None,
                       "dollarVolume": round(pm.get("dollarVolume", 0), 0) if pm.get("available") else None, "bars": int(pm.get("barsCount", 0)) if pm.get("available") else 0,
                       "lastTimestamp": pm.get("lastTimestamp")}, "previousClose": round(previous, 4),
        "sessionPlan": "DAY TRADE ONLY — conditional entry after opening bell; flatten before 15:55 America/New_York."}


def sector_cluster(ticker):
    groups = {
        "semiconductors": set("NVDA AMD AVGO TSM ASML ARM SMCI MU MRVL QCOM TXN INTC AMAT LRCX KLAC ON MCHP WDC STX".split()),
        "software/AI": set("PLTR CRWD PANW DDOG NET SNOW MDB APP PATH AI SOUN BBAI IONQ RGTI QBTS TEM ORCL CRM NOW ADBE".split()),
        "biotech/pharma": set("MRNA BNTX PFE LLY NVO ABBV REGN AMGN GILD VRTX BIIB ALNY SRPT CRSP NTLA BEAM EDIT VKTX ALT SANA IOVA IMVT RXRX RARE ARWR INSM CORT AXSM NBIX LQDA CYTK PRTA FLNA JNJ MRK BMY AZN NVS SNY GSK".split()),
        "space/defense": set("RKLB ASTS LUNR SPCE ACHR JOBY AUR RTX LMT NOC BA".split()),
        "crypto/fintech": set("HOOD COIN MSTR MARA RIOT CLSK AFRM UPST SOFI NU XYZ PYPL".split()),
        "EV/mobility": set("TSLA RIVN LCID NIO XPEV CVNA UBER DASH LYFT".split()),
        "energy/materials": set("XOM CVX OXY SLB OKLO VST GEV ENPH FSLR FCX AA NEM UUUU CCJ".split()),
    }
    return next((label for label, members in groups.items() if ticker in members), "other")


def choose_five(candidates):
    ranked = sorted(candidates, key=lambda x: (x.get("tradeStatus", "").startswith("QUALIFIED"), x.get("newsScore", 0), x.get("score", 0), x.get("premarketScore", 0)), reverse=True)
    picks, chosen = [], set()
    for item in ranked:
        if item["ticker"] in chosen: continue
        cluster = item.get("sectorCluster", "")
        if cluster and sum(1 for p in picks if p.get("sectorCluster") == cluster) >= 2: continue
        picks.append(item); chosen.add(item["ticker"])
        if len(picks) == 5: return picks
    for item in ranked:
        if item["ticker"] not in chosen:
            picks.append(item); chosen.add(item["ticker"])
            if len(picks) == 5: break
    return picks


def easter_sunday(year):
    # Gregorian computus (Meeus/Jones/Butcher)
    a=year%19; b=year//100; c=year%100; d=b//4; e=b%4; f=(b+8)//25; g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30; i=c//4; k=c%4; l=(32+2*e+2*i-h-k)%7; m=(a+11*h+22*l)//451
    month=(h+l-7*m+114)//31; day=((h+l-7*m+114)%31)+1
    return dt.date(year, month, day)


def observed(day):
    if day.weekday() == 5: return day - dt.timedelta(days=1)
    if day.weekday() == 6: return day + dt.timedelta(days=1)
    return day


def nth_weekday(year, month, weekday, n):
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday-first.weekday())%7 + 7*(n-1))


def last_weekday(year, month, weekday):
    if month == 12: day = dt.date(year+1, 1, 1) - dt.timedelta(days=1)
    else: day = dt.date(year, month+1, 1) - dt.timedelta(days=1)
    return day - dt.timedelta(days=(day.weekday()-weekday)%7)


def market_holidays(year):
    holidays = set()
    # Include observed New Year's Day around year boundaries.
    for y in (year-1, year, year+1):
        holidays.add(observed(dt.date(y, 1, 1)))
    holidays.update({nth_weekday(year, 1, 0, 3), nth_weekday(year, 2, 0, 3),
                     easter_sunday(year)-dt.timedelta(days=2), last_weekday(year, 5, 0),
                     observed(dt.date(year, 6, 19)), observed(dt.date(year, 7, 4)),
                     nth_weekday(year, 9, 0, 1), nth_weekday(year, 11, 3, 4), observed(dt.date(year, 12, 25))})
    return holidays


def is_market_day(day):
    return day.weekday() < 5 and day not in market_holidays(day.year)


def load_history():
    try:
        value = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    now_utc = dt.datetime.now(UTC); now_et = now_utc.astimezone(NY); date_et = now_et.date()
    print(f"AlphaEdge {APP_VERSION} scan: {now_et.isoformat()}")
    print(f"Session: {now_et.strftime('%A %H:%M %Z')} | contact configured={'yes' if CONTACT_EMAIL else 'NO'}")
    # Scheduled runs never replace the live dashboard outside premarket. Manual runs may
    # perform diagnostics on weekends/after-hours, but those results are never published.
    diagnostic_only = os.getenv("ALPHAEDGE_DIAGNOSTIC_ONLY", "false").strip().lower() in {"1", "true", "yes"}
    market_day = is_market_day(date_et)
    in_premarket = market_day and dt.time(4) <= now_et.time() < dt.time(9, 30)
    if not market_day and not diagnostic_only:
        print("Market closed (weekend/NYSE holiday). Existing picks/history preserved; no replacement written.")
        return
    if not in_premarket and not diagnostic_only:
        print("Outside 04:00–09:30 ET premarket window. Scan skipped to preserve last valid picks.")
        return
    if diagnostic_only and not in_premarket:
        print("MANUAL DIAGNOSTIC MODE: checking news/ticker/market feeds off-session; picks/history will NOT be replaced.")
    lookback = lookback_days(now_et)
    headlines = discover_global_news(now_et)
    print(f"Fresh global/event headlines: {len(headlines)}")
    sec_map = fetch_sec_ticker_map()
    ticker_names = dict(fetch_nasdaq_symbol_map())
    ticker_names.update(sec_map)
    for ticker in UNIVERSE: ticker_names.setdefault(ticker, ticker)
    alias_index = build_alias_index(ticker_names)
    dynamic, dynamic_signal = discover_tickers(headlines, ticker_names, alias_index)
    symbols = list(dict.fromkeys(UNIVERSE + dynamic))
    print(f"Ticker map: {len(ticker_names)} names | symbols considered: {len(symbols)} | dynamic discoveries: {len(dynamic)}")

    market_raw = fetch_bulk(symbols, lambda ticker: yahoo_chart(ticker, "6mo", "1d"), workers=10)
    markets = {}
    for ticker, value in market_raw.items():
        if value and len(value[0]) >= 55:
            markets[ticker] = value
    print(f"Daily market histories received: {len(markets)}/{len(symbols)}")
    if not markets:
        raise RuntimeError("No daily market data returned; refusing to overwrite existing picks.")

    # Rank names using actual broad-news matches before making per-ticker RSS requests.
    prelim = []
    for ticker, (bars, meta) in markets.items():
        name = meta.get("longName") or meta.get("shortName") or ticker_names.get(ticker, ticker)
        matches = relevant_news(ticker, name, headlines)
        score, _, _, lead = score_news(matches, now_utc)
        tm = technical_metrics(bars)
        prelim.append((ticker, name, bars, meta, matches, score, bool(lead), (tm or {}).get("technicalScore", 0)))
    # Keep broad company news coverage while reducing avoidable Yahoo RSS traffic.
    prelim.sort(key=lambda row: (row[5] >= 18 or row[6], row[5], dynamic_signal.get(row[0], 0), row[7]), reverse=True)
    rss_symbols = [row[0] for row in prelim[:100]]
    news_raw = fetch_bulk(rss_symbols, lambda ticker: yahoo_news(ticker, 10), workers=8)
    # If a ticker's global news did not qualify but Yahoo RSS finds a fresh event, it is included below.
    ticker_scoped = {k: v for k, v in news_raw.items() if v}
    print(f"Yahoo ticker-news feeds with data: {len(ticker_scoped)}/{len(rss_symbols)}")

    ready = []
    for ticker, name, bars, meta, matches, prelim_score, prelim_lead, tech_score in prelim:
        yahoo_items = news_raw.get(ticker) or []
        total, _, _, lead = score_news(matches + yahoo_items, now_utc)
        if total >= 18 or lead or (ticker in dynamic and dynamic_signal.get(ticker, 0) >= 25) or yahoo_items:
            ready.append((ticker, name, bars, meta, yahoo_items))
    ready.sort(key=lambda row: (score_news(relevant_news(row[0], row[1], headlines)+(row[4] or []), now_utc)[0], dynamic_signal.get(row[0], 0)), reverse=True)
    # Only request intraday bars for the likely catalyst candidates; 5-minute data is the bottleneck.
    intraday_symbols = [row[0] for row in ready[:60]]
    intraday_raw = fetch_bulk(intraday_symbols, lambda ticker: yahoo_chart(ticker, "5d", "5m")[0], workers=8)
    candidates = []
    for ticker, name, bars, meta, yahoo_items in ready:
        candidate = make_candidate(ticker, name, bars, meta, headlines, yahoo_items or [], intraday_raw.get(ticker) or [], now_utc, now_et)
        if candidate:
            candidate["sectorCluster"] = sector_cluster(ticker)
            candidates.append(candidate)
    print(f"Candidates built: {len(candidates)} | current-session intraday feeds received: {sum(bool(x) for x in intraday_raw.values())}/{len(intraday_symbols)}")

    # Complete the dashboard with explicit NO TRADE placeholders, never fabricated entries.
    if len(candidates) < 5:
        excluded = {x["ticker"] for x in candidates}
        fallback = sorted(((t, bars, meta) for t, (bars, meta) in markets.items() if t not in excluded),
                          key=lambda row: (technical_metrics(row[1]) or {}).get("technicalScore", 0), reverse=True)
        for ticker, bars, meta in fallback:
            metrics = technical_metrics(bars)
            if not metrics: continue
            name = meta.get("longName") or meta.get("shortName") or ticker_names.get(ticker, ticker)
            candidates.append({"ticker": ticker, "name": name, "price": round(metrics["previousClose"],4), "change5d": round(metrics["change5d"],2),
                "change20d": round(metrics["change20d"],2), "rsi": round(metrics["rsi"],1) if metrics["rsi"] is not None else None,
                "ema20": round(metrics["ema20"],4) if metrics["ema20"] is not None else None, "ema50": round(metrics["ema50"],4) if metrics["ema50"] is not None else None,
                "atr": round(metrics["atr"] or 0,4), "relVolume": round(metrics["relVolume"],2), "volatilityPct": round(metrics["volatilityPct"],2),
                "high20": round(metrics["high20"],4), "low20": round(metrics["low20"],4), "newsScore": 0, "technicalScore": round(metrics["technicalScore"],1),
                "premarketScore": 0, "score": round(metrics["technicalScore"]*.3,1), "setup": "NO TRADE — NO VERIFIED CATALYST", "tradeStatus": "NO TRADE",
                "tradeStatusReason": "Display-only fallback; no qualifying fresh positive catalyst was verified.", "entry": None, "stop": None, "tp1": None, "tp2": None, "rr": None,
                "entryLogic": "Unavailable without verified catalyst and premarket setup.", "stopLogic": "Unavailable", "tp1Logic": "Unavailable", "tp2Logic": "Unavailable",
                "confidence": 35, "confidenceNote": "Low evidence quality; not probability of profit.", "whyNow": "NO TRADE | No strong current positive catalyst verified.",
                "thesis": "Displayed only to complete the dashboard; no actionable trade is recommended.", "catalysts": [],
                "risks": ["No verified fresh positive catalyst.", "Technical momentum alone is not sufficient."], "invalidate": "No trade setup; wait for fresh catalyst and premarket confirmation.",
                "news": [], "source": "Public market history only; no qualifying fresh catalyst", "newsNote": "No qualifying fresh catalyst",
                "premarket": {"available": False, "last": None, "open": None, "high": None, "low": None, "changePct": None, "volume": None, "dollarVolume": None, "bars": 0, "lastTimestamp": None},
                "previousClose": round(metrics["previousClose"],4), "sessionPlan": "NO TRADE — no verified catalyst.", "sectorCluster": sector_cluster(ticker)})
            if len(candidates) >= 5: break
    if not candidates:
        raise RuntimeError("No candidates could be built; existing picks/history preserved.")
    picks = choose_five(candidates)
    if len(picks) < 5 and PICKS_FILE.exists():
        print("Fewer than five usable candidates; previous picks file preserved.")
        return
    for rank, pick in enumerate(picks, 1):
        pick["rank"] = rank; pick.pop("sectorCluster", None); pick["generatedAt"] = now_utc.isoformat()
    payload = {
        "asOf": date_et.isoformat(), "generatedAt": now_utc.isoformat(), "marketTimezone": "America/New_York", "marketSession": "PREMARKET",
        "mode": "FREE PREMARKET CATALYST DAY-TRADE SCAN", "strategy": "Fresh company-specific catalyst + premarket confirmation + conditional 5-minute breakout; one session only.",
        "universeSize": len(symbols), "candidatesScanned": len(candidates), "globalHeadlinesDiscovered": len(headlines),
        "qualifiedTradeSetups": sum(1 for x in picks if str(x.get("tradeStatus", "")).startswith("QUALIFIED")),
        "secContactConfigured": bool(CONTACT_EMAIL), "sourceCoverage": ["Google News RSS: US, UK, Canada, Australia, India, Spain, France, Germany, Japan", "Yahoo Finance public ticker RSS/chart endpoint", "FDA public RSS feeds", "SEC EDGAR feeds (requires contact email configured)"],
        "picks": picks,
        "method": "Rule-based news-first screener. A row is QUALIFIED only when catalyst, premarket reaction, liquidity and modeled risk filters pass. Others remain WATCH/NO TRADE.",
        "disclaimer": "Free public sources can be delayed, incomplete, or unavailable. Entry is conditional, not a guaranteed signal. Confirm live prices/spreads, account for slippage, and exit intraday positions before 15:55 ET."}
    print("Today's five:")
    for pick in picks: print(f"  #{pick['rank']} {pick['ticker']}: {pick.get('tradeStatus')} | news={pick.get('newsScore')} | score={pick.get('score')}")
    if diagnostic_only and not in_premarket:
        print("DIAGNOSTIC COMPLETE: source checks finished; data/picks.json and data/history.json were not modified.")
        return
    PICKS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    history = [x for x in load_history() if x.get("asOf") != date_et.isoformat()]
    history.insert(0, payload)
    HISTORY_FILE.write_text(json.dumps(history[:90], indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {PICKS_FILE} and {HISTORY_FILE}")


if __name__ == "__main__":
    main()
