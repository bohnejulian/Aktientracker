"""Ein Bild pro Kaufidee, das alles zusammenfasst:
Name, Index/Branche, Anlagedauer, Einsatz, Stop, Verkaufsziel, 2-Jahres-Chart und die Chancen aus dem Backtest."""

from __future__ import annotations

import io

GREEN, RED, BLUE, GRAY, INK, MUTED, TILE = "#16a34a", "#dc2626", "#2563eb", "#9ca3af", "#111827", "#4b5563", "#f3f4f6"

# Wie lange so ein Signal typischerweise trägt (Backtest hält max. 40 Handelstage ≈ 8 Wochen)
HORIZON = {
    "Rücksetzer im Aufwärtstrend": ("Kurz bis mittel", "ca. 2–8 Wochen"),
    "Trendwende nach oben": ("Mittel bis lang", "ca. 1–3 Monate"),
    "Stabiles Momentum": ("Kurz", "ca. 2–6 Wochen"),
}


def _eur(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def horizon(kind: str) -> tuple[str, str]:
    return HORIZON.get(kind, ("Offen", "kein Signal"))


def render(idea: dict, odds: dict | None, beispiel: bool = False) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import dates as mdates
    from matplotlib.patches import FancyBboxPatch
    from datetime import datetime

    closes = idea["closes"][-504:]
    dates = (idea.get("dates") or [])[-504:]
    try:
        xs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
        assert len(xs) == len(closes)
    except Exception:  # noqa: BLE001
        xs = list(range(len(closes)))
    price, stop, n = idea["price"], idea["stop"], idea["n"]
    best = odds["best"] if odds else None
    target = price * (1 + best / 100) if best else None
    dauer, dauer2 = horizon(idea["kind"])

    fig = plt.figure(figsize=(7.2, 10.6 if odds else 7.4), dpi=150)
    ratios = [1.0, 2.0, 1.3] if odds else [1.0, 2.0]
    gs = fig.add_gridspec(len(ratios), 1, height_ratios=ratios, hspace=0.32)

    # 1) Kopf: Name, Index, Infokacheln
    hd = fig.add_subplot(gs[0])
    hd.axis("off")
    hd.set_xlim(0, 1)
    hd.set_ylim(0, 1)
    hd.text(0, 0.97, ("BEISPIEL · " if beispiel else "") + idea["name"], fontsize=20, fontweight="bold",
            color=INK, va="top")
    sub = " · ".join(x for x in (idea.get("idx"), idea.get("sektor"), idea["kind"]) if x)
    hd.text(0, 0.71, sub, fontsize=10.5, color=MUTED, va="top")
    if best:
        sell, sell2, sell_c = f"bei +{best} %", f"≈ {_eur(target)} €", GREEN
    elif odds:
        sell, sell2, sell_c = "Gewinne laufen", "Stop nachziehen", BLUE
    else:
        sell, sell2, sell_c = "Stop nachziehen", "kein festes Ziel", BLUE
    tiles = [("Anlagedauer", dauer, dauer2, INK),
             ("Einsatz", f"{n} Stück" if n >= 1 else "–", f"≈ {_eur(n * price)} €" if n >= 1 else "Budget zu klein", INK),
             ("Verkaufen", sell, sell2, sell_c),
             ("Stop-Loss", f"{_eur(stop)} €", f"−{idea['stop_pct'] * 100:.0f} %", RED)]
    w, gap = 0.235, 0.02
    for k, (lab, big, small, col) in enumerate(tiles):
        x = k * (w + gap)
        hd.add_patch(FancyBboxPatch((x, 0.0), w, 0.5, boxstyle="round,pad=0,rounding_size=0.04",
                                    fc=TILE, ec="none", transform=hd.transAxes))
        hd.text(x + 0.02, 0.43, lab.upper(), fontsize=7.5, color=MUTED, va="top", fontweight="bold")
        hd.text(x + 0.02, 0.27, big, fontsize=11.5 if len(big) < 14 else 9.5, color=col, va="center",
                fontweight="bold")
        hd.text(x + 0.02, 0.1, small, fontsize=8.5, color=MUTED, va="center")

    # 2) Kursverlauf 2 Jahre
    ax = fig.add_subplot(gs[1])
    ax.plot(xs, closes, color=BLUE, lw=1.6, label="Kurs")
    full = idea["closes"]
    if len(full) >= 200:
        sma = [sum(full[i - 199:i + 1]) / 200 if i >= 199 else None for i in range(len(full))][-len(closes):]
        ax.plot(xs, sma, color=GRAY, lw=1, ls="--", label="200-Tage-Schnitt")
    ax.axhline(stop, color=RED, lw=1.2, ls="--")
    ax.text(xs[0], stop, f" Stop {_eur(stop)} €", color=RED, va="top", fontsize=8.5)
    if target:
        ax.axhline(target, color=GREEN, lw=1.2, ls="--")
        ax.text(xs[0], target, f" Ziel {_eur(target)} €", color=GREEN, va="bottom", fontsize=8.5)
    ax.plot([xs[-1]], [price], "o", color=BLUE, ms=6)
    if not isinstance(xs[0], int):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%y"))
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right", fontsize=8, frameon=False)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8.5)
    ax.set_title(f"Kurs der letzten 2 Jahre · jetzt {_eur(price)} €", loc="left", fontsize=11, fontweight="bold", color=INK)

    # 3) Chancen aus dem Backtest
    if odds:
        ax2 = fig.add_subplot(gs[2])
        tg = odds["targets"]
        ref = tg[str(best if best is not None else 0)]
        labels, vals, cols = [], [], []
        for x in (5, 10, 20):
            if str(x) in tg:
                labels.append(f"+{x} % erreicht")
                vals.append(tg[str(x)]["p_ziel"] * 100)
                cols.append(GREEN)
        labels.append("Stop gerissen")
        vals.append(ref["p_stop"] * 100)
        cols.append(RED)
        y = list(range(len(vals)))[::-1]
        ax2.barh(y, [100] * len(vals), color=TILE, height=0.62)
        ax2.barh(y, vals, color=cols, height=0.62)
        for yi, v in zip(y, vals):
            ax2.text(min(v, 100) + 1.5, yi, f"{v:.0f} %", va="center", fontsize=11, fontweight="bold", color=INK)
        ax2.set_yticks(y)
        ax2.set_yticklabels(labels, fontsize=9.5)
        ax2.set_xlim(0, 112)
        ax2.set_xticks([])
        for s in ("top", "right", "bottom", "left"):
            ax2.spines[s].set_visible(False)
        ax2.set_title(f"Chancen: so liefen {odds['n']} ähnliche Fälle (max. 8 Wochen gehalten)",
                      loc="left", fontsize=11, fontweight="bold", color=INK)
        ev = ref["ev"] * 100
        ax2.text(0, -0.16, f"Ø früher {'+' if ev >= 0 else ''}{f'{ev:.1f}'.replace('.', ',')} % pro Trade nach Gebühren · "
                 + ("Beispielzahlen · " if beispiel else "") + "Erfahrungswerte, keine Garantie",
                 transform=ax2.transAxes, fontsize=8, color=MUTED, va="top")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white", bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return buf.getvalue()
