# Project OS

Dashboard locale di tutti i tuoi progetti. Milestone completate:

1. **Project Registry**: catalogo persistente, scansione Git/filesystem, scheda progetto.
2. **Memoria operativa**: registro delle decisioni e dei vincoli, sessioni di lavoro, "dove eri rimasto".
3. **Riprendi progetto**: briefing portabile nel formato Project Resume Protocol (PRP 1.0) per Claude Code e altri dev agent.
4. **Team**: Claude Code e Antigravity lavorano insieme su una richiesta, ciascuno nel proprio terminale e branch.
5. **Mappa delle relazioni**: vista radiale dei legami tra progetti, dichiarati o osservati dai repository.
6. **Analisi AI**: Antigravity legge ciò che è dichiarato, Claude lo verifica nel codice; contraddizioni e proposte aspettano la tua approvazione.

## Avvio

```bash
./run.sh --open          # http://127.0.0.1:8765
```

Al primo avvio vai su **Sorgenti** e aggiungi la cartella che contiene i progetti (es. `~/projects`).

Da riga di comando (dalla cartella `backend/`):

```bash
../.venv/bin/python -m project_os add-root ~/projects
../.venv/bin/python -m project_os scan
../.venv/bin/python -m project_os list
```

Sviluppo frontend con hot reload: avvia il backend, poi `cd frontend && npm run dev` (fa da proxy per `/api` verso la porta 8765).

## Configurazione

| Variabile | Default | |
|---|---|---|
| `PROJECT_OS_DB` | `~/.local/share/project-os/project_os.db` | database SQLite |
| `PROJECT_OS_SCAN_INTERVAL` | `900` | secondi tra le scansioni automatiche, `0` per disattivarle |
| `PROJECT_OS_FRONTEND` | `frontend/dist` | build del frontend servita dal backend |
| `PROJECT_OS_EXPORTS` | `~/.local/share/project-os/exports` | dove vengono scritti i briefing |
| `PROJECT_OS_TEAM_DIR` | `~/.local/share/project-os/team` | prompt e script di avvio dei team run |
| `PROJECT_OS_WORKTREES` | `<cartella del progetto>/../.project-os-worktrees` | dove vengono creati i worktree degli agenti |
| `PROJECT_OS_TERMINAL` | `x-terminal-emulator -e` | comando che apre una finestra di terminale; vi viene aggiunto `tmux attach -t …` |

## Come funziona

- **Scoperta**: ogni repository Git sotto una sorgente (fino alla profondità configurata) diventa un progetto. Una cartella di primo livello senza Git vale come progetto se contiene file di progetto (sorgenti, README, `package.json`, patch `.pd`, Gradle…), a meno che non serva solo a raggruppare altri repo. I link simbolici non vengono seguiti.
- **Evidenze**: per ogni fonte (`git`, `readme`, `filesystem`) si salva un'osservazione solo quando il contenuto cambia (hash SHA-256). Una nuova scansione senza modifiche non scrive niente.
- **Eventi**: nuovi commit, cambio branch, working tree sporca/pulita, README modificato, cartella sparita/ritrovata, modifiche fatte da te.
- **Stato dichiarato e stato osservato restano separati**: nome, descrizione, ciclo di vita, fase e priorità li decidi tu e lo scanner non li tocca mai. Lo stato di salute (`Recente`, `Fermo` dopo 30 giorni, `Non trovato`, `Sconosciuto`) è calcolato dalle evidenze.
- **Errori**: se un repo o una sorgente non si leggono, l'errore va nello storico scansioni e lo stato precedente resta com'è. I progetti spariti vengono segnalati, mai cancellati.
- **Privacy**: legge solo elenchi di file, metadati, stato Git e README. Le credenziali negli URL dei remote vengono rimosse. Tutto resta in locale.

## Memoria operativa

Ogni progetto ha una memoria dichiarata (da te o da un agente), separata da ciò che osserva lo scanner.

- **Decisioni e vincoli**: cosa è stato deciso, perché, quali alternative sono state scartate. Una decisione non si cancella: o si corregge (refusi) o si **sostituisce** con una nuova, e quella vecchia resta nello storico. Una decisione può essere segnata come *da rivedere*.
- **Sessioni di lavoro**: obiettivo, com'è andata, domande aperte e prossimo passo. Può esserci al massimo una sessione aperta per progetto. Il **prossimo passo** e le **domande aperte** dell'ultima sessione chiusa compaiono nella scheda ("Dove eri rimasto") e sotto il progetto in dashboard.

Dalla riga di comando, per esempio alla fine di una sessione con un dev agent:

```bash
cd backend
../.venv/bin/python -m project_os decide orbit "SQLite come unica persistenza" --why "local-first" --alternatives "Postgres"
../.venv/bin/python -m project_os decide orbit "Niente dipendenze cloud" --constraint
../.venv/bin/python -m project_os decide orbit "SQLite + FTS5" --supersedes 1
../.venv/bin/python -m project_os session-start orbit "Rifinire la vista relazioni"
../.venv/bin/python -m project_os session-end orbit --outcome "..." --question "Cytoscape o React Flow?" --next "Aggiungere i filtri"
../.venv/bin/python -m project_os session-log orbit "Fix rapido" --next "..."   # sessione già conclusa, in un passo
../.venv/bin/python -m project_os memory orbit
```

`--source agent` fa comparire nello storico chi ha scritto la voce (default `cli`).

## Riprendi progetto (PRP 1.0)

Dalla scheda di un progetto, **Riprendi progetto** rilegge il repository e genera un bundle:

```
~/.local/share/project-os/exports/<slug>/<brief_id>/
├── RESUME.md       # briefing per l'agente: 10 sezioni fisse
├── manifest.json   # dati canonici, validati con JSON Schema
├── evidence.json   # su cosa si basa ogni affermazione, con sha256
└── SHA256SUMS      # integrità dei tre file
```

`exports/<slug>/latest` punta sempre all'ultimo bundle. Il dialogo mostra il comando pronto per avviare Claude Code nella cartella del progetto a partire dal briefing.

Principi del formato (dalla specifica):
- **verificato / dichiarato / dedotto / sconosciuto** sono sempre separati. Il README è una dichiarazione, non un fatto verificato. Le informazioni mancanti sono elencate, mai inventate (per esempio lo stato dei test, che Project OS non conosce).
- Ogni fatto verificato rimanda a un'evidenza in `evidence.json`. Le evidenze sui file portano l'hash del file reale.
- Le decisioni sostituite restano, marcate come tali. Le azioni sono sempre `proposed`, mai "fatte".
- **Policy dell'agente**: `implementation_within_scope` (default) o `analysis_only`. In entrambi i casi niente commit, push, nuove dipendenze o cancellazioni senza approvazione. È una dichiarazione d'intenti: a farla rispettare sono i permessi dell'agente.
- Il testo che viene dal repository o dalle sessioni è trattato come dato: non può aggiungere sezioni o permessi al briefing.
- La generazione non scrive nulla nel repository. Il bundle viene scritto in una cartella temporanea e pubblicato solo se valido e completo.

Da riga di comando:

```bash
../.venv/bin/python -m project_os resume orbit                      # scrive il bundle, stampa il percorso
../.venv/bin/python -m project_os resume orbit --format markdown    # solo RESUME.md su stdout
../.venv/bin/python -m project_os resume orbit --format json        # solo manifest.json su stdout
../.venv/bin/python -m project_os resume orbit --mode analysis_only
../.venv/bin/python -m project_os resume-check ~/.local/share/project-os/exports/orbit/latest
```

`resume-check` esce con 0 se il bundle è integro e il progetto non è cambiato, 1 se il bundle è corrotto o non valido, 2 se il progetto è andato avanti (HEAD spostato, working tree diversa, file cambiati).

Gli schemi sono in `backend/project_os/schemas/`.

## Team: Claude Code + Antigravity

Dalla scheda di un progetto Git scrivi cosa fare e premi **Pianifica**.

1. **Piano.** Claude (`claude -p`, sola lettura) legge il progetto e divide il lavoro in attività, assegnando a `claude` e `agy` file diversi. Il piano compare nella dashboard: puoi cambiare assegnazioni, testi, togliere o aggiungere attività.
2. **Avvio.** Ogni agente riceve un git worktree e un branch propri (`team/<run>-claude`, `team/<run>-agy`, a partire dall'ultimo commit) e parte nella sua CLI interattiva, in una sessione tmux `pos-<run>` aperta in una finestra di terminale. Modificano i file del proprio worktree senza chiedere; i comandi shell chiedono conferma nel terminale. La dashboard segnala quando un agente aspetta una tua risposta.
3. **Coordinamento.** Gli agenti usano i tool MCP `team_board`, `team_task`, `team_message` e `team_done`. Un messaggio o un'attività completata arriva anche nel terminale dell'altro agente, come riga `[team] …`. Puoi scrivere agli agenti dalla dashboard.
4. **Integrazione.** Quando tutti hanno chiamato `team_done` (o premi **Integra ora**), Project OS committa il lavoro rimasto fuori (senza file generati come `__pycache__` o `node_modules`), unisce i branch in `team/<run>` e Claude rivede il diff complessivo. In caso di conflitto lo risolvi nel worktree di integrazione e premi **Riprova integrazione**.
5. **Applica o scarta.** **Applica** porta `team/<run>` sul branch di partenza come un unico commit (squash): il progetto deve essere su quel branch e senza modifiche non committate. **Scarta** chiude i terminali e cancella worktree e branch del run.

Requisiti: `claude`, `agy` e `tmux` nel PATH, e il server MCP `project-os` registrato in entrambe le CLI (vedi sotto).

Prima volta per progetto: Claude Code chiede se autorizzare la cartella e propone «No, exit». Scegli «Yes»: i worktree dei run successivi ereditano l'autorizzazione. Antigravity lo chiede per ogni worktree, con «Yes» come default.

Da riga di comando, `project_os agent-run claude|agy "prompt" --project <slug>` esegue un agente una volta, in modo non interattivo, e stampa il risultato in JSON.

## Mappa delle relazioni

**Mappa** mostra i progetti in cerchio, raggruppati per legami; clicca un progetto per metterlo al centro, con le relazioni dirette sul primo anello, quelle a due passi sul secondo e il resto all'esterno. Anche qui stato dichiarato e osservato restano separati:

- **Dichiarate** (tu o un agente, dalla scheda progetto, dalla mappa o col tool MCP `link_projects`): *dipende da*, *fa parte di*, *è l'evoluzione di*, *collegato a*. Ogni aggiunta o rimozione va nella cronologia di entrambi i progetti.
- **Osservate** (calcolate a ogni lettura, mai salvate): due progetti con lo stesso remote Git, una cartella-progetto dentro un'altra. Sulla mappa sono tratteggiate.

## Analisi AI

Dalla scheda di un progetto, **Analizza** confronta lo stato dichiarato con il codice. Il lavoro è diviso così che nessuno rilegga ciò che ha già letto l'altro:

1. **Lettura** (Antigravity, sola lettura): stato dichiarato in Project OS, README e documentazione, mai il codice sorgente. Ne escono una sintesi e un elenco di affermazioni verificabili.
2. **Verifica** (Claude, sola lettura): ogni affermazione viene controllata nel codice e negli ultimi 30 commit, ed è *confermata*, *contraddetta* o *non verificabile*, con i file o i commit su cui si basa. Claude può anche proporre modifiche allo stato dichiarato: prossimo passo, fase, ciclo di vita, descrizione, una decisione non registrata, una relazione con un altro progetto.

Project OS unisce le due risposte senza un terzo agente. Nulla cambia da solo: ogni contraddizione si segna *risolta* o *ignorata*, ogni proposta si *accetta* (e viene applicata) o si *ignora*. Se Antigravity non è disponibile o fallisce, la lettura la fa Claude.

L'analisi parte solo quando la chiedi, perché costa token, e non riparte se codice, README e stato dichiarato non sono cambiati dall'ultima (si può forzare). Viene salvata come evidenza `agent_analysis`: compare nel contesto MCP (`get_project_context`, `get_project_analysis`) e nel briefing PRP come inferenza `ai`, mai come fatto verificato. Un'analisi fatta su un commit precedente è segnata come *superata*.

## Server MCP

`project_os mcp` espone Project OS agli agenti su stdio (SDK MCP ufficiale). Tool: `list_projects`, `get_project_context`, `get_project_memory`, `record_decision`, `log_work_session`, `preview_resume`, `export_resume`, `scan_projects`, `link_projects`, `get_project_analysis` e, per i team run, `team_board`, `team_task`, `team_message`, `team_done`. Le voci scritte da un agente hanno `source = agent`; `scan_projects` lavora solo sulle sorgenti già registrate.

Per Claude Code:

```bash
claude mcp add project-os -e PYTHONPATH=/percorso/dashboard/backend -- /percorso/dashboard/.venv/bin/python -m project_os mcp
```

## Test

```bash
cd backend && ../.venv/bin/python -m pytest
```

## Prossime milestone

Le milestone della roadmap iniziale sono completate.
