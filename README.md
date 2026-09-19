# Family Health Assistant

Assistente medico familiare su Telegram, pensato per genitori e nonni: si manda una **foto**
di un referto, di una ricetta o di una prenotazione e il bot capisce da solo cosa fare, senza
che l'utente scriva nulla.

- **Prenotazione di una visita** → la salva nel database e la aggiunge al **Google Calendar**
  della persona giusta.
- **Referto di analisi** (es. «esami del 25/10/2025») → salva i valori nel database.
- **Domanda in chat** («in base all'ultimo referto, cosa comportano quei valori?») → il bot passa
  alla consultazione: legge lo storico della persona e chiede a un **modello AI** (a scelta dell'admin) di valutarlo.

Tutto si configura da una **pagina web**: chiavi API, utenti (con il loro calendario) e cartella
in cui salvare i documenti.

> **Stato: pronto per la prima prova.**
> Pannello di configurazione; bot Telegram che legge foto e PDF con il modello AI scelto e salva referti,
> appuntamenti, ricette e altri documenti (con «Annulla»); visite sul Google Calendar di ciascuno
> tramite Composio; domande scritte o a voce con risposta del modello AI sui dati salvati.
> Testato con servizi AI, Telegram e Composio simulati.

## Come funziona

```
foto ─> bot Telegram (long polling) ─> solo ID approvati ─> il modello AI decide il tipo di documento
                                                              ├─ prenotazione ─> DB + Google Calendar (Composio)
                                                              ├─ referto      ─> DB + foto nella cartella scelta
                                                              └─ ricetta/altro ─> DB + foto
domanda ─> dati della persona dal DB ─> modello AI scelto (Gemini, Groq…) ─> risposta in italiano
```

- **Domande**: un messaggio scritto o un vocale è una domanda. Il modello riceve lo storico di chi scrive
  (e dei familiari per cui ha inviato documenti, non degli altri) e risponde in italiano semplice,
  distinguendo ciò che legge nei documenti dalle spiegazioni generali («In generale…»). Il bot **non
  cerca sul web** e non conosce la posizione di nessuno: a «l'oculista più vicino» risponde che non può
  e suggerisce il medico di base. Spiega e rimanda al medico: non fa diagnosi.
  Risponde solo a ciò che è stato chiesto, senza saluti né consigli non richiesti. Sulle **visite** fa la
  segretaria, non il medico: giorno, ora, tipo e luogo, più le indicazioni scritte sul foglio di prenotazione
  («portare i documenti delle ultime visite», digiuno…) con le stesse parole, e nient'altro.
- **Visite**: entrano solo in due modi, mai da una frase scritta (così una domanda come «ho appuntamenti il
  26 marzo?» non può essere scambiata per un inserimento): dalla **foto** di una prenotazione o di un documento,
  oppure con **`/visita`**, un passaggio guidato a pulsanti (per chi è → per quando → che visita è → conferma).
  Data e ora si scrivono a mano («26/10 alle 15», «26 ottobre 15:30», «domani alle 9») e sono lette con regole
  fisse, senza intelligenza artificiale: se non si capisce con certezza, il bot chiede di riscriverle.
  **`/appuntamenti`** mostra le prossime visite, ciascuna con i pulsanti *Modifica* (data e ora, tipo, luogo)
  ed *Elimina* (con conferma); anche scrivere esattamente «modifica appuntamento» o «elimina appuntamento»
  apre l'elenco. Sul calendario (anche del coordinatore) la visita viene rifatta o tolta. Ognuno gestisce le
  visite sue e quelle dei familiari per cui ha inviato documenti o segnato visite. I comandi compaiono anche
  nel menu di Telegram.
  Se qualcuno scrive «prenota una visita» (o simili) il bot non segna niente: propone «Vuoi segnare una nuova
  visita?» con un pulsante che apre `/visita`. Le domande sulle visite («quante visite ho a ottobre?») non
  fanno comparire la proposta.
- Il bot parla con Telegram in **long polling**: risponde subito e **non serve aprire nessuna porta**.
  Composio non viene usato per Telegram: il suo toolkit non ha trigger per i messaggi in arrivo né
  un modo per scaricare le foto. Viene usato per Google Calendar, dove serve.
- Risponde **solo agli ID Telegram approvati** dal pannello; gli altri sono ignorati in silenzio.
- Non è un dispositivo medico: non fa diagnosi e invita sempre a sentire il medico.

## Privacy

Le foto dei referti vengono inviate al servizio AI scelto. Con **Gemini**, sul **piano gratuito** dell'API Google può usare i
contenuti inviati per migliorare i suoi prodotti: per dati sanitari conviene un progetto Google
Cloud con **fatturazione attiva**, dove i dati non vengono usati per l'addestramento. I documenti
restano sul tuo server, nella cartella che scegli; il database sta nel volume del container.

Lo stesso vale per **qualunque provider AI** scelto in «Modelli IA»: le foto e le domande vengono
inviate a quel servizio (Groq, DeepSeek, OpenRouter…), con le sue regole sulla conservazione dei dati e
la sua sede. Controlla le condizioni prima di usare dati sanitari veri; un servizio sul tuo server
(per esempio Ollama) tiene tutto in casa.

## Cosa serve

1. **Bot Telegram**: crealo con @BotFather (`/newbot`) e copia il token.
2. **Un servizio AI** con la sua chiave: Gemini (<https://aistudio.google.com/apikey>), Groq, DeepSeek o
   qualunque servizio compatibile OpenAI. Si inseriscono dal pannello, a mano, quanti se ne vuole.
3. **Chiave Composio** (per Google Calendar): la **Project API key** (`ak_…`): su <https://dashboard.composio.dev> scegli
   **Platform** (non «For You»), apri il progetto, poi *Settings → API Keys*. Non la chiave `ck_…` della pagina
   Sessions / AI Client (consumer, per MCP): il pannello la rifiuta. Ogni persona
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

1. **Chiavi API**: token del bot Telegram e chiave Composio.
2. **Modelli IA** (tre riquadri):
   1. **Servizi AI**: inserisci a mano nome, tipo, indirizzo e chiave di ogni servizio che possiedi
      (Gemini, Groq, DeepSeek, OpenRouter, Ollama sul tuo server, qualunque servizio compatibile
      OpenAI, anche futuro). Le chiavi sono salvate cifrate. Il primo servizio diventa quello di
      tutte le funzioni, così si parte subito.
   2. **Modelli da usare**: per la lettura dei documenti e per le risposte scegli un modello
      **principale** e, se vuoi, uno di **riserva**, anche di un altro servizio (per esempio
      principale su Groq e riserva su Gemini). La riserva risponde quando la principale non può:
      limite finito, errore, servizio giù. Scegliendo un servizio, la lista dei modelli si carica da
      sola (con una finestra di attesa se serve) e il pannello sceglie quello adatto; per i documenti
      prova i modelli con un'immagine e tiene il primo che la legge. Si può cambiare dal menu o
      scrivere il nome a mano; un modello scelto non viene mai sostituito.
   3. **Verifica**: «Prova» fa una richiesta vera al modello principale e a quello di riserva e
      mostra l'esito con il motivo esatto se falliscono; «Scegli di nuovo in automatico» rifà la scelta.

   Con Groq i vocali si trascrivono con Whisper e con i limiti di token bassi (il piano gratuito
   ammette circa 8.000 token al minuto) a ogni domanda si inviano solo i documenti più recenti, entro
   un tetto regolabile. I PDF con testo si leggono con qualsiasi modello. Chi aggiorna da una
   versione precedente ritrova chiavi e scelte già fatte: vengono trasformate in servizi da sole.
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
   Ognuno ha il **proprio** calendario Google. In più si può scegliere un **coordinatore**: chi
   coordina le visite riceve sul suo calendario anche quelle di tutti gli altri, con il nome del
   paziente nel titolo (la visita resta anche sul calendario di chi la riguarda; «Annulla» la toglie
   da entrambi).
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
