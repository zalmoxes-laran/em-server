# FCN — test walkthrough (dev-stack)

Guida ai test della stack StratiGraph di campo. Presuppone l'FCN acceso:
`./fcn-up.sh` (o `--local-s3d`) e la CA fidata (`./fcn-trust-ca.sh`).

## Tappa 1 — i servizi sono vivi (browser)
- em-server   → https://em.localhost:8443/em/v1/health
- catalog     → https://em.localhost:8443/catalog/health
- MinIO cons. → http://localhost:9001   (credenziali in `.env.dev`)
- Keycloak    → http://localhost:8085   (realm `em-dev`)

## Tappa 2 — gli smoke end-to-end (la prova vera)
Da `~/Documents/GitHub/em-server` (servono i pacchetti `requests` e `minio`:
`pip3 install requests minio --break-system-packages`):

    python3 dev-stack/smoke.py          # AssetStore/MinIO reale: PUT→sha256→GET, auth, promozione DP-76
    python3 dev-stack/smoke_iiif.py     # immagini: MinIO → Cantaloupe → info.json/thumbnail/regione + manifest IIIF
    python3 dev-stack/smoke_catalog.py  # Catalog: registra studi, public/restricted, TTL-publish nasconde il tombstone,
                                        #          e RICOSTRUISCE l'indice dai container in MinIO (l'indice è derivato)

Atteso: tutti verdi, **zero SKIP** (con `minio` installato, gli smoke aprono il bucket e verificano da soli —
non si fidano della parola di em-server).

## Tappa 3 — vedi gli effetti
- MinIO console → bucket `em-assets`: asset nominati col loro **sha256**, prefisso `studies/` coi container.
- Catalog → https://em.localhost:8443/catalog/studies (i due studi) · vista HDT `…/catalog/hdt/<hc2>`.
- IIIF → l'`info.json`/thumbnail stampati da `smoke_iiif` (Cantaloupe pesca da MinIO per sha256).
- Reader (dissemination) → https://em.localhost:8443/catalog/study/<id>/narrative (se lo studio ha una narrativa;
  public = senza token, restricted = 401).

## Tappa 4 — EMStudio come client (rete locale)
EMStudio non è nella stack: è il client. Serve una stanza da aprire, un token, e la CA fidata
(`./fcn-trust-ca.sh`, senza la quale il `wss://` non si apre e sembra un server muto).

**1 · la stanza** (da `~/Documents/GitHub/em-server`):

    python3 dev-stack/seed_rooms.py     # crea `basilica-demo`: 6 US e 5 rapporti

Idempotente, e la stanza di lavoro **non viene sovrascritta** se c'è già (`--force` per rifarla): le altre due
(`mostra`/`scavo`) sono fixture, questa è dove si lavora.

**2 · il token** (700 caratteri, non si digita):

    ./dev-stack/token.sh | pbcopy       # negli appunti
    ./dev-stack/token.sh --claims       # cosa c'è dentro, quando una stanza risponde 4401

Dura un'ora: se la stanza smette di accettarlo, è la scadenza, non un guasto.

**3 · EMStudio** (da `~/Documents/GitHub/EMStudio/frontend`; `npm run serve` serve la `dist/`, quindi
`npm run build` almeno una volta):

    npm run build && npm run serve      # → http://localhost:4173

Nell'app, in quest'ordine:
- **Impostazioni ▸ Live sync** → `URL` = `https://em.localhost:8443/em` (la BASE, non l'endpoint: il
  `/v1/rooms/<stanza>/ws` lo compone `hub.ts`), `Stanza` = `basilica-demo`;
- poi il pulsante **Mode ▸ Hub** nella toolbar: **è lì che l'app chiede il token**, con un prompt del browser.
  Non è un campo delle impostazioni di proposito — il token vive in memoria per la sessione e non viene scritto
  da nessuna parte (`main.ts:4813`, `hubToken`).

**4 · la prova senza mani** (da `EMStudio/frontend`, usa il client VERO — `SyncClient` e `roomUrl`):

    node scripts/check-room-live.mjs    # due client, un edit, la presenza: 17 check

Prende il token da `dev-stack/token.sh` e la CA da `~/caddy-em-root.crt` da sé. Per provare la porta diretta
invece di Caddy: `EM_HUB_BASE=http://localhost:8000 node scripts/check-room-live.mjs`.

**5 · il test forte, a due macchine**: un **secondo** EMStudio sull'altro Mac, `./fcn-up.sh <mac>.local` (il nome
Bonjour, mai un IP nudo → rompe il TLS della CA interna), stessa stanza `basilica-demo` → editi di qua, compare
di là in tempo reale. Sull'altro Mac va copiato e fidato anche `caddy-em-root.crt`.

## Note
- La ROOT `/` è vuota: apri percorsi veri (`/em/v1/health`, `/catalog/…`, `/iiif/…`).
- Certificato rifiutato dal browser → `./fcn-trust-ca.sh` (una volta; e dopo ogni `--wipe`).
- Altro computer: serve un **hostname** (Bonjour `.local` / hosts / dominio), mai un IP nudo (rompe il TLS
  della CA interna), e le due macchine devono vedersi in rete (hotspot che isola → travel-router / Tailscale).
- Spegni: `./fcn-down.sh` (o `--stop` / `--wipe` / `--colima`).
