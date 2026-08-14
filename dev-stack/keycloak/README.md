# The dev realm

`realm-em-dev.json` is imported by the dev-stack Keycloak at start-up. Everything
in it is a **development** credential and none of it is ever a production one:
the real realm is the shared Keycloak the Ansible role points at.

**The file itself carries no comments on purpose.** Keycloak's importer
deserializes strictly and refuses an unknown key — a `_comment` field at the top
stops the container from starting, with a Jackson stack trace that says nothing
about realms. So the explanation lives here.

What it seeds, and why each piece is needed to get a token with `curl`:

| piece | why |
|---|---|
| realm `em-dev` | the isolated realm this stack validates against |
| client `em-server` | confidential (`em-dev-secret`), **service accounts ON** so `client_credentials` works, **direct access grants ON** so a password grant works too — which is what a human uses |
| mapper `audience` | **the one that is always missing.** Without it the token's `aud` is `account`, and em-server answers `403 … issued for another client`. It is the single most common reason a correct-looking token is refused |
| mapper `orcid` | puts the user's ORCID iD in the token, so the room stamps an identity (`_identity()` in `app/ws.py` reads `orcid` first) instead of leaving edits unsigned |
| user `dev` / `dev` | the human; carries the ORCID attribute |

No custom scope is required, so `OIDC_REQUIRED_SCOPE` stays unset.

Changing any of this means changing **this file**, not only `.env.dev`: the env
file selects which realm/client to ask for, the JSON is what actually exists.
