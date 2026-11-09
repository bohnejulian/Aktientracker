# Aktien- und News-Tracker fürs Planspiel Börse

Prüft werktags von 7 bis 23 Uhr alle 15 Minuten eure Aktien und schickt Alarme per ntfy aufs Handy:

- 📈📉 **Starke Kursbewegung**: ab ±3 % zum Vortag, dann erneut bei ±5 %, ±7 % … (einmal pro Stufe und Tag)
- 🎯 **Kursmarken**: wenn ein selbst gesetzter Kurs (`ueber` / `unter`) gerissen wird
- 📰 **News**: neue Schlagzeilen zu euren Aktien, eurer Watchlist und dem Gesamtmarkt (Google News)
- 💡 **Kaufideen**: werktags 8:30 Scan über DAX, MDAX, TecDAX, EuroStoxx50, Nasdaq 100 und Dow (ca. 250 Aktien), Top 5 mit Begründung und Betrag
- 🗓 **Quartalszahlen**: Warnung am Vortag (freitags fürs Wochenende/Montag)
- 🛑 **Auto-Stop-Loss**: nachgezogener Stop pro Position, Alarm wenn er gerissen wird
- 🤖 **KI-News** (optional): mit Secret `ANTHROPIC_API_KEY` bewertet Claude jede Schlagzeile nach Relevanz und Richtung und filtert Rauschen raus
- 📊 **Depot-Übersicht**: werktags um 9:15 und 17:45 Uhr, mit Klumpenrisiko-Warnung

Jeder Kurs- und News-Alarm enthält eine kurze **Einschätzung** nach festen Chart-Regeln:
Horizont (langfristig halten / kurzfristig-spekulativ), Signal (z. B. überkauft, Rücksetzer im Aufwärtstrend),
Betragsvorschlag aus dem Bot-Budget (`budget` in depot.yaml, je wilder die Aktie, desto kleiner) und eine Stop-Loss-Idee.
Das ist keine Anlageberatung, sondern eine Entscheidungshilfe fürs Planspiel.

Kosten: 0 € (läuft auf GitHub Actions, ca. 1.500 der 2.000 Freiminuten pro Monat).

## Einrichtung (einmalig, ca. 5 Minuten)

1. **ntfy-App** installieren (iOS/Android), auf **+** tippen und diesen Kanal abonnieren:
   `planspiel-b730f1fbab24`
   Alle im Team abonnieren denselben Kanal. Den Namen nicht öffentlich teilen, sonst kann jeder mitlesen.
2. Im GitHub-Repo: **Settings → Secrets and variables → Actions → New repository secret**
   Name: `NTFY_TOPIC`, Wert: `planspiel-b730f1fbab24`
3. **Actions**-Tab → „Aktien-Tracker“ → **Run workflow** (Haken bei „Testnachricht“ lassen).
   Nach ca. 1 Minute sollte „✅ Tracker läuft!“ auf dem Handy erscheinen.

## Drei ntfy-Kanäle

| Kanal | Wofür |
|---|---|
| `planspiel-b730f1fbab24` | Alle Alarme, Kaufideen, Übersichten |
| `planspiel-b730f1fbab24-depot` | Hier schreibt **ihr** rein: `kauf SAP 5`, `kauf SAP 5 274,50`, `verkauf Nvidia alle`, `budget 8000`, `status`, `hilfe` |
| `planspiel-b730f1fbab24-bot` | Alle 15 Min. (leise) Stand der Bot-Käufe mit Empfehlung und News |

Die „Gekauft“-Knöpfe unter den Kaufideen schicken automatisch die passende Nachricht in den Depot-Kanal.
Der Bot bucht sie beim nächsten Lauf (max. ~15 Min.) in depot.yaml ein und bestätigt im Hauptkanal.

## Depot pflegen

Nach jedem Kauf/Verkauf im Planspiel einfach `depot.yaml` bearbeiten (geht direkt auf github.com über das Stift-Symbol).
Es reicht die ISIN aus dem Planspiel, den Kurs sucht der Tracker selbst (bevorzugt Börse Stuttgart). Nach einem Kauf auch `budget` runtersetzen.

## Gut zu wissen

- GitHub startet geplante Läufe manchmal ein paar Minuten zu spät, das ist normal.
- Yahoo-Kurse können bis zu 15 Minuten verzögert sein. Die Kurse im Planspiel selbst sind maßgeblich.
- GitHub pausiert geplante Workflows, wenn 60 Tage lang nichts am Repo geändert wurde. Wer regelmäßig `depot.yaml` pflegt, merkt davon nichts.

## Manuell auslösen

Actions → „Aktien-Tracker“ → Run workflow: „Testnachricht“ schickt Übersicht + Beispiel, „Kaufideen-Scan“ startet den Scan sofort.
