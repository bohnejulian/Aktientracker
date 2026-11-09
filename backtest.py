"""Backtest der Kaufideen-Regeln: Was hätten die Signale der letzten ~1,5 Jahre gebracht?

Für jeden Handelstag und jede Aktie wird geprüft, ob scanner.signal() ausgelöst hätte (höchstens
ein Signal pro Aktie alle 10 Tage). Dann wird gemessen, was 10, 20 und 40 Handelstage später
passiert ist, mit Stop-Loss und nach Gebühren (Kauf + Verkauf). Ausgabe nach Signal-Art und Score.

Start: Actions -> Backtest -> Run workflow (oder: python backtest.py)
"""

from __future__ import annotations

import statistics as st

import yfinance as yf

import scanner
from tracker import analyse, log


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
            rows.append({"t": t, "kind": sig["kind"], "score": sig["score"], **{f"r{h}": res[h] for h in res}})

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


if __name__ == "__main__":
    main()
