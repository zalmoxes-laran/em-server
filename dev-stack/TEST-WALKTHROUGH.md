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
EMStudio non è nella stack: è il client.

    cd ~/Documents/GitHub/EMStudio/frontend && npm run serve

Poi in EMStudio connettiti alla room dell'FCN: https://em.localhost:8443/em
Test forte: un **secondo** EMStudio sull'altro Mac (via il nome Bonjour `<mac>.local`, non un IP) sulla stessa
room → editi di qua, compare di là in tempo reale.

## Note
- La ROOT `/` è vuota: apri percorsi veri (`/em/v1/health`, `/catalog/…`, `/iiif/…`).
- Certificato rifiutato dal browser → `./fcn-trust-ca.sh` (una volta; e dopo ogni `--wipe`).
- Altro computer: serve un **hostname** (Bonjour `.local` / hosts / dominio), mai un IP nudo (rompe il TLS
  della CA interna), e le due macchine devono vedersi in rete (hotspot che isola → travel-router / Tailscale).
- Spegni: `./fcn-down.sh` (o `--stop` / `--wipe` / `--colima`).
