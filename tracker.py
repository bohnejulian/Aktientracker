"""Aktien- und News-Tracker fürs Planspiel Börse.

Läuft alle 15 Minuten (GitHub Actions), prüft Kurse und News zu den
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
           click: str | None = None, dry_run: bool = False) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    if dry_run or not topic:
        log(f"[ntfy {'dry-run' if dry_run else 'KEIN NTFY_TOPIC'}] {title} | {message}")
        return
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    for attempt in range(3):
        try:
            r = requests.post(NTFY_SERVER, json=payload, headers=headers, timeout=15)
            r.raise_for_status()
            return
        except requests.RequestException as e:
            log(f"ntfy fehlgeschlagen ({e}), Versuch {attempt + 1}/3")
            time.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------- Kurse

PREFERRED_SUFFIXES = (".SG", ".DE", ".F", ".DU", ".MU", ".BE")  # Stuttgart zuerst, wie im Planspiel


def resolve_symbols(depot: dict, state: dict) -> None:
    """Setzt item['ticker'] anhand der ISIN (Yahoo-Suche), Ergebnis wird gemerkt."""
    import yfinance as yf

    cache = state.setdefault("symbols", {})
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
            quotes = yf.Search(isin, max_results=20, news_count=0).quotes or []
        except Exception as e:  # noqa: BLE001
            log(f"ISIN-Suche {isin} fehlgeschlagen: {e}")
            quotes = []
        symbols = [q.get("symbol") for q in quotes if q.get("symbol")]
        pick = next((s for suf in PREFERRED_SUFFIXES for s in symbols if s.endswith(suf)), None)
        pick = pick or (symbols[0] if symbols else None)
        if pick:
            log(f"{item.get('name', isin)}: {isin} -> {pick}")
            cache[isin] = pick
            item["ticker"] = pick
        else:
            log(f"Kein Kurs-Kürzel für {item.get('name', isin)} ({isin}) gefunden – bitte 'ticker' eintragen")


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {price, prev_close, currency, closes}} aus einem Jahr Tageskursen."""
    import yfinance as yf

    quotes = {}
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            hist = tk.history(period="1y", interval="1d", auto_adjust=False)
            closes = [float(c) for c in hist["Close"].dropna().tolist()]
            if len(closes) < 2:
                raise ValueError("zu wenige Kursdaten")
            try:
                currency = tk.fast_info.get("currency") or ""
            except Exception:  # noqa: BLE001
                currency = ""
            quotes[t] = {"price": closes[-1], "prev_close": closes[-2],
                         "currency": "€" if currency.upper() == "EUR" else currency, "closes": closes}
        except Exception as e:  # noqa: BLE001 – Yahoo wirft alles Mögliche
            log(f"Kurs für {t} nicht abrufbar: {e}")
    return quotes


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

    hi, lo = max(closes), min(closes)
    return {
        "sma50": sma(50), "sma200": sma(200), "rsi": rsi, "vol": vol,
        "hi": hi, "lo": lo, "pos52": (closes[-1] - lo) / (hi - lo) if hi > lo else 0.5,
        "perf1m": closes[-1] / closes[-22] - 1 if len(closes) >= 22 else None,
    }


def assessment(item: dict, q: dict, depot: dict) -> str:
    """Kurze, regelbasierte Einschätzung: Horizont, Signal, Betrag, Stop. Keine Anlageberatung."""
    a = analyse(q["closes"])
    p = q["price"]
    cur = q["currency"]
    cfg = depot.get("einstellungen") or {}
    budget = float(depot.get("budget") or 0)
    is_fund = item.get("art") in ("etf", "fonds")

    up_long = a["sma200"] is not None and p > a["sma200"] and (a["sma50"] or p) > a["sma200"]
    below_200 = a["sma200"] is not None and p < a["sma200"]

    # Horizont
    if is_fund:
        horizon = "🕰 Langfristig (breit gestreut, Basisbaustein)"
    elif up_long:
        horizon = "🕰 Eher langfristig halten (stabiler Aufwärtstrend)"
    elif below_200:
        horizon = "⚡ Eher kurzfristig/spekulativ (Abwärtstrend)"
    else:
        horizon = "↔️ Seitwärts, eher abwarten"

    # Signal
    owned = bool(item.get("stueck"))
    buy_ok = True
    if a["rsi"] >= 70:
        signal = f"Überkauft (RSI {a['rsi']:.0f}) – nicht hinterherkaufen" + (", Teilgewinn überlegen" if owned else "")
        buy_ok = False
    elif a["rsi"] <= 30 and up_long:
        signal = f"Rücksetzer im Aufwärtstrend (RSI {a['rsi']:.0f}) – mögliche Nachkauf-Chance"
    elif a["rsi"] <= 30:
        signal = f"Stark gefallen (RSI {a['rsi']:.0f}), Trend aber schwach – fallendes Messer, vorsichtig"
        buy_ok = False
    elif below_200:
        signal = "Unter der 200-Tage-Linie – erst Bodenbildung abwarten"
        buy_ok = False
    elif a["pos52"] >= 0.95:
        signal = "Nahe 52-Wochen-Hoch – Trend intakt, aber Einstieg teuer"
    else:
        signal = "Keine Auffälligkeit im Chart"

    # Schwankung und Positionsgröße (je wilder, desto kleiner)
    vol = a["vol"]
    if vol < 0.25:
        risk, share = "niedrig", 0.25
    elif vol < 0.40:
        risk, share = "mittel", 0.15
    elif vol < 0.60:
        risk, share = "hoch", 0.10
    else:
        risk, share = "sehr hoch", 0.06
    share = min(share, float(cfg.get("max_anteil_pro_kauf", 25)) / 100)
    stop_pct = min(max(vol * 0.35, 0.05), 0.25)

    lines = [horizon, f"📌 {signal}"]
    if budget > 0 and buy_ok:
        amount = budget * share
        n = int(amount // p)
        if n >= 1:
            lines.append(f"💶 Falls Kauf: max. ~{eur(n * p)} {cur} ({n} Stk., {share * 100:.0f} % vom Budget {eur(budget)} €)")
        else:
            lines.append(f"💶 Budget ({eur(budget)} €) reicht für keinen sinnvollen Kauf")
    elif budget > 0:
        lines.append("💶 Aktuell kein Nachkauf vorgeschlagen")
    lines.append(f"🛑 Stop-Loss-Idee: ~{eur(p * (1 - stop_pct))} {cur} (−{stop_pct * 100:.0f} %) · Schwankung {risk} ({vol * 100:.0f} %/J.)")
    if owned and item.get("kaufkurs"):
        lines.append(f"Seit Kauf: {pct((p / float(item['kaufkurs']) - 1) * 100)}")
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


DISCLAIMER = "\n— Automatische Chart-Regeln, keine Anlageberatung."


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
                extra = ""
                if own:
                    diff = (q["price"] - q["prev_close"]) * own
                    extra = f"\nEure {own} Stück: {'+' if diff >= 0 else ''}{eur(diff)} {q['currency']} heute"
                notify(f"{'📈' if chg > 0 else '📉'} {name} {pct(chg)}",
                       f"Kurs {eur(q['price'])} {q['currency']} (Vortag {eur(q['prev_close'])}){extra}\n\n"
                       + assessment(item, q, depot) + DISCLAIMER,
                       priority=4 if abs(chg) >= first + step else 3,
                       tags=["chart_with_upwards_trend" if chg > 0 else "chart_with_downwards_trend"],
                       click=link, dry_run=dry_run)

        # 2) Selbst gesetzte Kursmarken (Alarm beim Reißen, wieder scharf beim Zurückkommen)
        for kind, cond in (("ueber", lambda p, m: p >= m), ("unter", lambda p, m: p <= m)):
            mark = item.get(kind)
            if mark is None:
                continue
            key = f"{t}:{kind}:{mark}"
            hit = cond(q["price"], float(mark))
            if hit and not targets.get(key):
                notify(f"🎯 {name} {'über' if kind == 'ueber' else 'unter'} {eur(float(mark))}",
                       f"Aktuell {eur(q['price'])} {q['currency']} ({pct(chg)} heute)\n\n"
                       + assessment(item, q, depot) + DISCLAIMER,
                       priority=5 if kind == "unter" else 4, tags=["dart"],
                       click=link, dry_run=dry_run)
            targets[key] = hit


def all_items(depot: dict) -> list[dict]:
    return list(depot.get("positionen") or []) + list(depot.get("watchlist") or [])


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
        cost = float(p.get("kaufkurs", q["price"])) * n
        total_value += value
        total_cost += cost
        day_diff += (q["price"] - q["prev_close"]) * n
        perf = (value / cost - 1) * 100 if cost else 0
        lines.append(f"{name}: {eur(q['price'])} ({pct((q['price'] / q['prev_close'] - 1) * 100)} heute, "
                     f"{pct(perf)} seit Kauf) [{short_horizon(p, q)}]")
        weights.append((value, name))
    if total_cost:
        lines.append("")
        lines.append(f"Depotwert: {eur(total_value)}  |  heute {'+' if day_diff >= 0 else ''}{eur(day_diff)}  |  "
                     f"gesamt {pct((total_value / total_cost - 1) * 100)}")
        big = [f"{n} {v / total_value * 100:.0f} %" for v, n in sorted(weights, reverse=True) if v / total_value > 0.2]
        if big:
            lines.append("⚠️ Klumpenrisiko: " + ", ".join(big) + " vom Depot")
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
            if key in seen:
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

    for label, title, link, item in fresh[:budget]:
        msg = title
        q = quotes.get(item.get("ticker")) if item else None
        if q:
            chg = (q["price"] / q["prev_close"] - 1) * 100
            msg += f"\n\nKurs {eur(q['price'])} {q['currency']} ({pct(chg)} heute)\n" + assessment(item, q, depot) + DISCLAIMER
        notify(f"📰 {label}", msg, priority=3, tags=["newspaper"], click=link, dry_run=dry_run)
    if len(fresh) > budget:
        notify("📰 Weitere News", f"{len(fresh) - budget} weitere Meldungen, z. B.: "
               + "; ".join(f[1] for f in fresh[budget:budget + 3]),
               priority=2, tags=["newspaper"], dry_run=dry_run)

    # Gemerkte News nach 3 Tagen vergessen
    cutoff = now.timestamp() - 3 * 86400
    for k in [k for k, ts in seen.items() if ts < cutoff]:
        del seen[k]


# ---------------------------------------------------------------- Main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Test-Mitteilung + Übersicht senden")
    ap.add_argument("--dry-run", action="store_true", help="nichts senden, nur anzeigen")
    args = ap.parse_args()

    depot = yaml.safe_load(DEPOT_FILE.read_text()) or {}
    state = load_state()
    now = datetime.now(BERLIN)
    resolve_symbols(depot, state)
    tickers = sorted({i["ticker"] for i in all_items(depot) if i.get("ticker")})

    log(f"Hole Kurse für {len(tickers)} Werte …")
    quotes = fetch_quotes(tickers)
    log(f"{len(quotes)}/{len(tickers)} Kurse erhalten")

    if args.test:
        notify("✅ Tracker läuft!", "Testnachricht vom Aktien-Tracker.\n\n" + summary_text(depot, quotes),
               tags=["white_check_mark"], dry_run=args.dry_run)
        first = next((i for i in depot.get("positionen") or [] if quotes.get(i.get("ticker"))), None)
        if first:
            q = quotes[first["ticker"]]
            notify(f"🔎 Beispiel-Einschätzung: {first.get('name')}", assessment(first, q, depot) + DISCLAIMER,
                   tags=["mag"], dry_run=args.dry_run)
        save_state(state)
        return 0

    check_prices(depot, quotes, state, now.strftime("%Y-%m-%d"), args.dry_run)
    if now.weekday() < 5:
        maybe_summary(depot, quotes, state, now, args.dry_run)
    check_news(depot, quotes, state, now, args.dry_run)

    if tickers and not quotes:
        fails = state.get("quote_fail_runs", 0) + 1
        state["quote_fail_runs"] = fails
        if fails == 8:  # ca. 2 Stunden am Stück keine Kurse
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
