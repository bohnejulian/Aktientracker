"""Backtest der Kaufideen-Regeln: Was hätten die Signale der letzten ~1,5 Jahre gebracht?

Für jeden Handelstag und jede Aktie wird geprüft, ob scanner.signal() ausgelöst hätte (höchstens
ein Signal pro Aktie alle 10 Tage). Dann wird gemessen, was 10, 20 und 40 Handelstage später
passiert ist, mit Stop-Loss und nach Gebühren (Kauf + Verkauf). Ausgabe nach Signal-Art und Score.

Start: Actions -> Backtest -> Run workflow (oder: python backtest.py)
"""

from __future__ import annotations

import json
import statistics as st
from datetime import date
from pathlib import Path

import yfinance as yf

import scanner
from tracker import analyse, log


TARGETS = (3, 5, 8, 10, 15, 20, 30)
STATS_FILE = Path(__file__).parent / "stats.json"


def vol_bucket(vol: float) -> str:
    return "ruhig" if vol < 0.25 else "mittel" if vol < 0.40 else "wild"


def group_stats(rs: list[dict]) -> dict:
    """Wahrscheinlichkeiten je Gewinnziel + bestes Ziel (höchster Ø-Ertrag pro Trade nach Gebühren)."""
    n = len(rs)
    targets = {}
    for x in TARGETS:
        outs = [r["out"][x] for r in rs]
        targets[x] = {
            "p_ziel": sum(o[0] == "ziel" for o in outs) / n,
            "p_stop": sum(o[0] == "stop" for o in outs) / n,
            "p_zeit": sum(o[0] == "zeit" for o in outs) / n,
            "ev": st.mean(o[1] for o in outs),
        }
    best = max(TARGETS, key=lambda x: targets[x]["ev"])
    return {"n": n, "targets": {str(k): v for k, v in targets.items()}, "best": best,
            "ev_best": targets[best]["ev"], "p_stop_best": targets[best]["p_stop"]}


def main() -> None:
    uni = scanner.load_universe(log)
    tickers = sorted(uni)
    log(f"Lade 3 Jahre Kurse für {len(tickers)} Aktien …")
    data = yf.download(tickers, period="3y", interval="1d", group_by="ticker", auto_adjust=True,
                       progress=False, threads=True)
    fee = 0.006  # ca. 0,3 % pro Kauf und Verkauf
    rows = []
    for t in tickers:
        try:
            closes = [float(c) for c in data[t]["Close"].dropna().tolist()]
        except Exception:  # noqa: BLE001
            continue
        if len(closes) < 300:
            continue
        last_sig = -99
        for i in range(260, len(closes) - 40):
            if i - last_sig < 10:
                continue
            hist = closes[max(0, i - 260):i + 1]
            sig = scanner.signal(hist, analyse)
            if not sig:
                continue
            last_sig = i
            entry = closes[i]
            stop = max(hist[-20:]) * (1 - min(max(sig["vol"] * 0.35, 0.05), 0.25))
            res = {}
            for h in (10, 20, 40):
                path = closes[i + 1:i + h + 1]
                hit = next((p for p in path if p <= stop), None)
                exit_ = stop if hit is not None else path[-1]
                res[h] = exit_ / entry - 1 - fee
            # Für jedes Gewinnziel: was passiert zuerst – Ziel, Stop oder 40 Tage vorbei?
            path = closes[i + 1:i + 41]
            outcomes = {}
            for x in TARGETS:
                out, ret = "zeit", path[-1] / entry - 1 - fee
                for p in path:
                    if p <= stop:
                        out, ret = "stop", stop / entry - 1 - fee
                        break
                    if p >= entry * (1 + x / 100):
                        out, ret = "ziel", x / 100 - fee
                        break
                outcomes[x] = (out, ret)
            rows.append({"t": t, "kind": sig["kind"], "score": sig["score"], "vb": vol_bucket(sig["vol"]),
                         "out": outcomes, **{f"r{h}": res[h] for h in res}})

    def report(label, rs):
        if not rs:
            return
        r20 = [r["r20"] for r in rs]
        r40 = [r["r40"] for r in rs]
        win = sum(1 for x in r20 if x > 0) / len(r20) * 100
        print(f"{label:34s} n={len(rs):5d} | 20T: Ø {st.mean(r20) * 100:+5.1f} %  Median {st.median(r20) * 100:+5.1f} %  "
              f"Treffer {win:4.0f} % | 40T: Ø {st.mean(r40) * 100:+5.1f} %")

    print("\n===== Backtest (nach Gebühren, mit Stop-Loss) =====")
    # Vergleich: einfach irgendwann kaufen
    base = []
    for t in tickers:
        try:
            c = [float(x) for x in data[t]["Close"].dropna().tolist()]
        except Exception:  # noqa: BLE001
            continue
        for i in range(260, len(c) - 40, 10):
            base.append({"r20": c[i + 20] / c[i] - 1 - fee, "r40": c[i + 40] / c[i] - 1 - fee})
    report("Vergleich: zufällig kaufen", base)
    report("Alle Signale", rows)
    for k in sorted({r["kind"] for r in rows}):
        report(k, [r for r in rows if r["kind"] == k])
    for lo, hi in ((-99, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 99)):
        report(f"Score {lo}–{hi}", [r for r in rows if lo <= r["score"] < hi])

    # Statistik für die Kaufideen: je Signal-Art x Schwankung, jeweils alle und nur starke (Score >= 25)
    stats = {"stand": date.today().isoformat(), "gruppen": {}}
    for k in sorted({r["kind"] for r in rows}):
        for vb in ("ruhig", "mittel", "wild"):
            for label, cond in (("alle", lambda r: True), ("stark", lambda r: r["score"] >= 25)):
                rs = [r for r in rows if r["kind"] == k and r["vb"] == vb and cond(r)]
                if len(rs) >= 40:
                    stats["gruppen"][f"{k}|{vb}|{label}"] = group_stats(rs)
    print("\n===== Bestes Gewinnziel je Gruppe =====")
    for key, g in stats["gruppen"].items():
        t = g["targets"][str(g["best"])]
        print(f"{key:45s} n={g['n']:4d}  Ziel +{g['best']:2d} %: erreicht {t['p_ziel'] * 100:3.0f} %, "
              f"Stop {t['p_stop'] * 100:3.0f} %, Ø {t['ev'] * 100:+.1f} %/Trade")
    STATS_FILE.write_text(json.dumps(stats, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
