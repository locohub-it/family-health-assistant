# family-health-assistant — istruzioni repo

Progetto amatoriale pubblico, vedi `README.md`. Repo: github.com/locohub-it/family-health-assistant.

## Regola di sincronizzazione con git

Quando un pezzo di lavoro **funziona** (bot/servizio testato, non un
tentativo a metà), fare commit e push su `origin`.

Non fare commit per ogni piccola modifica intermedia: si accumula in
locale e si committa quando il risultato è stabile e funzionante, con un
messaggio che descrive cosa funziona ora.

## Repo pubblico

Niente segreti, ID Telegram reali, dati sanitari o percorsi personali nei file
committati (`.env` e `data/` sono in `.gitignore`). Esempi e test usano dati inventati.
