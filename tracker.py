"""Aktien- und News-Tracker fürs Planspiel Börse.

Läuft alle 5 Minuten (GitHub Actions, angestoßen von cron-job.org), prüft Kurse und News zu den
Aktien in depot.yaml und schickt Alarme über ntfy aufs Handy.

Lokal testen:  NTFY_TOPIC=mein-geheimer-kanal python tracker.py --test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

BERLIN = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).parent
DEPOT_FILE = ROOT / "depot.yaml"
STATE_FILE = ROOT / "state.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


# ---------------------------------------------------------------- Hilfen

def log(msg: str) -> None:
    print(f"[{datetime.now(BERLIN):%H:%M:%S}] {msg}", flush=True)


def eur(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x: float) -> str:
    return f"{x:+.1f} %".replace(".", ",")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json kaputt, fange neu an")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- ntfy

def notify(title: str, message: str, *, priority: int = 3, tags: list[str] | None = None,
           click: str | None = None, actions: list[tuple[str, str]] | None = None,
           channel: str = "", attach: str | None = None, image: bytes | None = None, dry_run: bool = False) -> None:
    """actions: [(Knopf-Text, Nachricht an den Eingangskanal)], max. 3. channel: z. B. "bot" -> <topic>-bot"""
    base = os.environ.get("NTFY_TOPIC")
    topic = f"{base}-{channel}" if base and channel else base
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if attach:
        payload["attach"] = attach
        payload["filename"] = "chart.png"
    if actions and topic:
        payload["actions"] = [{"action": "http", "label": label[:40], "method": "POST", "clear": True,
                               "url": f"{NTFY_SERVER}/{base}", "body": body}
                              for label, body in actions[:3]]
    melden = CFG.get("melden_url")
    # Knopf zur Verkaufsseite, nur mit den vom Bot vorgeschlagenen Käufen (bot: true).
    # Kanal + Positionen stehen nur im #-Teil des Links (geht nie an einen Server)
    pos = []
    if melden and base and not channel:
        pos = [[p.get("ticker"), str(p.get("name", p.get("ticker")))[:28], _num_or(p.get("stueck")),
                round((LAST_QUOTES.get(p.get("ticker")) or {}).get("price") or 0, 2)]
               for p in DEPOT.get("positionen") or [] if p.get("ticker") and p.get("bot")]
    if melden and base and not channel and pos and len(payload.get("actions") or []) < 3:
        frag = urllib.parse.urlencode({"t": base, "p": json.dumps(pos, ensure_ascii=False, separators=(",", ":")),
                                       "b": round(float(DEPOT.get("budget") or 0), 2),
                                       "z": datetime.now(BERLIN).strftime("%d.%m. %H:%M"),
                                       "f": f"{CFG.get('gebuehr_prozent', 0.3)},{CFG.get('gebuehr_min', 15)}"},
                                      quote_via=urllib.parse.quote)
        payload.setdefault("actions", []).append({"action": "view", "label": "📝 Aktie verkaufen",
                                                  "url": f"{melden}#{frag}", "clear": False})
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    if dry_run or not topic:
        log(f"[ntfy{(' ' + channel) if channel else ''} {'dry-run' if dry_run else 'KEIN NTFY_TOPIC'}] {title} | {message}"
            + (f" | Knöpfe: {actions}" if actions else ""))
        return
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    if image:
        # Bild direkt hochladen (PUT), alle anderen Felder als URL-Parameter
        params = {k: v for k, v in payload.items() if k in ("title", "message", "priority", "click")}
        params["filename"] = "kaufidee.png"
        if tags:
            params["tags"] = ",".join(tags)
        if payload.get("actions"):
            params["actions"] = json.dumps(payload["actions"], ensure_ascii=False)
        for attempt in range(3):
            try:
                r = requests.put(f"{NTFY_SERVER}/{topic}", data=image, params=params, headers=headers, timeout=30)
                r.raise_for_status()
                return
            except requests.RequestException as e:
                log(f"ntfy-Bild fehlgeschlagen ({e}), Versuch {attempt + 1}/3")
                time.sleep(2 * (attempt + 1))
        payload.pop("attach", None)  # zur Not ohne Bild schicken
    for attempt in range(3):
        try:
            r = requests.post(NTFY_SERVER, json=payload, headers=headers, timeout=15)
            r.raise_for_status()
            return
        except requests.RequestException as e:
            log(f"ntfy fehlgeschlagen ({e}), Versuch {attempt + 1}/3")
            time.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------- Kurse

PREFERRED_SUFFIXES = (".DE", ".F", ".SG", ".MU", ".DU", ".BE", ".HM")  # deutsche Börsenplätze, Kurse in €


def _has_eur_data(yf, symbol: str) -> bool:
    try:
        tk = yf.Ticker(symbol)
        if len(tk.history(period="1mo", interval="1d")["Close"].dropna()) < 5:
            return False
        return (tk.fast_info.get("currency") or "").upper() == "EUR"
    except Exception:  # noqa: BLE001
        return False


def resolve_symbols(depot: dict, state: dict) -> None:
    """Setzt item['ticker'] anhand der ISIN: deutscher Börsenplatz in € mit echten Kursdaten."""
    import yfinance as yf

    cache = state.setdefault("symbols_eur", {})
    for item in all_items(depot):
        if item.get("ticker"):
            continue
        isin = item.get("isin")
        if not isin:
            continue
        if isin in cache:
            item["ticker"] = cache[isin]
            continue
        try:
            found = yf.Search(isin, max_results=25, news_count=0).quotes or []
        except Exception as e:  # noqa: BLE001
            log(f"ISIN-Suche {isin} fehlgeschlagen: {e}")
            found = []
        symbols = [q.get("symbol") for q in found if q.get("symbol")]
        ordered = [s for suf in PREFERRED_SUFFIXES for s in symbols if s.endswith(suf)]
        ordered += [s for s in symbols if s not in ordered]
        log(f"{item.get('name', isin)}: Kandidaten {symbols}")
        pick = next((s for s in ordered if _has_eur_data(yf, s)), None)
        if not pick:
            # z. B. US-Aktien: Heimatbörse nehmen und später in € umrechnen
            pick = next((s for s in ordered if "." not in s), None)
        if pick:
            log(f"{item.get('name', isin)}: {isin} -> {pick}")
            cache[isin] = pick
            item["ticker"] = pick
        else:
            log(f"Kein €-Kurs für {item.get('name', isin)} ({isin}) gefunden – bitte 'ticker' in depot.yaml eintragen")


_FX: dict[str, float | None] = {}


def fx_rate(cur: str) -> float | None:
    if cur not in _FX:
        import yfinance as yf
        try:
            _FX[cur] = float(yf.Ticker(f"EUR{cur}=X").history(period="5d")["Close"].dropna().iloc[-1])
        except Exception as e:  # noqa: BLE001
            log(f"Wechselkurs EUR/{cur} nicht abrufbar: {e}")
            _FX[cur] = None
    return _FX[cur]


def live_overlay(depot: dict, quotes: dict) -> None:
    """Ersetzt Kurs und Vortageskurs durch den Börsenplatz des Planspiels (Stuttgart), falls verfügbar."""
    import yfinance as yf

    today = datetime.now(BERLIN).date()
    for item in all_items(depot):
        t = item.get("ticker")
        q = quotes.get(t)
        if not q:
            continue
        cands = item.get("kurs") or ([t[:-3] + ".SG"] if t.endswith(".DE") else [])
        for c in cands:
            if c == t:
                break  # Hauptkürzel ist schon drin
            try:
                h = yf.Ticker(c).history(period="5d", interval="1d")["Close"].dropna()
            except Exception:  # noqa: BLE001
                continue
            if not len(h) or (today - h.index[-1].date()).days > 4:
                continue
            q["price"] = float(h.iloc[-1])
            if len(h) >= 2:
                q["prev_close"] = float(h.iloc[-2])
            q["closes"][-1] = q["price"]
            q["quelle"] = c
            break


def cost_basis(item: dict) -> float:
    """Kaufwert inkl. Kaufgebühr – so rechnet das Planspiel „seit Kauf“."""
    n = float(item.get("stueck", 0))
    k = float(item.get("kaufkurs", 0))
    f = item.get("gebuehr_kauf")
    return n * k + (float(f) if f is not None else fee(n * k, CFG))


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {price, prev_close, currency, closes}} aus einem Jahr Tageskursen."""
    import yfinance as yf

    quotes = {}
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            hist = tk.history(period="1y", interval="1d", auto_adjust=False)
            cl = hist["Close"].dropna()
            closes = [float(c) for c in cl.tolist()]
            dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else "" for d in cl.index]
            if len(closes) < 2:
                raise ValueError("zu wenige Kursdaten")
            try:
                currency = tk.fast_info.get("currency") or ""
            except Exception:  # noqa: BLE001
                currency = ""
            if currency and currency.upper() != "EUR":
                rate = fx_rate(currency.upper())  # 1 € = rate Fremdwährung
                if rate:
                    closes = [c / rate for c in closes]
                    currency = "EUR"
            quotes[t] = {"price": closes[-1], "prev_close": closes[-2],
                         "currency": "€" if currency.upper() == "EUR" else currency, "closes": closes,
                         "dates": dates}
        except Exception as e:  # noqa: BLE001 – Yahoo wirft alles Mögliche
            log(f"Kurs für {t} nicht abrufbar: {e}")
    return quotes


# ---------------------------------------------------------------- Gebühren & Endspurt

def fee(amount: float, cfg: dict) -> float:
    return max(float(cfg.get("gebuehr_min", 15)), amount * float(cfg.get("gebuehr_prozent", 0.3)) / 100)


def min_trade(cfg: dict) -> float:
    """Kleinster sinnvoller Kauf: Gebühren höchstens ~1 % des Betrags."""
    return float(cfg.get("gebuehr_min", 15)) / 0.01


def days_left(cfg: dict, now: datetime) -> int | None:
    end = cfg.get("spielende")
    if not end:
        return None
    return (datetime.fromisoformat(str(end)).date() - now.date()).days


def endspurt(cfg: dict, now: datetime | None = None) -> int | None:
    """Tage bis Spielende, wenn wir im Endspurt sind, sonst None."""
    d = days_left(cfg, now or datetime.now(BERLIN))
    return d if d is not None and 0 <= d <= int(cfg.get("endspurt_tage", 14)) else None


# ---------------------------------------------------------------- Charts (Bild über quickchart.io)

def chart_url(q: dict, days: int, title: str, lines: list[tuple[str, float, str]] = (), sma200: bool = False) -> str | None:
    """Liniendiagramm der letzten `days` Handelstage als Bild-URL. lines: [(Beschriftung, Wert, Farbe)]."""
    closes, dates = q.get("closes") or [], q.get("dates") or []
    if len(closes) < 10:
        return None
    n = min(days, len(closes))
    idx = list(range(len(closes) - n, len(closes)))
    step = max(1, n // 60)
    pick = idx[::step]
    if pick[-1] != idx[-1]:
        pick.append(idx[-1])

    def lab(i):
        d = dates[i] if i < len(dates) and dates[i] else ""
        return f"{d[8:10]}.{d[5:7]}.{d[2:4]}" if len(d) == 10 else ""

    ds = [{"label": "Kurs", "data": [round(closes[i], 2) for i in pick], "borderColor": "#2563eb",
           "fill": False, "pointRadius": 0, "borderWidth": 2}]
    if sma200 and len(closes) >= 200:
        ds.append({"label": "200-Tage-Schnitt", "data": [round(sum(closes[max(0, i - 199):i + 1]) / min(200, i + 1), 2)
                                                         if i >= 199 else None for i in pick],
                   "borderColor": "#9ca3af", "borderDash": [4, 4], "fill": False, "pointRadius": 0, "borderWidth": 1})
    for label, value, color in lines:
        if value:
            ds.append({"label": f"{label} {eur(value)}", "data": [round(value, 2)] * len(pick), "borderColor": color,
                       "borderDash": [6, 3], "fill": False, "pointRadius": 0, "borderWidth": 1.5})
    cfg = {"type": "line", "data": {"labels": [lab(i) for i in pick], "datasets": ds},
           "options": {"title": {"display": True, "text": title},
                       "legend": {"labels": {"boxWidth": 12, "fontSize": 11}},
                       "scales": {"xAxes": [{"ticks": {"maxTicksLimit": 6, "maxRotation": 0}}]}}}
    c = urllib.parse.quote(json.dumps(cfg, separators=(",", ":"), ensure_ascii=False))
    return f"https://quickchart.io/chart?w=640&h=340&bkg=white&f=png&c={c}"


def headline(item: dict, q: dict) -> str:
    """Eine Zeile: Kurs · heute · (seit Kauf)."""
    chg = (q["price"] / q["prev_close"] - 1) * 100
    out = f"{eur(q['price'])} € · heute {pct(chg)}"
    if item.get("stueck") and item.get("kaufkurs"):
        n = float(item["stueck"])
        diff = (q["price"] - q["prev_close"]) * n
        out += f" ({'+' if diff >= 0 else ''}{eur(diff)} €) · seit Kauf {pct((q['price'] * n / cost_basis(item) - 1) * 100)}"
    return out


# ---------------------------------------------------------------- Einschätzung

def analyse(closes: list[float]) -> dict:
    """Einfache Chart-Kennzahlen: gleitende Durchschnitte, RSI, Schwankung, 52-Wochen-Spanne."""
    def sma(n):
        return sum(closes[-n:]) / n if len(closes) >= n else None

    rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 60), len(closes))]
    mean = sum(rets) / len(rets) if rets else 0
    vol = (sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)) ** 0.5 * 252 ** 0.5 if rets else 0

    gains = losses = 0.0
    window = closes[-15:]
    for a, b in zip(window, window[1:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    rsi = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)

    hi, lo = max(closes[-252:]), min(closes[-252:])
    return {
        "sma50": sma(50), "sma200": sma(200), "rsi": rsi, "vol": vol,
        "hi": hi, "lo": lo, "pos52": (closes[-1] - lo) / (hi - lo) if hi > lo else 0.5,
        "perf1m": closes[-1] / closes[-22] - 1 if len(closes) >= 22 else None,
    }


def assessment(item: dict, q: dict, depot: dict, allow_buy: bool = True) -> str:
    """Kompakte, regelbasierte Einschätzung (Trend, Signal, Kauf, Stop, ein Satz Warum). Keine Anlageberatung."""
    a = analyse(q["closes"])
    p = q["price"]
    cfg = depot.get("einstellungen") or {}
    budget = float(depot.get("budget") or 0)
    is_fund = item.get("art") in ("etf", "fonds")
    up_long = a["sma200"] is not None and p > a["sma200"] and (a["sma50"] or p) > a["sma200"]
    below_200 = a["sma200"] is not None and p < a["sma200"]
    d200 = (p / a["sma200"] - 1) * 100 if a["sma200"] else 0

    if is_fund:
        trend, t_why = "🕰 langfristig halten (Fonds)", "Breit gestreut, schwankt weniger"
    elif up_long:
        trend, t_why = "↗ aufwärts – eher halten", f"{d200:.0f} % über dem 200-Tage-Schnitt"
    elif below_200:
        trend, t_why = "↘ abwärts – spekulativ", f"{abs(d200):.0f} % unter dem 200-Tage-Schnitt"
    else:
        trend, t_why = "→ seitwärts – abwarten", "Kein klarer Trend"

    owned = bool(item.get("stueck"))
    buy_ok, s_why = True, ""
    if a["rsi"] >= 70:
        sig = "überkauft" + (" – Teilgewinn möglich" if owned else " – nicht hinterherkaufen")
        buy_ok, s_why = False, "14 Tage fast nur gestiegen, danach kommt oft ein Rücksetzer."
    elif a["rsi"] <= 30 and up_long:
        sig = "Rücksetzer – Nachkauf möglich"
        s_why = "Kurze Delle im Aufwärtstrend, die wurde oft wieder aufgeholt."
    elif a["rsi"] <= 30:
        sig = "stark gefallen – Vorsicht"
        buy_ok, s_why = False, "Gefallen ohne Aufwärtstrend als Stütze, kann weiter fallen."
    elif below_200:
        sig = "erst Bodenbildung abwarten"
        buy_ok, s_why = False, f"Interessant erst wieder über {eur(a['sma200'])} €."
    elif a["pos52"] >= 0.95:
        sig = "nahe Jahreshoch"
        s_why = "Trend stark, aber wenig Puffer nach unten."
    else:
        sig = "unauffällig"

    vol = a["vol"]
    share = 0.35 if vol < 0.25 else 0.25 if vol < 0.40 else 0.15 if vol < 0.60 else 0.08
    share = min(share, float(cfg.get("max_anteil_pro_kauf", 35)) / 100)
    stop = stop_level(q, item if owned else None)

    left = endspurt(cfg)
    if left is not None and buy_ok and (vol >= 0.35 or not up_long):
        buy_ok, s_why = False, "Zu wenig Zeit bis Spielende für riskante Käufe."
    if not allow_buy:
        buy_ok = False

    lines = []
    if left is not None:
        lines.append(f"🏁 Endspurt: noch {left} Tage")
    lines.append(f"Trend:  {trend}")
    lines.append(f"Signal: {sig}")
    if budget > 0 and allow_buy:
        n = int(budget * share // p)
        if buy_ok and n >= 1 and n * p >= min_trade(cfg):
            lines.append(f"Kauf:   bis ~{eur(n * p)} € ({n} Stk.)")
        elif buy_ok:
            lines.append("Kauf:   Budget zu klein (Gebühren)")
        else:
            lines.append("Kauf:   lieber nicht")
    if stop >= p:
        lines.append(f"Stop:   {eur(stop)} € – unterschritten!")
    else:
        lines.append(f"Stop:   {eur(stop)} € (−{(1 - stop / p) * 100:.0f} %)")
    lines.append(f"💡 {t_why}." + (f" {s_why}" if s_why else ""))
    return "\n".join(lines)


def short_horizon(item: dict, q: dict) -> str:
    a = analyse(q["closes"])
    if item.get("art") in ("etf", "fonds"):
        return "lang"
    if a["sma200"] is None:
        return "?"
    if q["price"] > a["sma200"]:
        return "überkauft" if a["rsi"] >= 70 else "lang"
    return "kurz"


DISCLAIMER = ""
IDEA_NOTE = "\nChart-Regeln, keine Anlageberatung."


def check_prices(depot: dict, quotes: dict, state: dict, today: str, dry_run: bool) -> None:
    cfg = depot.get("einstellungen", {})
    first = float(cfg.get("bewegung_prozent", 3))
    step = float(cfg.get("bewegung_schritt", 2))

    moves = state.setdefault("moves", {})
    if moves.get("_day") != today:
        moves.clear()
        moves["_day"] = today
    targets = state.setdefault("targets", {})

    for item in all_items(depot):
        t = item.get("ticker")
        q = quotes.get(t)
        if not q:
            continue
        name = item.get("name", t)
        chg = (q["price"] / q["prev_close"] - 1) * 100
        link = f"https://finance.yahoo.com/quote/{urllib.parse.quote(t)}"

        # 1) Starke Tagesbewegung: Stufen bei 3 %, 5 %, 7 % … (pro Richtung, pro Tag einmal)
        if abs(chg) >= first:
            level = int((abs(chg) - first) // step)
            key = f"{t}:{'up' if chg > 0 else 'down'}"
            if level > moves.get(key, -1):
                moves[key] = level
                own = item.get("stueck")
                notify(f"{'📈' if chg > 0 else '📉'} {name} {pct(chg)}",
                       headline(item, q) + "\n\n" + assessment(item, q, depot),
                       priority=4 if abs(chg) >= first + step else 3,
                       tags=["chart_with_upwards_trend" if chg > 0 else "chart_with_downwards_trend"],
                       attach=chart_url(q, 63, f"{name} – 3 Monate",
                                        [("Kauf", float(item["kaufkurs"]) if own and item.get("kaufkurs") else 0, "#16a34a"),
                                         ("Stop", stop_level(q, item) if own else 0, "#dc2626")]),
                       click=link, dry_run=dry_run)

        # 2) Selbst gesetzte Kursmarken (Alarm beim Reißen, wieder scharf beim Zurückkommen)
        for kind, cond in (("ueber", lambda p, m: p >= m), ("unter", lambda p, m: p <= m)):
            mark = item.get(kind)
            if mark is None:
                continue
            key = f"{t}:{kind}:{mark}"
            hit = cond(q["price"], float(mark))
            if hit and not targets.get(key):
                is_goal = kind == "ueber" and item.get("ziel_prozent")
                intro = (f"Ziel +{item['ziel_prozent']} % erreicht. Im Backtest war Verkaufen hier besser als Halten.\n"
                         if is_goal else "")
                own = item.get("stueck")
                notify(f"🎯 {name} {'über' if kind == 'ueber' else 'unter'} {eur(float(mark))}"
                       + (" – Gewinnziel!" if is_goal else ""),
                       intro + headline(item, q) + "\n\n" + assessment(item, q, depot, allow_buy=not is_goal),
                       attach=chart_url(q, 126, f"{name} – 6 Monate",
                                        [("Kauf", float(item["kaufkurs"]) if own and item.get("kaufkurs") else 0, "#16a34a"),
                                         ("Marke", float(mark), "#f59e0b")]),
                       priority=5 if kind == "unter" or is_goal else 4, tags=["dart"],
                       actions=([("Alles verkauft", f"verkauf {t} alle"),
                                 ("Hälfte verkauft", f"verkauf {t} {max(1, int(float(own) // 2))}")] if own else None),
                       click=link, dry_run=dry_run)
            targets[key] = hit


def all_items(depot: dict) -> list[dict]:
    return list(depot.get("positionen") or []) + list(depot.get("watchlist") or [])


# ---------------------------------------------------------------- Klumpenrisiko (Branchen)

SECTOR_DE = {"Technology": "Tech", "Communication Services": "Tech", "Financial Services": "Finanzen",
             "Healthcare": "Gesundheit", "Industrials": "Industrie", "Consumer Cyclical": "Konsum",
             "Consumer Defensive": "Konsum", "Energy": "Energie", "Utilities": "Energie",
             "Basic Materials": "Rohstoffe", "Real Estate": "Immobilien"}


def sector_lookup(ticker: str, state: dict) -> str | None:
    """Branche über Yahoo, im Zustand zwischengespeichert (ändert sich ja nicht)."""
    cache = state.setdefault("sectors", {})
    if ticker in cache:
        return cache[ticker]
    try:
        import yfinance as yf
        sec = SECTOR_DE.get(yf.Ticker(ticker).info.get("sector") or "")
    except Exception:  # noqa: BLE001
        return None
    cache[ticker] = sec
    return sec


def sector_of(item: dict, state: dict) -> str:
    if item.get("sektor"):
        return str(item["sektor"])
    if item.get("art") in ("etf", "fonds"):
        return "Gemischt"
    t = item.get("heimat") or item.get("ticker")
    return (sector_lookup(t, state) if t else None) or "Sonstige"


def sector_weights(depot: dict, quotes: dict, state: dict) -> dict[str, float]:
    """{Branche: Anteil am Aktien-/Fondswert des Depots}"""
    vals: dict[str, float] = {}
    for p in depot.get("positionen") or []:
        q = quotes.get(p.get("ticker"))
        if q:
            sec = sector_of(p, state)
            vals[sec] = vals.get(sec, 0.0) + q["price"] * float(p.get("stueck", 0))
    total = sum(vals.values())
    return {k: v / total for k, v in sorted(vals.items(), key=lambda x: -x[1])} if total else {}


def check_sector_risk(depot: dict, quotes: dict, state: dict, dry_run: bool) -> None:
    """Warnt einmal, wenn eine Branche zu groß wird, und erneut erst bei weiteren +10 Prozentpunkten."""
    limit = float(CFG.get("max_sektor_prozent", 50)) / 100
    w = sector_weights(depot, quotes, state)
    warned = state.setdefault("sector_warned", {})
    for sec, share in w.items():
        if share <= limit - 0.05:
            warned.pop(sec, None)  # wieder klar unter der Grenze: zurücksetzen
        if share > limit and share >= warned.get(sec, 0) + 0.10 and sec != "Gemischt":
            warned[sec] = share
            others = ", ".join(f"{k} {v * 100:.0f} %" for k, v in w.items() if k != sec) or "–"
            notify(f"⚠️ Klumpenrisiko: {sec[:1].upper() + sec[1:]} {share * 100:.0f} %",
                   f"{share * 100:.0f} % eures Depots stecken in {sec}. Fällt die Branche an einem Tag, "
                   f"trifft es fast alles gleichzeitig.\nRest: {others}\n\n"
                   f"💡 Neue Kaufideen aus {sec} bekommen ab jetzt einen Abzug, andere Branchen werden bevorzugt. "
                   "Bei Gewinnmitnahmen eher hier verkaufen.",
                   priority=3, tags=["warning"], dry_run=dry_run)


# ---------------------------------------------------------------- Übersicht

def summary_text(depot: dict, quotes: dict) -> str:
    lines, total_value, total_cost, day_diff, weights = [], 0.0, 0.0, 0.0, []
    for p in depot.get("positionen") or []:
        q = quotes.get(p.get("ticker"))
        name = p.get("name", p.get("ticker") or p.get("isin"))
        if not q:
            lines.append(f"{name}: kein Kurs")
            continue
        n = float(p.get("stueck", 0))
        value = q["price"] * n
        cost = cost_basis(p) if p.get("kaufkurs") else q["price"] * n
        total_value += value
        total_cost += cost
        day_diff += (q["price"] - q["prev_close"]) * n
        perf = (value / cost - 1) * 100 if cost else 0
        lines.append(f"{name}: {eur(q['price'])} € · {pct((q['price'] / q['prev_close'] - 1) * 100)} · "
                     f"ges. {pct(perf)}")
        weights.append((value, name))
    if total_cost:
        lines.append("")
        lines.insert(0, f"Aktien: {eur(total_value)} € · heute {'+' if day_diff >= 0 else ''}{eur(day_diff)} € · "
                        f"gesamt {pct((total_value / total_cost - 1) * 100)}\n")
        big = [f"{n} {v / total_value * 100:.0f} %" for v, n in sorted(weights, reverse=True) if v / total_value > 0.2]
        if big:
            lines.append("⚠️ Klumpenrisiko: " + ", ".join(big) + " vom Depot")
        sw = sector_weights(depot, quotes, STATE)
        if sw:
            lines.append("Branchen: " + " · ".join(f"{k} {v * 100:.0f} %" for k, v in sw.items()))
    budget = float(depot.get("budget") or 0)
    if budget:
        lines.append(f"Freies Bot-Budget: {eur(budget)} €")
    watch = []
    for w in depot.get("watchlist") or []:
        q = quotes.get(w.get("ticker"))
        if q:
            watch.append(f"{w.get('name', w.get('ticker'))} {pct((q['price'] / q['prev_close'] - 1) * 100)}")
    if watch:
        lines.append("Watchlist: " + ", ".join(watch))
    if depot.get("_termine"):
        lines.append("🗓 Zahlen in den nächsten 7 Tagen: " + ", ".join(depot["_termine"]))
    return "\n".join(lines)


def maybe_summary(depot: dict, quotes: dict, state: dict, now: datetime, dry_run: bool) -> None:
    times = (depot.get("einstellungen") or {}).get("uebersicht_uhrzeiten") or []
    sent = state.setdefault("summaries", {})
    today = now.strftime("%Y-%m-%d")
    for hhmm in times:
        h, m = map(int, str(hhmm).split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        key = f"{today} {hhmm}"
        if slot <= now < slot + timedelta(hours=2) and key not in sent:
            notify(f"📊 Depot-Übersicht {hhmm}", summary_text(depot, quotes),
                   priority=2, tags=["bar_chart"], dry_run=dry_run)
            sent[key] = True
    # alte Einträge aufräumen
    for k in [k for k in sent if not k.startswith(today)]:
        del sent[k]


# ---------------------------------------------------------------- News

def news_queries(depot: dict) -> list[tuple[str, str]]:
    """(Anzeigename, Suchanfrage)"""
    out = []
    for item in all_items(depot):
        term = item.get("news") or item.get("name")
        if term:
            out.append((item.get("name", term), f'"{term}" (Aktie OR Börse OR Kurs OR Quartal OR Prognose)', item))
    for term in depot.get("markt_news") or []:
        out.append((term, f'{term} Börse', None))
    return out


# Werbung für Hebelprodukte u. Ä. rausfiltern
NEWS_JUNK = ("turbo", "optionsschein", "knock-out", "open end", "faktor-zertifikat", "mini future", "discount-zertifikat")


def google_news_url(query: str) -> str:
    q = urllib.parse.quote(f"{query} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de"


def check_news(depot: dict, quotes: dict, state: dict, now: datetime, dry_run: bool) -> None:
    cfg = depot.get("einstellungen") or {}
    max_age = timedelta(hours=float(cfg.get("news_max_alter_stunden", 6)))
    budget = int(cfg.get("max_news_pro_lauf", 5))
    seen: dict = state.setdefault("news_seen", {})
    seeded: list = state.setdefault("news_seeded", [])

    fresh = []
    for label, query, item in news_queries(depot):
        try:
            feed = feedparser.parse(google_news_url(query))
        except Exception as e:  # noqa: BLE001
            log(f"News für {label} fehlgeschlagen: {e}")
            continue
        first_time = query not in seeded
        for e in feed.entries:
            title = e.get("title", "").strip()
            # Titel ohne Quelle (" - Handelsblatt") als Schlüssel, damit dieselbe Meldung
            # von mehreren Seiten nur einmal kommt
            core = title.rsplit(" - ", 1)[0].lower()
            key = hashlib.sha1(core.encode()).hexdigest()[:16]
            if key in seen or any(w in core for w in NEWS_JUNK):
                continue
            seen[key] = now.timestamp()
            if first_time:
                continue  # beim ersten Mal nur merken, sonst kommt eine Flut alter Artikel
            pub = e.get("published_parsed")
            if pub and now - datetime(*pub[:6], tzinfo=timezone.utc) > max_age:
                continue
            fresh.append((label, title, e.get("link"), item))
        if first_time:
            seeded.append(query)

    ratings = ki_rate(depot, fresh) if fresh else None
    recent = state.setdefault("recent_news", {})
    for n, (label, title, link, item) in enumerate(fresh):
        r = ratings.get(n) if ratings else None
        recent.setdefault(label, []).append({"t": now.timestamp(), "title": title.rsplit(" - ", 1)[0],
                                             **({k: r[k] for k in ("relevanz", "richtung", "einschaetzung")} if r else {})})
    for label in list(recent):
        recent[label] = [x for x in recent[label] if x["t"] > now.timestamp() - 86400][-5:]
        if not recent[label]:
            del recent[label]
    if ratings is not None:
        min_rel = int(cfg.get("ki_min_relevanz", 3))
        kept = []
        for i, f in enumerate(fresh):
            r = ratings.get(i)
            if r and r["relevanz"] >= min_rel:
                kept.append((r["relevanz"], f, r))
        log(f"KI: {len(kept)}/{len(fresh)} Meldungen relevant")
        kept.sort(key=lambda x: -x[0])
        fresh = [f for _, f, _ in kept]
        ratings = {id(f): r for _, f, r in kept}

    for f in fresh[:budget]:
        label, title, link, item = f
        msg = title
        prio = 3
        r = ratings.get(id(f)) if ratings else None
        if r:
            icon = {"positiv": "🟢", "negativ": "🔴"}.get(r["richtung"], "⚪")
            msg += f"\n🤖 {icon} {r['richtung']} · Relevanz {r['relevanz']}/5: {r['einschaetzung']}"
            prio = 4 if r["relevanz"] >= 5 else 3
        q = quotes.get(item.get("ticker")) if item else None
        if q:
            msg += f"\n\n{headline(item, q)}\n" + "\n".join(assessment(item, q, depot).split("\n")[:2])
        notify(f"📰 {label}", msg, priority=prio, tags=["newspaper"], click=link, dry_run=dry_run)
    if len(fresh) > budget:
        notify("📰 Weitere News", f"{len(fresh) - budget} weitere Meldungen, z. B.: "
               + "; ".join(f[1] for f in fresh[budget:budget + 3]),
               priority=2, tags=["newspaper"], dry_run=dry_run)

    # Gemerkte News nach 3 Tagen vergessen
    cutoff = now.timestamp() - 3 * 86400
    for k in [k for k, ts in seen.items() if ts < cutoff]:
        del seen[k]


# ---------------------------------------------------------------- KI-Bewertung (optional)

KI_SYSTEM = (
    "Du bewertest Börsen-Schlagzeilen für ein Schüler-Team im Planspiel Börse (Spielgeld, läuft bis Januar). "
    "Für jede Schlagzeile: Wie relevant ist sie für den Kurs der genannten Aktie bzw. den Markt in den nächsten "
    "Tagen bis Wochen (1 = Werbung/Rauschen/Wiederholung, 3 = lesenswert, 5 = kann den Kurs deutlich bewegen, "
    "z. B. Quartalszahlen, Prognoseänderung, Übernahme, Ermittlungen, Großauftrag)? Richtung: positiv, negativ "
    "oder neutral für die Aktie. Einschätzung: ein kurzer deutscher Satz (max. 120 Zeichen), was das für das Team "
    "bedeutet. Sei nüchtern, keine Garantien."
)

KI_SCHEMA = {
    "type": "object",
    "properties": {
        "bewertungen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nr": {"type": "integer"},
                    "relevanz": {"type": "integer"},
                    "richtung": {"type": "string", "enum": ["positiv", "negativ", "neutral"]},
                    "einschaetzung": {"type": "string"},
                },
                "required": ["nr", "relevanz", "richtung", "einschaetzung"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["bewertungen"],
    "additionalProperties": False,
}


def ki_rate(depot: dict, fresh: list) -> dict[int, dict] | None:
    """Bewertet Schlagzeilen mit Claude. None = keine KI (kein Key oder Fehler) -> alles normal schicken."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log("Paket 'anthropic' fehlt, KI-Bewertung aus")
        return None

    held = {i.get("name") for i in depot.get("positionen") or []}
    lines = []
    for n, (label, title, _link, item) in enumerate(fresh):
        tag = "im Depot" if label in held else ("Watchlist" if item else "Gesamtmarkt")
        lines.append(f"{n}. [{label}, {tag}] {title}")
    model = (depot.get("einstellungen") or {}).get("ki_modell", "claude-haiku-5-5")
    try:
        client = anthropic.Anthropic(max_retries=2, timeout=60.0)
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=KI_SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": KI_SCHEMA}},
            messages=[{"role": "user", "content": "Schlagzeilen:\n" + "\n".join(lines)}],
        )
    except anthropic.APIStatusError as e:
        log(f"KI-Fehler {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError as e:
        log(f"KI nicht erreichbar: {e}")
        return None
    if resp.stop_reason != "end_turn":
        log(f"KI-Antwort unvollständig ({resp.stop_reason})")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)["bewertungen"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log("KI-Antwort nicht lesbar")
        return None
    log(f"KI: {resp.usage.input_tokens} Eingabe-, {resp.usage.output_tokens} Ausgabe-Tokens")
    out = {}
    for r in data:
        if isinstance(r.get("nr"), int) and 0 <= r["nr"] < len(fresh):
            r["relevanz"] = max(1, min(5, int(r.get("relevanz", 3))))
            r["einschaetzung"] = str(r.get("einschaetzung", ""))[:160]
            out[r["nr"]] = r
    return out


# ---------------------------------------------------------------- Quartalszahlen

def check_earnings(depot: dict, state: dict, now: datetime, dry_run: bool) -> list[str]:
    """Warnt einmal am Vortag (freitags auch fürs Wochenende/Montag) vor Quartalszahlen.
    Gibt Termine der nächsten 7 Tage für die Übersicht zurück."""
    import yfinance as yf

    cache = state.setdefault("earnings", {})
    warned = state.setdefault("earnings_warned", [])
    today = now.date()
    upcoming = []
    for item in all_items(depot):
        if item.get("art") in ("etf", "fonds"):
            continue
        sym = item.get("heimat") or item.get("ticker")
        if not sym:
            continue
        c = cache.get(sym)
        if not c or c.get("abgerufen") != today.isoformat():
            date = None
            try:
                cal = yf.Ticker(sym).calendar or {}
                dates = cal.get("Earnings Date") or []
                future = sorted(d for d in dates if d >= today)
                date = future[0].isoformat() if future else None
            except Exception as e:  # noqa: BLE001
                log(f"Zahlen-Termin {sym} nicht abrufbar: {e}")
            c = cache[sym] = {"datum": date, "abgerufen": today.isoformat()}
        if not c.get("datum"):
            continue
        d = datetime.fromisoformat(c["datum"]).date()
        days = (d - today).days
        if 0 <= days <= 7:
            upcoming.append(f"{item.get('name', sym)} {d:%d.%m.}")
        lookahead = 3 if today.weekday() == 4 else 1
        key = f"{sym}:{c['datum']}"
        if 1 <= days <= lookahead and key not in warned:
            when = "morgen" if days == 1 else f"am {['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][d.weekday()]} {d:%d.%m.}"
            own = item.get("stueck")
            notify(f"🗓 {item.get('name', sym)}: Quartalszahlen {when}",
                   ("Ihr haltet " + str(own) + " Stück. " if own else "")
                   + "An Zahlen-Tagen sind Sprünge von ±10 % normal. Vorher überlegen: halten, Teil verkaufen "
                   "oder Stop-Loss setzen? US-Firmen melden meist nach US-Börsenschluss (nachts bei uns), "
                   "die Reaktion kommt dann am nächsten Morgen.",
                   priority=4, tags=["calendar"], dry_run=dry_run)
            warned.append(key)
    del warned[:-100]
    return upcoming


# ---------------------------------------------------------------- Auto-Stop-Loss

CFG: dict = {}
DEPOT: dict = {}  # aktuelles Depot (für den Verkaufen-Knopf)


def _num_or(x):
    try:
        f = float(x)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return 0
STATE: dict = {}        # Zustand dieses Laufs (für Branchen-Cache)
LAST_QUOTES: dict = {}  # Kurse dieses Laufs


def stop_level(q: dict, item: dict | None = None) -> float:
    """Nachgezogener Stop: höchster Kurs seit dem Kauf (mindestens Kaufkurs) minus schwankungsabhängiger
    Abstand. Ohne Kaufdaten (z. B. bei Ideen) vom aktuellen Kurs aus. Im Endspurt halb so viel Abstand."""
    a = analyse(q["closes"])
    stop_pct = min(max(a["vol"] * 0.35, 0.05), 0.25)
    if endspurt(CFG) is not None:
        stop_pct = max(0.03, stop_pct * 0.5)
    high = q["price"]
    if item and item.get("kaufkurs"):
        since = str(item.get("kaufdatum") or "")
        after = [c for c, d in zip(q["closes"], q.get("dates") or []) if since and d >= since]
        high = max([float(item["kaufkurs"])] + after + [q["price"]])
    return high * (1 - stop_pct)


def check_auto_stops(depot: dict, quotes: dict, state: dict, dry_run: bool) -> None:
    if not (depot.get("einstellungen") or {}).get("auto_stop", True):
        return
    armed = state.setdefault("auto_stop_hit", {})
    for item in depot.get("positionen") or []:
        t = item.get("ticker")
        q = quotes.get(t)
        if not q or item.get("unter") is not None:
            continue  # eigene Marke hat Vorrang
        stop = stop_level(q, item)
        if q["price"] <= stop and not armed.get(t):
            notify(f"🛑 {item.get('name', t)} unter Stop {eur(stop)}",
                   headline(item, q) + "\nVerkaufen, Teil verkaufen oder bewusst halten?\n\n"
                   + assessment(item, q, depot, allow_buy=False),
                   priority=5, tags=["octagonal_sign"],
                   attach=chart_url(q, 126, f"{item.get('name', t)} – 6 Monate",
                                    [("Kauf", float(item.get("kaufkurs") or 0), "#16a34a"), ("Stop", stop, "#dc2626")]),
                   actions=[("Alles verkauft", f"verkauf {t} alle"),
                            ("Hälfte verkauft", f"verkauf {t} {max(1, int(float(item.get('stueck', 2)) // 2))}")],
                   click=f"https://finance.yahoo.com/quote/{urllib.parse.quote(t)}", dry_run=dry_run)
            armed[t] = True
        elif q["price"] > stop * 1.02:
            armed[t] = False


# ---------------------------------------------------------------- Backtest-Statistik

STATS_FILE = ROOT / "stats.json"


def load_stats() -> dict:
    try:
        return json.loads(STATS_FILE.read_text()).get("gruppen", {})
    except (OSError, json.JSONDecodeError):
        return {}


def idea_odds(idea: dict, stats: dict) -> dict | None:
    vb = "ruhig" if idea["vol"] < 0.25 else "mittel" if idea["vol"] < 0.40 else "wild"
    for label in (("stark", "alle") if idea["score"] >= 25 else ("alle",)):
        g = stats.get(f"{idea['kind']}|{vb}|{label}")
        if g:
            return g
    return None


def odds_text(g: dict, price: float) -> str:
    best = g["best"]
    t = g["targets"][str(best)]
    if not best:
        chances = " · ".join(f"+{x} %: {g['targets'][str(x)]['p_ziel'] * 100:.0f} %"
                             for x in (5, 10, 15, 20) if str(x) in g["targets"])
        return (f"📊 {g['n']} ähnliche Signale der letzten 2 Jahre (bis 40 Handelstage gehalten):\n"
                f"   Chance, vor dem Stop zu erreichen: {chances}\n"
                f"   👉 Kein festes Verkaufsziel: Am besten lief es, Gewinne laufen zu lassen und nur den Stop "
                f"nachzuziehen (Stop in {t['p_stop'] * 100:.0f} % der Fälle). Ø {pct(t['ev'] * 100)} pro Trade nach Gebühren")
    chances = " · ".join(f"+{x} %: {g['targets'][str(x)]['p_ziel'] * 100:.0f} %"
                         for x in (5, 10, 15, 20) if str(x) in g["targets"])
    return (f"📊 {g['n']} ähnliche Signale der letzten 2 Jahre (bis 40 Handelstage gehalten):\n"
            f"   Chance, vor dem Stop zu erreichen: {chances}\n"
            f"   👉 Verkaufsziel +{best} % (~{eur(price * (1 + best / 100))} €): erreicht in {t['p_ziel'] * 100:.0f} %, "
            f"Stop in {t['p_stop'] * 100:.0f} %, sonst Verkauf nach 40 Tagen. "
            f"Ø {pct(t['ev'] * 100)} pro Trade nach Gebühren")


# ---------------------------------------------------------------- Kaufideen-Scan

def maybe_scan(depot: dict, state: dict, now: datetime, dry_run: bool, force: bool = False,
               example: str | None = None) -> None:
    """Scannt zu festen Zeiten, meldet sich aber NUR bei wirklich starken, neuen Kaufideen."""
    cfg = depot.get("einstellungen") or {}
    times = cfg.get("scan_uhrzeiten") or ([cfg["scan_uhrzeit"]] if cfg.get("scan_uhrzeit") else [])
    today = now.strftime("%Y-%m-%d")
    done = state.setdefault("scans_done", [])
    due = None
    for hhmm in times:
        h, m = map(int, str(hhmm).split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if slot <= now < slot + timedelta(hours=2) and f"{today} {hhmm}" not in done:
            due = f"{today} {hhmm}"
    test_n = None
    if example and ":" in example:  # "DTE.DE:1" = Test-Idee mit fester Stückzahl und echtem Gekauft-Knopf
        example, n_s = example.split(":", 1)
        test_n = max(1, int(n_s))
    if not force and not due and not example:
        return
    if due:
        done.append(due)
        del done[:-20]
    import scanner

    uni = state.get("universe")
    if not uni or uni.get("week") != now.strftime("%G-%V"):
        loaded = scanner.load_universe(log)
        uni = {"week": now.strftime("%G-%V"), "items": {t: list(v) for t, v in loaded.items()}}
        state["universe"] = uni
    universe = {t: tuple(v) for t, v in uni["items"].items()}
    if example:
        universe = {example: universe.get(example, (example, "Beispiel"))}
    held = {i.get("ticker") for i in all_items(depot)} | {i.get("heimat") for i in all_items(depot)}
    budget = float(depot.get("budget") or 0)
    ideas = scanner.scan(universe, held, analyse, fx_rate, budget,
                         float(cfg.get("max_anteil_pro_kauf", 35)) / 100, log, 20, force=bool(example))
    min_score = float(cfg.get("scan_min_score", 30))
    sent = state.setdefault("ideas_sent", {})
    for t in [t for t, d in sent.items() if (now.date() - datetime.fromisoformat(d).date()).days > 7]:
        del sent[t]
    left = endspurt(cfg, now)
    if left is not None:
        if left <= 5:
            log("Endspurt: keine Neukäufe mehr")
            return
        min_score += 5
        ideas = [i for i in ideas if i["vol"] < 0.35]
    raw = list(ideas)
    ideas = [i for i in ideas if i["n"] * i["price"] >= min_trade(cfg) and i["t"] not in sent]
    # Klumpenrisiko: Ideen aus schon übergewichteten Branchen abwerten, andere bevorzugen
    weights = sector_weights(depot, LAST_QUOTES, state)
    limit = float(cfg.get("max_sektor_prozent", 50)) / 100
    for i in ideas:
        if i["score"] < min_score - 15:
            continue
        i["sektor"] = sector_lookup(i["t"], state) or "Sonstige"
        share = weights.get(i["sektor"], 0)
        if share > limit:
            i["score"] -= 15
            i["klumpen"] = share
        elif share < 0.15:
            i["score"] += 3  # neue Branche = breiter aufgestellt
    ideas.sort(key=lambda x: -x["score"])
    if example:
        ideas = [i for i in sorted(raw, key=lambda x: -x["score"])][:1]
        for i in ideas:
            i["sektor"] = sector_lookup(i["t"], state)
            if weights.get(i["sektor"] or "", 0) > limit:
                i["klumpen"] = weights[i["sektor"]]
    else:
        ideas = [i for i in ideas if i["score"] >= min_score][:int(cfg.get("scan_anzahl", 2))]
    log(f"Scan: {len(ideas)} starke neue Ideen (Schwelle {min_score})")
    if not ideas:
        return  # keine Mitteilung – nur melden, wenn es wirklich etwas Gutes gibt
    for i in ideas if not example else []:
        sent[i["t"]] = today
    stats = load_stats()
    for i in ideas:
        if test_n:
            i["n"] = test_n
        if example and i["kind"].startswith("Kein Signal"):
            i["kind"] = "Rücksetzer im Aufwärtstrend"  # nur zur Veranschaulichung, Bild sagt "Beispielzahlen"
        i["odds"] = idea_odds(i, stats)
    if not example or test_n:
      state["last_ideas"] = ({**(state.get("last_ideas") or {})} if test_n else {}) | {i["t"]: {"name": i["name"], "price": i["price"],
                                    **({"ziel_prozent": i["odds"]["best"]} if i.get("odds") and i["odds"]["best"] else {}),
                                    **({"sektor": i["sektor"]} if i.get("sektor") else {}),
                                    **({"heimat": i["t"]} if "." not in i["t"] else {})} for i in ideas}
    for i in ideas:
        import ideacard
        g = i.get("odds")
        best = g["best"] if g else None
        dauer, dauer2 = ideacard.horizon(i["kind"])
        body = [f"{i['idx']}" + (f" · {i['sektor']}" if i.get("sektor") else "") + f" · {i['kind']}",
                f"Dauer:  {dauer} ({dauer2})",
                f"Kauf:   {i['n']} Stk. ≈ {eur(i['n'] * i['price'])} €" if i["n"] >= 1
                else f"Kurs:   {eur(i['price'])} € (Budget zu klein)",
                f"Ziel:   +{best} % (~{eur(i['price'] * (1 + best / 100))} €)" if best
                else "Ziel:   keins, Gewinne laufen lassen",
                f"Stop:   {eur(i['stop'])} € (−{i['stop_pct'] * 100:.0f} %)"]
        if g:
            t = g["targets"][str(best or 0)]
            body.append(f"Chance +10 %: {g['targets']['10']['p_ziel'] * 100:.0f} % · Stop: {t['p_stop'] * 100:.0f} %"
                        if "10" in g["targets"] else f"Stop gerissen: {t['p_stop'] * 100:.0f} %")
        body.append("")
        body.append(f"💡 {i['why'].split('. ')[0].rstrip('.')}.")
        if i.get("klumpen"):
            body.append(f"⚠️ Ihr habt schon {i['klumpen'] * 100:.0f} % {i['sektor']}, das erhöht das Klumpenrisiko.")
        body.append(IDEA_NOTE.strip())
        btn = [(f"Gekauft: {i['n']}× {i['name']}", f"kauf {i['t']} {i['n']}")] if i["n"] >= 1 and (test_n or not example) else None
        try:
            img = ideacard.render(i, g, beispiel=bool(example))
        except Exception as e:  # noqa: BLE001
            log(f"Ideen-Bild fehlgeschlagen: {e}")
            img = None
        notify(f"{'🧪 TEST – ' if test_n else '🧪 BEISPIEL – ' if example else '💡 '}Kaufidee: {i['name']}", "\n".join(body), priority=4, tags=["bulb"], actions=btn,
               image=img,
               attach=None if img else chart_url({"closes": i["closes"], "dates": i["dates"]}, 504, f"{i['name']} – 2 Jahre",
                                                 [("Stop", i["stop"], "#dc2626")], sma200=True),
               click=f"https://finance.yahoo.com/quote/{urllib.parse.quote(i['t'])}", dry_run=dry_run)


# ---------------------------------------------------------------- Bot-Kanal

def bot_update(depot: dict, quotes: dict, state: dict, now: datetime, dry_run: bool) -> None:
    """Prüft alle 15 Min. die Bot-Käufe, meldet sich aber nur, wenn sich die Empfehlung ändert oder starke News kommen."""
    bot_pos = [p for p in depot.get("positionen") or [] if p.get("bot")]
    if not bot_pos:
        return
    recent = state.get("recent_news", {})
    total_v = total_c = total_d = 0.0
    blocks = []
    for p in bot_pos:
        q = quotes.get(p.get("ticker"))
        name = p.get("name", p.get("ticker"))
        if not q:
            blocks.append(f"{name}: kein Kurs")
            continue
        n, k = float(p.get("stueck", 0)), float(p.get("kaufkurs", q["price"]))
        v, c, d = n * q["price"], cost_basis(p), n * (q["price"] - q["prev_close"])
        total_v, total_c, total_d = total_v + v, total_c + c, total_d + d
        a = analyse(q["closes"])
        stop = float(p["unter"]) if p.get("unter") is not None else stop_level(q, p)
        up = a["sma200"] is not None and q["price"] > a["sma200"]
        if q["price"] <= stop:
            rec = "🛑 Unter Stop – Verkauf prüfen"
        elif a["rsi"] >= 70:
            rec = "🟡 Halten, überkauft – Teilgewinn möglich, nicht nachkaufen"
        elif a["rsi"] <= 35 and up:
            rec = "🟢 Rücksetzer im Aufwärtstrend – Nachkauf möglich"
        elif not up:
            rec = "🟠 Halten mit Stop, kein Nachkauf (Abwärtstrend)"
        else:
            rec = "⚪ Halten"
        news = recent.get(name, [])
        strong = [x for x in news if x.get("relevanz", 0) >= 4]
        if any(x.get("richtung") == "negativ" for x in strong):
            rec = "⚠️ Wichtige negative News – lesen, ggf. Stop enger setzen"
        elif any(x.get("richtung") == "positiv" for x in strong) and not rec.startswith(("🛑", "🟡")):
            rec += " · 📰 starke positive News"
        last_rec = state.setdefault("bot_rec", {})
        news_seen_t = state.setdefault("bot_news_t", {})
        new_strong = [x for x in strong if x["t"] > news_seen_t.get(name, 0)]
        rec_key = rec.split(" ")[0] + rec.split("·")[0]
        changed = last_rec.get(p.get("ticker")) != rec_key or bool(new_strong)
        last_rec[p.get("ticker")] = rec_key
        if new_strong:
            news_seen_t[name] = max(x["t"] for x in new_strong)
        if not changed:
            continue
        line = (f"{name}: {eur(q['price'])} € ({pct((q['price'] / q['prev_close'] - 1) * 100)} heute, "
                f"{pct((v / c - 1) * 100)} seit Kauf = {'+' if v - c >= 0 else ''}{eur(v - c)} €)\n"
                f"   {rec} · Stop {eur(stop)}")
        for x in sorted(news, key=lambda x: (-x.get("relevanz", 0), -x["t"]))[:2]:
            tag = f"🤖{x['relevanz']}/5 {x['richtung']}: " if x.get("relevanz") else ""
            line += f"\n   📰 {tag}{x['title'][:110]}"
        blocks.append(line)
    if not blocks:
        log("Bot-Käufe gecheckt, nichts Neues")
        return
    head = (f"Bot-Depot: {eur(total_v)} € · heute {'+' if total_d >= 0 else ''}{eur(total_d)} € · "
            f"gesamt {pct((total_v / total_c - 1) * 100) if total_c else '–'}"
            f" · freies Budget {eur(float(depot.get('budget') or 0))} €")
    notify(f"🤖 Bot-Käufe: Empfehlung geändert", head + "\n\n" + "\n\n".join(blocks) + DISCLAIMER,
           priority=3, tags=["robot"], dry_run=dry_run)


# ---------------------------------------------------------------- Wochenrückblick

def weekly_review(depot: dict, quotes: dict, state: dict, now: datetime, dry_run: bool, force: bool = False) -> None:
    """Sonntagabend: Woche des Depots gegen DAX und Nasdaq 100, Trades der Woche."""
    week = now.strftime("%G-%V")
    if not force and (now.weekday() != 6 or now.hour < 17 or state.get("review_week") == week):
        return
    state["review_week"] = week
    import yfinance as yf

    def wk(closes):
        return closes[-1] / closes[-6] - 1 if len(closes) >= 6 else None

    lines, val, diff = [], 0.0, 0.0
    perf = []
    for p in depot.get("positionen") or []:
        q = quotes.get(p.get("ticker"))
        if not q or len(q["closes"]) < 6:
            continue
        n = float(p.get("stueck", 0))
        d = n * (q["closes"][-1] - q["closes"][-6])
        val += n * q["price"]
        diff += d
        perf.append((wk(q["closes"]), d, p.get("name")))
    my = diff / (val - diff) if val - diff else 0
    bench = []
    for sym, name in (("^GDAXI", "DAX"), ("^NDX", "Nasdaq 100")):
        try:
            c = [float(x) for x in yf.Ticker(sym).history(period="1mo")["Close"].dropna().tolist()]
            bench.append((name, wk(c)))
        except Exception as e:  # noqa: BLE001
            log(f"Benchmark {name} fehlt: {e}")
    lines.append(f"Eure Aktien diese Woche: {pct(my * 100)} ({'+' if diff >= 0 else ''}{eur(diff)} €)")
    for name, b in bench:
        if b is not None:
            lines.append(f"{name}: {pct(b * 100)} → {'besser 💪' if my > b else 'schlechter'} als der Markt")
    perf.sort(key=lambda x: -(x[0] or 0))
    if perf:
        lines.append(f"\n🥇 Beste: {perf[0][2]} {pct(perf[0][0] * 100)}")
        lines.append(f"🥉 Schlechteste: {perf[-1][2]} {pct(perf[-1][0] * 100)}")
    trades = [t for t in state.get("trades", []) if t["t"] >= now.timestamp() - 7 * 86400]
    if trades:
        lines.append("\nTrades der Woche:")
        for t in trades:
            lines.append(f"• {t['text']}")
    start = float(depot.get("startkapital") or 0)
    cash = float(depot.get("barbestand") or 0)
    if start and cash:
        total = val + cash
        lines.append(f"\nGesamtdepot ca. {eur(total)} € ({pct((total / start - 1) * 100)} seit Start)"
                     "")
    left = days_left(depot.get("einstellungen") or {}, now)
    if left is not None and left >= 0:
        lines.append(f"🏁 Noch {left} Tage bis Spielende")
    notify("📅 Wochenrückblick", "\n".join(lines), priority=3, tags=["calendar"], dry_run=dry_run)


# ---------------------------------------------------------------- Main

def heartbeat(depot: dict, quotes: dict, state: dict, now: datetime, dry_run: bool) -> None:
    """Alle paar Stunden (7–23 Uhr) ein leises „Bot läuft“ mit Verkaufen-Knopf."""
    every = int(CFG.get("lebenszeichen_minuten", 30) or 0)
    if not every:
        return
    slot = now.strftime("%Y-%m-%d ") + str((now.hour * 60 + now.minute) // every)
    if state.get("heartbeat_slot") == slot:
        return
    state["heartbeat_slot"] = slot
    bot = [p for p in depot.get("positionen") or [] if p.get("bot")]
    lines = []
    for p in bot:
        q = quotes.get(p.get("ticker"))
        if q:
            lines.append(f"{p.get('name')}: {eur(q['price'])} € · seit Kauf "
                         f"{pct((q['price'] * float(p.get('stueck', 0)) / cost_basis(p) - 1) * 100)}")
    body = (("Bot-Käufe:\n" + "\n".join(lines)) if lines else "Keine offenen Bot-Käufe.")
    body += f"\nFreies Budget: {eur(float(depot.get('budget') or 0))} €"
    notify(f"🟢 Bot läuft · {now.strftime('%H:%M')}", body, priority=1, tags=["green_circle"], dry_run=dry_run)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Test-Mitteilung + Übersicht senden")
    ap.add_argument("--dry-run", action="store_true", help="nichts senden, nur anzeigen")
    ap.add_argument("--scan", action="store_true", help="Kaufideen-Scan sofort ausführen")
    ap.add_argument("--beispiel", metavar="TICKER", help="Beispiel-Kaufidee für eine Aktie schicken")
    ap.add_argument("--review", action="store_true", help="Wochenrückblick sofort schicken")
    args = ap.parse_args()

    now0 = datetime.now(BERLIN)
    if not (args.test or args.scan or args.review or args.beispiel or args.dry_run) and not (7 <= now0.hour < 23):
        log("Nachtruhe (23–7 Uhr), nichts zu tun.")
        return 0

    state = load_state()
    state.setdefault("sectors", {})
    STATE.clear()
    STATE.update(state)
    now = datetime.now(BERLIN)

    # Trades aus dem ntfy-Eingangskanal einbuchen
    want_status = []
    if os.environ.get("NTFY_TOPIC") and not args.dry_run:
        import depotbuch
        msgs = depotbuch.fetch_inbox(NTFY_SERVER, os.environ["NTFY_TOPIC"], state, log)
        if depotbuch.process(DEPOT_FILE, msgs, state, fetch_quotes, notify, lambda: want_status.append(1),
                             log, args.dry_run):
            log("depot.yaml aktualisiert")
    depot = yaml.safe_load(DEPOT_FILE.read_text()) or {}
    CFG.clear()
    CFG.update(depot.get("einstellungen") or {})
    DEPOT.clear()
    DEPOT.update(depot)
    resolve_symbols(depot, state)
    tickers = sorted({i["ticker"] for i in all_items(depot) if i.get("ticker")})

    log(f"Hole Kurse für {len(tickers)} Werte …")
    quotes = fetch_quotes(tickers)
    LAST_QUOTES.update(quotes)
    try:
        live_overlay(depot, quotes)
    except Exception as e:  # noqa: BLE001
        log(f"Stuttgart-Kurse nicht abrufbar: {e}")
    log(f"{len(quotes)}/{len(tickers)} Kurse erhalten")
    for p in depot.get("positionen") or []:
        q = quotes.get(p.get("ticker"))
        if q and p.get("kaufkurs") and q.get("prev_close"):
            log(f"  {p['name']}: {q['price']:.2f} € ({q.get('quelle', p['ticker'])}), heute "
                f"{(q['price'] / q['prev_close'] - 1) * 100:+.2f} %, seit Kauf "
                f"{(q['price'] * float(p['stueck']) / cost_basis(p) - 1) * 100:+.2f} %")

    if args.beispiel:
        maybe_scan(depot, state, now, args.dry_run, example=args.beispiel)
        save_state(state)
        return 0

    if args.scan:
        maybe_scan(depot, state, now, args.dry_run, force=True)
        save_state(state)
        return 0

    if args.test:
        for u in [CFG.get("melden_url"), "https://raw.githack.com/bohnejulian/aktientracker-verkauf/main/index.html",
                  "https://cdn.jsdelivr.net/gh/bohnejulian/aktientracker-verkauf@main/index.html"]:
            if u:
                try:
                    r = requests.get(u, timeout=15)
                    log(f"Seiten-Test: {r.status_code} {r.headers.get('content-type')} {u}")
                except requests.RequestException as e:
                    log(f"Seiten-Test fehlgeschlagen {u}: {e}")
        if not args.dry_run:  # Selbsttest: kommen Bild + Knopf bei ntfy richtig an? (eigener Wegwerf-Kanal)
            try:
                import base64, uuid
                tt = "selbsttest-" + uuid.uuid4().hex[:12]
                png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
                acts = [{"action": "http", "label": "Gekauft: 9× Test", "method": "POST", "clear": True,
                         "url": f"{NTFY_SERVER}/{tt}", "body": "kauf TEST 9"}]
                r = requests.put(f"{NTFY_SERVER}/{tt}", data=png, timeout=20,
                                 params={"title": "Test ü", "message": "a\nb", "filename": "k.png",
                                         "actions": json.dumps(acts, ensure_ascii=False)})
                m = r.json()
                log(f"Bild-Selbsttest: HTTP {r.status_code}, Knöpfe {[(a.get('label'), a.get('body')) for a in m.get('actions') or []]}, "
                    f"Anhang {bool(m.get('attachment'))}, Titel {m.get('title')!r}")
            except Exception as e:  # noqa: BLE001
                log(f"Bild-Selbsttest fehlgeschlagen: {e}")
        if os.environ.get("ANTHROPIC_API_KEY"):
            pos = (depot.get("positionen") or [{}])[0]
            sample = [(pos.get("name", "DAX"), f"{pos.get('name', 'DAX')}: Quartalszahlen über den Erwartungen", "", pos),
                      ("DAX", "Wetter am Wochenende wird sonnig", "", None)]
            r = ki_rate(depot, sample)
            log(f"KI-Test: {'OK ' + json.dumps(r, ensure_ascii=False) if r else 'fehlgeschlagen'}")
        else:
            log("KI-Test: kein ANTHROPIC_API_KEY gesetzt")
        notify("✅ Tracker läuft!", "Testnachricht vom Aktien-Tracker.\n\n" + summary_text(depot, quotes),
               tags=["white_check_mark"], dry_run=args.dry_run)
        first = next((i for i in depot.get("positionen") or [] if quotes.get(i.get("ticker"))), None)
        if first:
            q = quotes[first["ticker"]]
            url = chart_url(q, 252, f"{first.get('name')} – 1 Jahr",
                            [("Kauf", float(first.get("kaufkurs") or 0), "#16a34a"), ("Stop", stop_level(q, first), "#dc2626")],
                            sma200=True)
            if not args.dry_run:
                try:
                    r = requests.get(url, timeout=20)
                    log(f"Chart-Test: HTTP {r.status_code}, {r.headers.get('content-type')}, {len(r.content)} Bytes, URL {len(url)} Zeichen")
                except requests.RequestException as e:
                    log(f"Chart-Test fehlgeschlagen: {e}")
            notify(f"🔎 Beispiel: {first.get('name')}", headline(first, q) + "\n\n" + assessment(first, q, depot),
                   attach=url,
                   tags=["mag"], dry_run=args.dry_run)
        save_state(state)
        return 0

    if want_status:
        notify("📊 Depot-Übersicht", summary_text(depot, quotes), priority=2, tags=["bar_chart"], dry_run=args.dry_run)

    check_prices(depot, quotes, state, now.strftime("%Y-%m-%d"), args.dry_run)
    check_auto_stops(depot, quotes, state, args.dry_run)
    try:
        check_sector_risk(depot, quotes, state, args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f"Klumpenrisiko-Check fehlgeschlagen: {e}")
    try:
        depot["_termine"] = check_earnings(depot, state, now, args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f"Zahlen-Termine fehlgeschlagen: {e}")
    if now.weekday() < 5:
        maybe_summary(depot, quotes, state, now, args.dry_run)
        try:
            maybe_scan(depot, state, now, args.dry_run)
        except Exception as e:  # noqa: BLE001
            log(f"Scan fehlgeschlagen: {e}")
    check_news(depot, quotes, state, now, args.dry_run)
    try:
        weekly_review(depot, quotes, state, now, args.dry_run, force=args.review)
    except Exception as e:  # noqa: BLE001
        log(f"Wochenrückblick fehlgeschlagen: {e}")
    if now.weekday() < 5 and 8 <= now.hour < 23:
        bot_update(depot, quotes, state, now, args.dry_run)

    try:
        heartbeat(depot, quotes, state, now, args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f"Lebenszeichen fehlgeschlagen: {e}")

    if tickers and not quotes:
        fails = state.get("quote_fail_runs", 0) + 1
        state["quote_fail_runs"] = fails
        if fails == 24:  # ca. 2 Stunden am Stück keine Kurse (Lauf alle 5 Min.)
            notify("⚠️ Tracker: keine Kurse", "Seit ca. 2 Stunden kommen keine Kurse von Yahoo. "
                   "Bitte Ticker in depot.yaml prüfen oder Bescheid geben.", priority=4,
                   tags=["warning"], dry_run=args.dry_run)
    else:
        state["quote_fail_runs"] = 0

    save_state(state)
    log("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
