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
           click: str | None = None, actions: list[tuple[str, str]] | None = None,
           dry_run: bool = False) -> None:
    """actions: [(Knopf-Text, Nachricht an den Eingangskanal)], max. 3"""
    topic = os.environ.get("NTFY_TOPIC")
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if actions and topic:
        payload["actions"] = [{"action": "http", "label": label[:40], "method": "POST", "clear": True,
                               "url": f"{NTFY_SERVER}/{topic}-depot", "body": body}
                              for label, body in actions[:3]]
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    if dry_run or not topic:
        log(f"[ntfy {'dry-run' if dry_run else 'KEIN NTFY_TOPIC'}] {title} | {message}"
            + (f" | Knöpfe: {actions}" if actions else ""))
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
            if currency and currency.upper() != "EUR":
                rate = fx_rate(currency.upper())  # 1 € = rate Fremdwährung
                if rate:
                    closes = [c / rate for c in closes]
                    currency = "EUR"
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

    d200 = (p / a["sma200"] - 1) * 100 if a["sma200"] else None
    why = []

    # Horizont
    if is_fund:
        horizon = "🕰 Langfristig (breit gestreut, Basisbaustein)"
        why.append("Ein Fonds/ETF steckt in vielen Firmen gleichzeitig. Fällt eine, fangen die anderen das ab, "
                   "deshalb schwankt er weniger und eignet sich zum Liegenlassen.")
    elif up_long:
        horizon = "🕰 Eher langfristig halten (stabiler Aufwärtstrend)"
        why.append(f"Der Kurs liegt {d200:.0f} % über seinem Durchschnitt der letzten 200 Tage, und auch der "
                   "50-Tage-Schnitt liegt darüber. Seit Monaten haben also die Käufer die Oberhand.")
    elif below_200:
        horizon = "⚡ Eher kurzfristig/spekulativ (Abwärtstrend)"
        why.append(f"Der Kurs liegt {abs(d200):.0f} % unter seinem 200-Tage-Durchschnitt. Seit Monaten überwiegen "
                   "die Verkäufer, Erholungen sind in so einer Phase oft nur kurz.")
    else:
        horizon = "↔️ Seitwärts, eher abwarten"
        why.append("Der Kurs pendelt um seinen 200-Tage-Durchschnitt, es gibt keinen klaren Trend.")

    # Signal
    owned = bool(item.get("stueck"))
    buy_ok = True
    if a["rsi"] >= 70:
        signal = f"Überkauft (RSI {a['rsi']:.0f}) – nicht hinterherkaufen" + (", Teilgewinn überlegen" if owned else "")
        buy_ok = False
        why.append("In den letzten 14 Handelstagen ging es fast nur nach oben. Nach so einem Lauf nehmen viele "
                   "Anleger Gewinne mit, und der Kurs setzt oft zurück. Wer jetzt kauft, kauft teuer.")
    elif a["rsi"] <= 30 and up_long:
        signal = f"Rücksetzer im Aufwärtstrend (RSI {a['rsi']:.0f}) – mögliche Nachkauf-Chance"
        why.append("Kurzfristig ist die Aktie stark gefallen, obwohl der langfristige Trend nach oben zeigt. "
                   "Solche Dellen wurden in Aufwärtstrends oft wieder aufgeholt, man kauft also günstiger ein. "
                   "Vorher prüfen, ob schlechte News dahinterstecken.")
    elif a["rsi"] <= 30:
        signal = f"Stark gefallen (RSI {a['rsi']:.0f}), Trend aber schwach – fallendes Messer, vorsichtig"
        buy_ok = False
        why.append("Die Aktie ist stark gefallen, und es gibt keinen Aufwärtstrend, der sie stützt. "
                   "„Billig“ kann hier noch billiger werden.")
    elif below_200:
        signal = "Unter der 200-Tage-Linie – erst Bodenbildung abwarten"
        buy_ok = False
        why.append("Ein Kauf wäre eine Wette auf die Trendwende. Sicherer ist es zu warten, bis der Kurs wieder über "
                   f"dem 200-Tage-Schnitt (~{eur(a['sma200'])}) steht.")
    elif a["pos52"] >= 0.95:
        signal = "Nahe 52-Wochen-Hoch – Trend intakt, aber Einstieg teuer"
        why.append(f"Sie steht fast auf dem höchsten Kurs des Jahres ({eur(a['hi'])}). Der Trend ist stark, "
                   "aber bei einem Rückschlag gibt es wenig Puffer.")
    else:
        signal = "Keine Auffälligkeit im Chart"
        why.append("Sie ist weder überkauft noch überverkauft.")
    if a["perf1m"] is not None and abs(a["perf1m"]) >= 0.1:
        why.append(f"Im letzten Monat: {pct(a['perf1m'] * 100)}.")

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
    stop = stop_level(q)
    stop_pct = max(0.0, 1 - stop / p)

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
    lines.append(f"🛑 Stop-Loss: ~{eur(stop)} {cur} (−{stop_pct * 100:.0f} % vom Kurs) · Schwankung {risk} ({vol * 100:.0f} %/J.)")
    if owned and item.get("kaufkurs"):
        lines.append(f"Seit Kauf: {pct((p / float(item['kaufkurs']) - 1) * 100)}")
    why.append(f"Einsatz und Stop-Abstand richten sich nach der Schwankung (~{vol * 100:.0f} % im Jahr): "
               "je wilder, desto kleiner der Einsatz und desto mehr Luft bis zum Stop.")
    lines.append("\n💡 Warum? " + " ".join(why))
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
                     f"{pct(perf)} seit Kauf) [{short_horizon(p, q)}]"
                     + (f" Stop {eur(float(p['unter']) if p.get('unter') is not None else stop_level(q))}"
                        if (depot.get("einstellungen") or {}).get("auto_stop", True) or p.get("unter") is not None else ""))
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
            chg = (q["price"] / q["prev_close"] - 1) * 100
            msg += f"\n\nKurs {eur(q['price'])} {q['currency']} ({pct(chg)} heute)\n" + assessment(item, q, depot) + DISCLAIMER
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

def stop_level(q: dict) -> float:
    """Nachgezogener Stop: höchster Schlusskurs der letzten 20 Tage minus schwankungsabhängiger Abstand."""
    a = analyse(q["closes"])
    stop_pct = min(max(a["vol"] * 0.35, 0.05), 0.25)
    return max(q["closes"][-20:] + [q["price"]]) * (1 - stop_pct)


def check_auto_stops(depot: dict, quotes: dict, state: dict, dry_run: bool) -> None:
    if not (depot.get("einstellungen") or {}).get("auto_stop", True):
        return
    armed = state.setdefault("auto_stop_hit", {})
    for item in depot.get("positionen") or []:
        t = item.get("ticker")
        q = quotes.get(t)
        if not q or item.get("unter") is not None:
            continue  # eigene Marke hat Vorrang
        stop = stop_level(q)
        if q["price"] <= stop and not armed.get(t):
            loss = (q["price"] / float(item["kaufkurs"]) - 1) * 100 if item.get("kaufkurs") else None
            notify(f"🛑 {item.get('name', t)} unter Stop-Loss {eur(stop)}",
                   f"Kurs {eur(q['price'])} {q['currency']}"
                   + (f", seit Kauf {pct(loss)}" if loss is not None else "")
                   + ".\nDer Kurs ist deutlich unter sein jüngstes Hoch gefallen. Verkaufen, Teil verkaufen "
                   "oder bewusst halten?\n\n" + assessment(item, q, depot) + DISCLAIMER,
                   priority=5, tags=["octagonal_sign"],
                   actions=[("Alles verkauft", f"verkauf {t} alle"),
                            ("Hälfte verkauft", f"verkauf {t} {max(1, int(float(item.get('stueck', 2)) // 2))}")],
                   click=f"https://finance.yahoo.com/quote/{urllib.parse.quote(t)}", dry_run=dry_run)
            armed[t] = True
        elif q["price"] > stop * 1.02:
            armed[t] = False


# ---------------------------------------------------------------- Kaufideen-Scan

def maybe_scan(depot: dict, state: dict, now: datetime, dry_run: bool, force: bool = False) -> None:
    cfg = depot.get("einstellungen") or {}
    hhmm = cfg.get("scan_uhrzeit", "08:30")
    if not hhmm:
        return
    h, m = map(int, str(hhmm).split(":"))
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    if not force and (state.get("scan_day") == today or not slot <= now < slot + timedelta(hours=3)):
        return
    state["scan_day"] = today
    import scanner

    uni = state.get("universe")
    if not uni or uni.get("week") != now.strftime("%G-%V"):
        loaded = scanner.load_universe(log)
        uni = {"week": now.strftime("%G-%V"), "items": {t: list(v) for t, v in loaded.items()}}
        state["universe"] = uni
    universe = {t: tuple(v) for t, v in uni["items"].items()}
    held = {i.get("ticker") for i in all_items(depot)} | {i.get("heimat") for i in all_items(depot)}
    budget = float(depot.get("budget") or 0)
    ideas = scanner.scan(universe, held, analyse, fx_rate, budget,
                         float(cfg.get("max_anteil_pro_kauf", 25)) / 100, log, int(cfg.get("scan_anzahl", 5)))
    if not ideas:
        notify("💡 Kaufideen: heute nichts", f"{len(universe)} Aktien geprüft, kein klares Kaufsignal. "
               "Nicht kaufen ist auch eine Entscheidung.", priority=2, tags=["bulb"], dry_run=dry_run)
        return
    state["last_ideas"] = {i["t"]: {"name": i["name"], "price": i["price"],
                                    **({"heimat": i["t"]} if "." not in i["t"] else {})} for i in ideas}
    buttons = [(f"Gekauft: {i['n']}× {i['name']}", f"kauf {i['t']} {i['n']}") for i in ideas if i["n"] >= 1][:3]
    parts = []
    for n, i in enumerate(ideas, 1):
        amount = (f"bis ~{eur(i['n'] * i['price'])} € ({i['n']} Stk. à ~{eur(i['price'])} €)" if i["n"] >= 1
                  else f"Kurs ~{eur(i['price'])} €, Budget reicht nicht")
        parts.append(f"{n}. {i['name']} ({i['idx']}) – {i['kind']}\n"
                     f"💶 {amount} · 🛑 Stop ~{eur(i['stop'])} € (−{i['stop_pct'] * 100:.0f} %)\n"
                     f"💡 {i['why']}")
    notify(f"💡 {len(ideas)} Kaufideen für heute", "\n\n".join(parts)
           + f"\n\nGeprüft: {len(universe)} Aktien aus DAX, MDAX, TecDAX, EuroStoxx50, Nasdaq 100, Dow. "
           "Bitte im Planspiel prüfen, ob die Aktie dort handelbar ist. Nach dem Kauf auf den Knopf tippen "
           "(oder z. B. „kauf SAP 5“ in den Depot-Kanal schreiben), dann bucht der Bot es ein." + DISCLAIMER,
           priority=3, tags=["bulb"], actions=buttons, dry_run=dry_run)


# ---------------------------------------------------------------- Main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Test-Mitteilung + Übersicht senden")
    ap.add_argument("--dry-run", action="store_true", help="nichts senden, nur anzeigen")
    ap.add_argument("--scan", action="store_true", help="Kaufideen-Scan sofort ausführen")
    args = ap.parse_args()

    state = load_state()
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
    resolve_symbols(depot, state)
    tickers = sorted({i["ticker"] for i in all_items(depot) if i.get("ticker")})

    log(f"Hole Kurse für {len(tickers)} Werte …")
    quotes = fetch_quotes(tickers)
    log(f"{len(quotes)}/{len(tickers)} Kurse erhalten")

    if args.scan:
        maybe_scan(depot, state, now, args.dry_run, force=True)
        save_state(state)
        return 0

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

    if want_status:
        notify("📊 Depot-Übersicht", summary_text(depot, quotes), priority=2, tags=["bar_chart"], dry_run=args.dry_run)

    check_prices(depot, quotes, state, now.strftime("%Y-%m-%d"), args.dry_run)
    check_auto_stops(depot, quotes, state, args.dry_run)
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
