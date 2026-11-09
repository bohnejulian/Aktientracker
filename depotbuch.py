"""Käufe/Verkäufe per ntfy-Nachricht melden und depot.yaml automatisch anpassen.

Eingangskanal: <NTFY_TOPIC>-depot. Nachrichten (Groß/Klein egal, Komma oder Punkt):
  kauf SAP 9            -> 9 Stück zum aktuellen Kurs
  kauf SAP 9 274,50     -> 9 Stück zu 274,50 €
  verkauf Nvidia 20     -> 20 Stück verkauft (aktueller Kurs)
  verkauf Nvidia alle 160
  budget 8000           -> freies Bot-Budget setzen
  status                -> Depot-Übersicht sofort
  hilfe
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests
from ruamel.yaml import YAML

HELP = ("So meldet ihr Trades (einfach als Nachricht in den Kanal schreiben):\n"
        "kauf SAP 9  ·  kauf SAP 9 274,50\n"
        "verkauf Nvidia 20  ·  verkauf Nvidia alle 160\n"
        "budget 8000  ·  status\n"
        "Ohne Kurs nimmt der Bot den aktuellen. Verarbeitet wird beim nächsten Lauf (max. ~15 Min.).")

BUY = ("kauf", "kaufe", "gekauft", "buy", "k")
SELL = ("verkauf", "verkaufe", "verkauft", "sell", "v")


def inbox_topic(topic: str) -> str:
    return f"{topic}-depot"


def fetch_inbox(server: str, topic: str, state: dict, log) -> list[dict]:
    """Liest Befehle aus dem Hauptkanal (und dem alten -depot-Kanal).
    Nachrichten des Bots haben immer einen Titel, Befehle von euch nicht – daran werden sie unterschieden."""
    last = state.setdefault("inbox_last2", {})
    msgs = []
    for t in (topic, inbox_topic(topic)):
        since = last.get(t) or "12h"
        try:
            r = requests.get(f"{server}/{t}/json", params={"poll": "1", "since": since}, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"Kanal {t[-6:]} nicht lesbar: {e}")
            continue
        got = []
        for line in r.text.splitlines():
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("event") == "message":
                got.append(m)
        got.sort(key=lambda m: m.get("time", 0))
        if got:
            last[t] = got[-1]["id"]
        elif t not in last:
            last[t] = "now"
        msgs += [m for m in got if m.get("message") and not m.get("title")]
    msgs.sort(key=lambda m: m.get("time", 0))
    return msgs


def _num(s: str) -> float | None:
    s = s.strip().replace("€", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):  # 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse(text: str) -> dict | None:
    words = text.strip().split()
    if not words:
        return None
    cmd = words[0].lower().rstrip(":")
    if cmd in ("hilfe", "help", "?"):
        return {"cmd": "hilfe"}
    if cmd in ("status", "depot", "übersicht", "uebersicht"):
        return {"cmd": "status"}
    if cmd == "budget" and len(words) >= 2:
        v = _num(words[1])
        return {"cmd": "budget", "wert": v} if v is not None else None
    if cmd in BUY or cmd in SELL:
        rest = words[1:]
        price = qty = None
        all_ = False
        # von hinten: [kurs] und stück
        nums = []
        while rest and (_num(rest[-1]) is not None or rest[-1].lower() in ("alle", "alles", "all")):
            nums.insert(0, rest.pop())
        if not rest or not nums:
            return None
        if nums[0].lower() in ("alle", "alles", "all"):
            all_ = True
        else:
            qty = _num(nums[0])
        if len(nums) >= 2:
            price = _num(nums[1])
        return {"cmd": "kauf" if cmd in BUY else "verkauf", "wer": " ".join(rest), "stueck": qty,
                "alle": all_, "kurs": price}
    return None


def _find(positions, who: str):
    w = who.lower()
    for p in positions:
        keys = [str(p.get(k, "")).lower() for k in ("ticker", "heimat", "isin", "name")]
        if w in keys:
            return p
    for p in positions:
        if str(p.get("name", "")).lower().startswith(w):
            return p
    return None


def _fee(doc, amount: float) -> float:
    cfg = doc.get("einstellungen") or {}
    return max(float(cfg.get("gebuehr_min", 15)), amount * float(cfg.get("gebuehr_prozent", 0.3)) / 100)


def _cash(doc, delta: float) -> None:
    if doc.get("barbestand") is not None:
        doc["barbestand"] = round(float(doc["barbestand"]) + delta, 2)


def _log_trade(state: dict, m: dict, text: str) -> None:
    state.setdefault("trades", []).append({"t": m.get("time", 0), "text": text})
    del state["trades"][:-50]


def process(depot_file: Path, msgs: list[dict], state: dict, fetch_quotes, notify, summary, log, dry_run) -> bool:
    """Bucht alle Nachrichten. Gibt True zurück, wenn depot.yaml geändert wurde."""
    if not msgs:
        return False
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    doc = yaml.load(depot_file.read_text())
    positions = doc.get("positionen")
    if positions is None:
        doc["positionen"] = positions = []
    ideas = state.get("last_ideas") or {}
    changed = False

    for m in msgs:
        text = m["message"]
        cmd = parse(text)
        log(f"Eingang: {text!r} -> {cmd}")
        if not cmd:
            notify("❓ Nicht verstanden", f"„{text}“\n\n{HELP}", priority=2, tags=["question"], dry_run=dry_run)
            continue
        if cmd["cmd"] == "hilfe":
            notify("ℹ️ Trades melden", HELP, priority=2, tags=["information_source"], dry_run=dry_run)
            continue
        if cmd["cmd"] == "status":
            summary()
            continue
        if cmd["cmd"] == "budget":
            doc["budget"] = round(cmd["wert"], 2)
            changed = True
            notify("✅ Budget gesetzt", f"Freies Bot-Budget: {cmd['wert']:,.2f} €".replace(",", "X").replace(".", ",")
                   .replace("X", "."), priority=2, tags=["white_check_mark"], dry_run=dry_run)
            continue

        pos = _find(positions, cmd["wer"])
        ticker = pos.get("ticker") if pos else None
        name = pos.get("name") if pos else None
        if not pos:
            # Kaufidee von heute oder direktes Yahoo-Kürzel
            for t, i in ideas.items():
                if cmd["wer"].lower() in (t.lower(), i["name"].lower()) or i["name"].lower().startswith(cmd["wer"].lower()):
                    ticker, name = t, i["name"]
                    break
            else:
                ticker, name = cmd["wer"].upper(), cmd["wer"]
        price = cmd["kurs"]
        if price is None:
            q = fetch_quotes([ticker]).get(ticker)
            if not q:
                notify("⚠️ Kurs unbekannt", f"„{text}“: Für {name} finde ich keinen Kurs. Bitte mit Kurs schicken, "
                       f"z. B. „{cmd['cmd']} {cmd['wer']} {int(cmd['stueck'] or 1)} 123,45“, oder das Yahoo-Kürzel "
                       "nehmen (z. B. SAP.DE).", priority=3, tags=["warning"], dry_run=dry_run)
                continue
            price = q["price"]

        budget = float(doc.get("budget") or 0)
        if cmd["cmd"] == "kauf":
            n = cmd["stueck"]
            if not n:
                continue
            if pos:
                pos["bot"] = True
                old_n = float(pos.get("stueck", 0))
                old_k = float(pos.get("kaufkurs", price))
                pos["stueck"] = _int_or(old_n + n)
                pos["kaufkurs"] = round((old_n * old_k + n * price) / (old_n + n), 2)
            else:
                new = {"name": name, "ticker": ticker, "stueck": _int_or(n), "kaufkurs": round(price, 2),
                       "news": name, "bot": True}
                if ticker in ideas and ideas[ticker].get("heimat"):
                    new["heimat"] = ideas[ticker]["heimat"]
                if ticker in ideas and ideas[ticker].get("ziel_prozent"):
                    zp = ideas[ticker]["ziel_prozent"]
                    new["ziel_prozent"] = zp
                    new["ueber"] = round(price * (1 + zp / 100), 2)  # Gewinnziel-Alarm
                positions.append(new)
            f = _fee(doc, n * price)
            doc["budget"] = round(max(0.0, budget - n * price - f), 2)
            _cash(doc, -(n * price + f))
            changed = True
            _log_trade(state, m, f"Kauf {_int_or(n)} × {name} à {_eur(price)} €")
            goal = ""
            if not pos and ticker in ideas and ideas[ticker].get("ziel_prozent"):
                goal = (f"\n🎯 Gewinnziel-Alarm gesetzt bei {_eur(price * (1 + ideas[ticker]['ziel_prozent'] / 100))} € "
                        f"(+{ideas[ticker]['ziel_prozent']} %)")
            notify(f"✅ Gebucht: {_int_or(n)} × {name}", f"Kauf à {_eur(price)} € = {_eur(n * price)} € "
                   f"+ ~{_eur(f)} € Gebühr\nFreies Budget jetzt: {_eur(doc['budget'])} €{goal}", priority=2,
                   tags=["white_check_mark"], dry_run=dry_run)
        else:
            if not pos:
                notify("⚠️ Nicht im Depot", f"„{text}“: {cmd['wer']} steht nicht in eurem Depot.", priority=3,
                       tags=["warning"], dry_run=dry_run)
                continue
            have = float(pos.get("stueck", 0))
            n = have if cmd["alle"] else min(cmd["stueck"] or 0, have)
            if not n:
                continue
            buy_k = float(pos.get("kaufkurs", price))
            f = _fee(doc, n * price)
            gain = (price - buy_k) * n - f - _fee(doc, n * buy_k)
            if n >= have:
                positions.remove(pos)
                rest = "Position komplett verkauft."
            else:
                pos["stueck"] = _int_or(have - n)
                rest = f"Noch {_int_or(have - n)} Stück im Depot."
            doc["budget"] = round(budget + n * price - f, 2)
            _cash(doc, n * price - f)
            changed = True
            _log_trade(state, m, f"Verkauf {_int_or(n)} × {name} à {_eur(price)} € ({'+' if gain >= 0 else ''}{_eur(gain)} € nach Gebühren)")
            notify(f"✅ Verkauf gebucht: {_int_or(n)} × {name}",
                   f"à {_eur(price)} € = {_eur(n * price)} € ({'+' if gain >= 0 else ''}{_eur(gain)} € Gewinn/Verlust nach Gebühren)\n"
                   f"{rest}\nFreies Budget jetzt: {_eur(doc['budget'])} €", priority=2, tags=["white_check_mark"],
                   dry_run=dry_run)

    if changed:
        with depot_file.open("w") as f:
            yaml.dump(doc, f)
    return changed


def _int_or(x: float):
    return int(x) if float(x).is_integer() else round(x, 4)


def _eur(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
