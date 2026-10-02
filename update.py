#!/usr/bin/env python3
"""AlphaEdge free daily scanner.
No paid market API key is required. It pulls daily OHLCV from Yahoo Finance's
public chart endpoint and headline RSS feeds, ranks a high-beta universe, and
writes data/picks.json + data/history.json.

This is an automated quantitative scanner, not a guaranteed AI forecast.
"""
from __future__ import annotations
import concurrent.futures, datetime as dt, json, math, os, re, statistics, time
import urllib.parse, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TODAY = DATA / "picks.json"
HISTORY = DATA / "history.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 AlphaEdgeDaily/1.0"

# High-beta / catalyst-sensitive US universe. The scanner changes picks every session.
UNIVERSE = """AMD NVDA AVGO SMCI MU ARM TSM INTC ASML AMAT LRCX KLAC ON MCHP MRVL
PLTR CRWD PANW DDOG NET SNOW MDB SHOP UBER DASH HOOD COIN MSTR MARA RIOT CLSK
RKLB ASTS LUNR SPCE SOUN BBAI AI PATH IONQ RGTI QBTS TEM HIMS CELH DKNG RIVN
LCID TSLA NIO XPEV XPENG CVNA AFRM UPST SOFI NU HOOD TMDX ENPH FSLR OKLO VST
GEV CAVA RBLX APP DUOL ARM AUR ACHR JOBY.
""".split()
UNIVERSE = [s for s in dict.fromkeys(UNIVERSE) if re.fullmatch(r"[A-Z]{1,5}", s)]

POS_KWS = ["raises", "upgrade", "contract", "award", "partnership", "launch", "record", "beats", "beat", "guidance", "approval", "deal", "orders", "revenue"]
NEG_KWS = ["cuts", "downgrade", "miss", "misses", "lawsuit", "investigation", "offering", "dilution", "recall", "probe", "warning", "layoffs"]


def http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last = None
    for wait in (0, 1, 2):
        try:
            if wait: time.sleep(wait)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
    raise last or RuntimeError("request failed")


def yahoo_chart(symbol: str):
    q = urllib.parse.urlencode({"range":"6mo", "interval":"1d", "includePrePost":"true", "events":"div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{q}"
    raw = json.loads(http_get(url))
    result = raw["chart"]["result"][0]
    ts = result.get("timestamp") or []
    qt = result.get("indicators", {}).get("quote", [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        try:
            o,h,l,c,v = qt["open"][i],qt["high"][i],qt["low"][i],qt["close"][i],qt["volume"][i]
            if None not in (o,h,l,c): bars.append({"t":t,"o":o,"h":h,"l":l,"c":c,"v":v or 0})
        except Exception: pass
    meta = result.get("meta", {})
    return bars, meta


def rsi(closes, period=14):
    if len(closes) <= period: return None
    gains, losses = [], []
    for a,b in zip(closes[-period-1:-1], closes[-period:]):
        d = b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains)/period; al=sum(losses)/period
    if al == 0: return 100.0
    return 100 - (100/(1+ag/al))


def ema(closes, period):
    if len(closes) < period: return None
    k=2/(period+1); e=sum(closes[:period])/period
    for x in closes[period:]: e=x*k+e*(1-k)
    return e


def atr(bars, period=14):
    if len(bars)<period+1: return None
    tr=[]
    for a,b in zip(bars[-period-1:-1],bars[-period:]):
        tr.append(max(b["h"]-b["l"], abs(b["h"]-a["c"]), abs(b["l"]-a["c"])))
    return sum(tr)/len(tr)


def news(symbol):
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    try:
        root=ET.fromstring(http_get(url))
    except Exception:
        return []
    out=[]
    for item in root.findall(".//item")[:8]:
        title=(item.findtext("title") or "").strip()
        link=(item.findtext("link") or "").strip()
        pub=(item.findtext("pubDate") or "").strip()
        out.append({"title":title,"url":link,"published":pub})
    return out


def score_news(items):
    if not items: return 0, "No fresh symbol headlines found"
    pos=neg=0
    for x in items[:6]:
        s=x["title"].lower()
        pos += any(k in s for k in POS_KWS)
        neg += any(k in s for k in NEG_KWS)
    return max(-12,min(18,pos*5-neg*6)), (f"{len(items)} fresh headlines; {pos} constructive / {neg} adverse terms")


def candidate(symbol):
    try:
        bars,meta=yahoo_chart(symbol)
        if len(bars)<55: return None
        closes=[b["c"] for b in bars]; vols=[b["v"] for b in bars]
        p=closes[-1]
        e20=ema(closes,20); e50=ema(closes,50); r=rsi(closes,14); a=atr(bars,14)
        ch5=(p/closes[-6]-1)*100; ch20=(p/closes[-21]-1)*100
        avgvol=sum(vols[-21:-1])/20 if len(vols)>=21 else 0
        rv=vols[-1]/avgvol if avgvol else 0
        volpct=(a/p*100) if a and p else 0
        high20=max(x["h"] for x in bars[-20:]); low20=min(x["l"] for x in bars[-20:])
        dist_high=(p/high20-1)*100 if high20 else 0
        news_items=news(symbol)
        ns,news_note=score_news(news_items)
        # Ranking favors momentum + trend + healthy RSI + active volume + volatility.
        s=0
        s += max(0,min(22,ch5*1.5))
        s += max(0,min(22,ch20*0.9))
        s += 12 if e20 and p>e20 else 0
        s += 10 if e50 and p>e50 else 0
        s += 12 if r and 52<=r<=72 else (5 if r and 45<=r<52 else 0)
        s += 12 if rv>=1.5 else (7 if rv>=1.1 else 2)
        s += max(0,min(10,volpct*1.6))
        s += 5 if dist_high>-3 else 0
        s += ns
        s=max(0,min(100,s))
        # Volatility-aware levels.
        atrv=a or p*0.04
        stop=max(p-1.35*atrv, p*0.92)
        risk=p-stop
        tp1=p+2.0*risk; tp2=p+3.2*risk
        rr=(tp2-p)/risk if risk else 0
        direction="Momentum continuation" if e20 and p>e20 and ch20>0 else "Volatility reversal"
        rationale=[]
        if ch5>3: rationale.append(f"5D momentum +{ch5:.1f}%")
        if rv>=1.4: rationale.append(f"relative volume {rv:.1f}x")
        if e20 and p>e20: rationale.append("price above EMA20")
        if r and 52<=r<=72: rationale.append(f"RSI {r:.0f} in momentum zone")
        if ns>0: rationale.append("fresh constructive headlines")
        if not rationale: rationale.append("multi-factor volatility setup")
        return {
            "ticker":symbol,"name":meta.get("longName") or meta.get("shortName") or symbol,
            "price":round(p,2),"change5d":round(ch5,2),"change20d":round(ch20,2),
            "rsi":round(r,1) if r else None,"ema20":round(e20,2) if e20 else None,"ema50":round(e50,2) if e50 else None,
            "atr":round(atrv,2),"relVolume":round(rv,2),"volatilityPct":round(volpct,2),"high20":round(high20,2),"low20":round(low20,2),
            "score":round(s,1),"setup":direction,"entry":round(p,2),"stop":round(stop,2),"tp1":round(tp1,2),"tp2":round(tp2,2),"rr":round(rr,2),
            "confidence":round(max(55,min(90,50+s*0.4)),0),
            "whyNow":"; ".join(rationale[:4]),
            "thesis":f"High-beta setup driven by {direction.lower()}, recent price structure and the latest available headlines. This is a scenario, not a certainty.",
            "catalysts":[x["title"] for x in news_items[:3]],"risks":["High volatility can gap through stops","Fresh expectations may already be priced in","Headline or sector reversal"],
            "invalidate":f"Close below ${stop:.2f} or a material deterioration in the setup/news thesis.",
            "news":news_items[:6],"source":"Yahoo Finance public chart + RSS","newsNote":news_note
        }
    except Exception as e:
        return None


def main():
    today=dt.datetime.now(dt.timezone.utc).date().isoformat()
    DATA.mkdir(exist_ok=True)
    results=[]
    # Conservative concurrency so the public endpoint is not hammered.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs={ex.submit(candidate,s):s for s in UNIVERSE}
        for fut in concurrent.futures.as_completed(futs):
            x=fut.result()
            if x: results.append(x)
    results.sort(key=lambda x:(x["score"],x["change20d"]),reverse=True)
    picks=[]
    for x in results:
        # Keep the daily set diverse: avoid more than 2 names from the same obvious cluster.
        picks.append(x)
        if len(picks)>=5: break
    # Fallback: keep current file if the public endpoints had a bad day.
    if len(picks)<5 and TODAY.exists():
        print("Not enough candidates; keeping previous picks.")
        return
    for i,p in enumerate(picks,1): p["rank"]=i
    payload={"asOf":today,"generatedAt":dt.datetime.now(dt.timezone.utc).isoformat(),"mode":"FREE DAILY QUANT SCAN","universeSize":len(UNIVERSE),"candidatesScanned":len(results),"picks":picks,"disclaimer":"Automated quantitative ranking using public Yahoo Finance chart/RSS endpoints. Not financial advice; levels are model scenarios."}
    TODAY.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding="utf-8")
    history=json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    history=[h for h in history if h.get("asOf")!=today]
    history.insert(0,payload)
    HISTORY.write_text(json.dumps(history[:90],indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"Saved {today}: {[p['ticker'] for p in picks]}")

if __name__=="__main__": main()
