# accounting-agent-demo

Demo jetable, mono-conteneur, isolee de tout autre projet. **Aucune donnee
reelle** : factures et releves bancaires sont entierement fictifs
(marques "DEMO").

Perimetre volontairement reduit par rapport au projet complet
`client-accounting-agent` :
- 1 seul conteneur (bot Telegram + logique), 1 seule image publique.
- SQLite local, pas de Postgres/Redis/worker.
- La seule integration externe est la synchronisation Google Sheets
  optionnelle, via la connexion Composio deja active (voir plus bas) ;
  pas de Gmail/Drive, pas de MCP dans le conteneur lui-meme.
- OpenAI Responses API utilisee uniquement pour une extraction de
  demonstration (texte de facture fictif -> JSON structure), avec repli
  systematique sur `needs_human_review` en cas d'erreur/ambiguite.
- Modele unique : `gpt-5.6-luna` (`OPENAI_MODEL`), effort de raisonnement
  `none` (`OPENAI_REASONING_EFFORT`). Aucun fallback vers un autre modele :
  en cas d'erreur API, la demo retombe sur `needs_human_review`, jamais sur
  un autre modele.
- SQLite reste la base interne principale (source de verite). Un Google
  Sheet est une vue synchronisee optionnelle, jamais indispensable au
  fonctionnement du bot : si non configure, toutes les commandes continuent
  de fonctionner normalement (la sync est simplement ignoree).

## Commandes Telegram

- `/start`, `/help`, `/status`
- `/demo_facture` — genere 5 factures fictives (persistees en SQLite,
  synchronisees vers le Sheet si configure)
- `/demo_releve` — genere un releve bancaire fictif lie aux factures
- `/tva` — simule la TVA (HT -> TVA -> TTC) sur les factures generees
- `/rapprochement` — rapproche factures et releve bancaire
- `/export` — genere et envoie un rapport Excel (.xlsx)
- `/demo_extraction` — teste l'extraction OpenAI (Responses API, `gpt-5.6-luna`)
  sur une facture fictive
- `/sheet` — renvoie le lien du Google Sheet de suivi (si configure)
- `/sync_sheet` — resynchronise toutes les donnees de la session en cours
  vers le Sheet, de facon idempotente (jamais de doublon)
- `/dashboard` — resume des KPI de la session en cours (factures, releve,
  rapprochements, lien du Sheet)

## Synchronisation Google Sheets (optionnelle)

Desactivee par defaut. Pour l'activer, le bot utilise la connexion Google
Sheets **deja active dans Composio** (OAuth) : aucun compte de service
Google Cloud, aucune cle JSON a creer ou a fournir.

1. S'assurer que la connexion Google Sheets est active cote Composio pour
   le compte/utilisateur qui sera utilise par le bot.
2. Renseigner dans `.env` (jamais committe) : `COMPOSIO_API_KEY`, l'un des
   deux `COMPOSIO_USER_ID` ou `COMPOSIO_CONNECTED_ACCOUNT_ID` (identifiant
   du compte/de la connexion Composio), et `GOOGLE_SHEET_ID` (l'ID dans
   l'URL du classeur).

Un bouton Telegram inline « Synchroniser Google Sheets » declenche la meme
synchronisation idempotente que `/sync_sheet` (affiche sous `/sync_sheet`
et `/dashboard`).

OpenAI n'est jamais consomme par la synchronisation Sheets : les tokens
OpenAI ne servent qu'a `/demo_extraction` (extraction de document).

## Securite

- Liste blanche Telegram stricte (`ALLOWED_TELEGRAM_USER_IDS`), fail-closed.
- Aucun secret dans l'image ni dans le code source : tout provient de `.env`
  (voir `.env.example`), jamais committe.
- Aucun port publie : long polling uniquement, ne touche jamais aux ports
  80/443 ni au reverse proxy de l'hote.
- La cle API Composio et les identifiants de compte ne sont jamais
  loggues ; toute erreur d'authentification/synchronisation Sheets est
  capturee et n'interrompt jamais le bot.

## Tests

```
pip install -r requirements-dev.txt
pytest -q
```

Aucun test n'envoie de message Telegram reel ni n'appelle l'API OpenAI
reelle (mocks systematiques).
