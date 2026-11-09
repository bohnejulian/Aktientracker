"""Täglicher Kaufideen-Scan über die Indizes, die im Planspiel handelbar sind.

DAX, MDAX, TecDAX, EuroStoxx 50 (Mitglieder live von Wikipedia) plus Nasdaq 100 / Dow Jones
(feste Liste großer US-Werte). Gesucht werden einfache, erklärbare Chart-Muster.
"""

from __future__ import annotations

import io
from datetime import datetime

import requests

UA = {"User-Agent": "aktientracker/1.0 (Schulprojekt Planspiel Boerse)"}

# Nasdaq 100 + Dow Jones (Stand Ende 2026 aus dem Gedächtnis, kleine Abweichungen egal)
US = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon", "META": "Meta",
    "GOOGL": "Alphabet", "AVGO": "Broadcom", "TSLA": "Tesla", "COST": "Costco", "NFLX": "Netflix",
    "AMD": "AMD", "PEP": "PepsiCo", "ADBE": "Adobe", "CSCO": "Cisco", "TMUS": "T-Mobile US",
    "INTC": "Intel", "QCOM": "Qualcomm", "TXN": "Texas Instruments", "AMGN": "Amgen", "INTU": "Intuit",
    "ISRG": "Intuitive Surgical", "AMAT": "Applied Materials", "HON": "Honeywell", "BKNG": "Booking",
    "VRTX": "Vertex Pharma", "SBUX": "Starbucks", "GILD": "Gilead", "ADI": "Analog Devices",
    "MDLZ": "Mondelez", "REGN": "Regeneron", "ADP": "ADP", "LRCX": "Lam Research",
    "PANW": "Palo Alto Networks", "MU": "Micron", "KLAC": "KLA", "SNPS": "Synopsys", "CDNS": "Cadence",
    "MELI": "MercadoLibre", "PYPL": "PayPal", "CRWD": "CrowdStrike", "MAR": "Marriott",
    "ASML": "ASML", "ABNB": "Airbnb", "FTNT": "Fortinet", "MRVL": "Marvell", "WDAY": "Workday",
    "ADSK": "Autodesk", "NXPI": "NXP", "MNST": "Monster Beverage", "DDOG": "Datadog", "EA": "Electronic Arts",
    "LULU": "Lululemon", "ZS": "Zscaler", "TEAM": "Atlassian", "DXCM": "DexCom", "ARM": "Arm",
    "PLTR": "Palantir", "APP": "AppLovin", "AXON": "Axon", "MSTR": "Strategy", "SHOP": "Shopify",
    "DASH": "DoorDash", "TTWO": "Take-Two", "AZN": "AstraZeneca", "PDD": "PDD", "LIN": "Linde",
    "CEG": "Constellation Energy", "UNH": "UnitedHealth", "GS": "Goldman Sachs", "HD": "Home Depot",
    "CAT": "Caterpillar", "CRM": "Salesforce", "V": "Visa", "MCD": "McDonald's", "AXP": "American Express",
    "JPM": "JPMorgan", "IBM": "IBM", "BA": "Boeing", "JNJ": "Johnson & Johnson", "PG": "Procter & Gamble",
    "CVX": "Chevron", "MRK": "Merck & Co", "WMT": "Walmart", "DIS": "Disney", "KO": "Coca-Cola",
    "NKE": "Nike", "VZ": "Verizon", "SHW": "Sherwin-Williams", "TRV": "Travelers", "MMM": "3M",
}


def _wiki_tables(url: str):
    import pandas as pd
    html = requests.get(url, headers=UA, timeout=30).text
    return pd.read_html(io.StringIO(html))


def _pick(tables, sym_col_hint: str, name_cols=("Company", "Name"), min_rows=25, suffix=""):
    for t in tables:
        cols = [str(c) for c in t.columns]
        sym = next((c for c in cols if c.lower().startswith(sym_col_hint)), None)
        name = next((c for c in cols if c in name_cols), None)
        if sym and name and len(t) >= min_rows:
            out = {}
            for s, n in zip(t[sym], t[name]):
                s = str(s).strip()
                if s and s != "nan":
                    out[s if "." in s else s + suffix] = str(n)
            return out
    return {}


def load_universe(log) -> dict[str, tuple[str, str]]:
    """{ticker: (name, index)}"""
    uni: dict[str, tuple[str, str]] = {}
    sources = [
        ("DAX", "https://en.wikipedia.org/wiki/DAX", "ticker", ""),
        ("MDAX", "https://en.wikipedia.org/wiki/MDAX", "symbol", ".DE"),
        ("TecDAX", "https://de.wikipedia.org/wiki/TecDAX", "symbol", ".DE"),
        ("EuroStoxx50", "https://en.wikipedia.org/wiki/EURO_STOXX_50", "ticker", ""),
    ]
    for idx, url, hint, suf in sources:
        try:
            found = _pick(_wiki_tables(url), hint, suffix=suf)
            log(f"Scan: {idx} {len(found)} Werte")
            for t, n in found.items():
                uni.setdefault(t, (n, idx))
        except Exception as e:  # noqa: BLE001
            log(f"Scan: {idx}-Liste nicht ladbar: {e}")
    for t, n in US.items():
        uni.setdefault(t, (n, "USA"))
    return uni


def signal(closes: list[float], analyse) -> dict | None:
    """Prüft eine Kursreihe auf ein Kaufsignal. Wird vom Scan und vom Backtest benutzt."""
    if len(closes) < 210:
        return None
    a = analyse(closes)
    p = closes[-1]
    s200, s50 = a["sma200"], a["sma50"]
    d200 = (p / s200 - 1) * 100
    up = p > s200 and s50 > s200
    prev200 = sum(closes[-206:-6]) / 200
    crossed = closes[-6] < prev200 and p > s200 * 1.01 and s50 > sum(closes[-60:-10]) / 50
    if up and a["rsi"] <= 35:
        score = 40 - a["rsi"] + min(d200, 20)
        kind = "Rücksetzer im Aufwärtstrend"
        why = (f"Seit Monaten im Aufwärtstrend ({d200:.0f} % über dem 200-Tage-Schnitt), jetzt aber kurz "
               f"stark gefallen (RSI {a['rsi']:.0f}). So eine Delle ist oft eine günstige Einstiegschance, "
               "vorher kurz checken, ob schlimme News dahinterstecken.")
    elif crossed:
        score = 15 + min(d200, 10)
        kind = "Trendwende nach oben"
        why = ("Der Kurs ist in den letzten Tagen über seinen 200-Tage-Durchschnitt gestiegen, und der "
               "50-Tage-Schnitt dreht nach oben. Das ist oft der Beginn eines neuen Aufwärtstrends.")
    elif up and 50 <= a["rsi"] <= 65 and a["pos52"] >= 0.9 and (a["perf1m"] or 0) > 0.04:
        score = 10 + (a["perf1m"] or 0) * 50
        kind = "Stabiles Momentum"
        why = (f"Nahe am Jahreshoch, im letzten Monat {a['perf1m'] * 100:+.0f} %, aber noch nicht überkauft "
               f"(RSI {a['rsi']:.0f}). Starke Aktien bleiben oft eine Weile stark.")
    else:
        return None
    vol = a["vol"]
    score -= vol * 10  # ruhigere Aktien bevorzugen
    perf6m = closes[-1] / closes[-126] - 1
    score += max(-5.0, min(perf6m * 20, 10))  # langfristig starke Aktien haben mehr Potenzial
    if perf6m >= 0.15:
        why += f" Seit einem halben Jahr {perf6m * 100:+.0f} %, die Aktie hat also echten Rückenwind."
    return {"kind": kind, "score": score, "why": why, "vol": vol}


def scan(universe: dict, held: set[str], analyse, fx_rate, budget: float, max_share: float, log, top: int = 5,
         force: bool = False):
    import yfinance as yf

    tickers = sorted(universe)
    data = yf.download(tickers, period="2y", interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False, threads=True)
    ideas = []
    for t in tickers:
        if t in held:
            continue
        try:
            cl = data[t]["Close"].dropna()
            closes = [float(c) for c in cl.tolist()]
            dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else "" for d in cl.index]
        except Exception:  # noqa: BLE001
            continue
        if len(closes) < 210:
            continue
        sig = signal(closes, analyse)
        if not sig and force:  # nur für Beispielnachrichten
            sig = {"kind": "Kein Signal (nur Beispiel)", "score": 0, "vol": analyse(closes)["vol"],
                   "why": "Gerade erfüllt die Aktie keine Kaufregel, das hier zeigt nur das Format."}
        if not sig:
            continue
        name, idx = universe[t]
        kind, score, why, vol = sig["kind"], sig["score"], sig["why"], sig["vol"]
        p = closes[-1]
        # Wenige, dafür größere Trades: Einsatz je nach Schwankung
        share = 0.35 if vol < 0.25 else 0.25 if vol < 0.40 else 0.15 if vol < 0.60 else 0.08
        share = min(share, max_share)
        p_eur = p
        if not (t.endswith(".DE") or t.endswith(".PA") or t.endswith(".AS") or t.endswith(".MI")
                or t.endswith(".MC") or t.endswith(".BR") or t.endswith(".HE") or t.endswith(".IR")
                or t.endswith(".F") or t.endswith(".VI") or t.endswith(".LS")):
            rate = fx_rate("USD") if "." not in t else None
            if rate:
                p_eur = p / rate
        n = int(budget * share // p_eur) if budget else 0
        stop_pct = min(max(vol * 0.35, 0.05), 0.25)
        fx = p_eur / p if p else 1
        ideas.append({"t": t, "name": name, "idx": idx, "kind": kind, "why": why, "score": score,
                      "closes": [c * fx for c in closes], "dates": dates,
                      "price": p_eur, "n": n, "share": share, "stop": p_eur * (1 - stop_pct),
                      "stop_pct": stop_pct, "vol": vol})
    ideas.sort(key=lambda x: -x["score"])
    log(f"Scan: {len(ideas)} Kandidaten gefunden")
    return ideas[:top]
