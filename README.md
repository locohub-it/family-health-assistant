# Family Health Assistant

Assistente medico familiare su Telegram, pensato per genitori e nonni: si manda una **foto**
di un referto, di una ricetta o di una prenotazione e il bot capisce da solo cosa fare, senza
che l'utente scriva nulla.

- **Prenotazione di una visita** → la salva nel database e la aggiunge al **Google Calendar**
  della persona giusta.
- **Referto di analisi** (es. «esami del 25/10/2025») → salva i valori nel database.
- **Domanda in chat** («in base all'ultimo referto, cosa comportano quei valori?») → il bot passa
  alla consultazione: legge lo storico della persona e chiede a **Gemini** di valutarlo.

Tutto si configura da una **pagina web**: chiavi API, utenti (con il loro calendario) e cartella
in cui salvare i documenti.

> **Stato: pronto per la prima prova.**
> Pannello di configurazione; bot Telegram che legge foto e PDF con Gemini e salva referti,
> appuntamenti, ricette e altri documenti (con «Annulla»); visite sul Google Calendar di ciascuno
> tramite Composio; domande scritte o a voce con risposta di Gemini sui dati salvati.
> Testato con Gemini, Telegram e Composio simulati: la prima prova con le chiavi vere è da fare.

## Come funziona

```
foto ─> bot Telegram (long polling) ─> solo ID approvati ─> Gemini decide il tipo di documento
                                                              ├─ prenotazione ─> DB + Google Calendar (Composio)
                                                              ├─ referto      ─> DB + foto nella cartella scelta
                                                              └─ ricetta/altro ─> DB + foto
domanda ─> dati della persona dal DB ─> Gemini (con ricerca web) ─> risposta in italiano
```

- **Domande**: un messaggio scritto o un vocale è una domanda. Gemini riceve lo storico di chi scrive
  (e dei familiari per cui ha inviato documenti, non degli altri) e risponde in italiano semplice,
  con la ricerca web di Google per spiegare i valori. Spiega e rimanda al medico: non fa diagnosi.
- Il bot parla con Telegram in **long polling**: risponde subito e **non serve aprire nessuna porta**.
  Composio non viene usato per Telegram: il suo toolkit non ha trigger per i messaggi in arrivo né
  un modo per scaricare le foto. Viene usato per Google Calendar, dove serve.
- Risponde **solo agli ID Telegram approvati** dal pannello; gli altri sono ignorati in silenzio.
- Non è un dispositivo medico: non fa diagnosi e invita sempre a sentire il medico.

## Privacy

Le foto dei referti vengono inviate a Gemini. Sul **piano gratuito** dell'API Google può usare i
contenuti inviati per migliorare i suoi prodotti: per dati sanitari conviene un progetto Google
Cloud con **fatturazione attiva**, dove i dati non vengono usati per l'addestramento. I documenti
restano sul tuo server, nella cartella che scegli; il database sta nel volume del container.

## Cosa serve

1. **Bot Telegram**: crealo con @BotFather (`/newbot`) e copia il token.
2. **Chiave Gemini** da <https://aistudio.google.com/apikey>.
3. **Chiave Composio** (per Google Calendar): la **Project API key** (`ak_…`) da <https://platform.composio.dev>,
   nelle impostazioni del progetto. Non la chiave `ck_…` (consumer, per MCP): il pannello la rifiuta. Ogni persona
   collega il proprio account Google dal pannello.
4. L'**ID Telegram** di ciascun utente: basta scrivere a @userinfobot.

## Deploy su Portainer

A ogni push su `main` la GitHub Action esegue i test e pubblica l'immagine
`ghcr.io/locohub-it/family-health-assistant:latest` (amd64 e arm64). Il repo è pubblico, quindi il
pacchetto si scarica **senza credenziali**: non serve nessun token in Portainer.

1. Portainer → **Stacks** → Add stack → nome `famiglia` → *Web editor*: incolla
   `docker-compose.portainer.yml`.
2. *Environment variables*:
   - `SECRET_KEY`: stringa lunga casuale, da conservare
   - `ADMIN_PASSWORD`: almeno 10 caratteri
   - `STORAGE_ROOT`: cartella **dell'host** che il pannello potrà sfogliare, ad esempio il NAS
     montato (`/mnt/nas`) o la cartella Documenti del PC
   - `WEB_PORT`: porta libera sul server (default 8096)
3. **Deploy the stack**, poi apri `http://<ip-del-server>:<WEB_PORT>`.

Aggiornare dopo un nuovo push: Stacks → `famiglia` → *Update the stack* con **Re-pull image**.

## Avvio con docker compose (senza Portainer)

```bash
cp .env.example .env      # imposta SECRET_KEY, ADMIN_PASSWORD e STORAGE_ROOT
docker compose up -d --build
```

Il pannello è su `http://localhost:8080`. **Non esporlo su internet**: è pensato per la rete locale.

## Primo avvio dal pannello

1. **Chiavi API**: token del bot, chiave Gemini, chiave Composio. Sul piano gratuito Gemini ha
   limiti di richieste per modello: il **modello di riserva** entra in azione da solo quando quello
   principale ha finito le richieste o non risponde, e il bot aspetta e riprova se il limite è al minuto.
2. **Cartella** (facoltativo): senza scegliere niente il bot salva in `Documenti`, dentro
   `STORAGE_ROOT`, e la crea da solo. Per cambiarla, sfoglia dentro `STORAGE_ROOT`, crea una
   cartella se serve e scegli «Usa questa cartella» (il pannello controlla che si possa
   scrivere). Se la cartella scelta smette di funzionare, i documenti vanno comunque in
   `Documenti` (e, in ultima istanza, nel volume dei dati) e l'errore compare solo nel pannello,
   mai in chat.
3. **Utenti**: aggiungi di ogni familiare **nome e cognome come sono scritti su ricette e referti** e l'ID
   Telegram: servono ad abbinare ogni documento alla persona giusta, anche quando lo invia un altro
   familiare. Con «Modifica» si correggono i dati (il database esistente si aggiorna da solo). Poi, per ognuno, **Collega**: si apre
   una pagina con il link (e il QR) su cui la persona accede al proprio Google. Lo stesso link lo
   riceve scrivendo `/calendario` al bot. Se ha più calendari, si sceglie quello delle visite.
   Il pulsante **↻ Aggiorna** ricontrolla subito il collegamento di quella persona; se non si
   riesce a verificare, la causa vera compare sotto lo stato.

Password del pannello dimenticata:
`docker compose run --rm famiglia python -m famiglia.set_password`

`SECRET_KEY` cifra le chiavi salvate: se la cambi vanno reinserite. Il database sta in `./data`
(o nel volume `famiglia-data`): da includere nei backup insieme alla cartella dei documenti.

## Sviluppo

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -e '.[dev]'
.venv/bin/pytest
```
