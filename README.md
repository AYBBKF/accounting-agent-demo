# accounting-agent-demo

Demo jetable, mono-conteneur, isolee de tout autre projet. **Aucune donnee
reelle** : factures et releves bancaires sont entierement fictifs
(marques "DEMO").

Perimetre volontairement reduit par rapport au projet complet
`client-accounting-agent` :
- 1 seul conteneur (bot Telegram + logique), 1 seule image publique.
- SQLite local, pas de Postgres/Redis/worker.
- Pas de Composio/MCP, pas de Gmail/Sheets/Drive.
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

Desactivee par defaut. Pour l'activer, le conteneur a besoin de son **propre**
compte de service Google Cloud, independant de tout compte utilisateur :

1. Creer un projet GCP (ou reutiliser un projet existant) et y activer les
   API Google Sheets et Google Drive.
2. Creer un compte de service, generer une cle JSON.
3. Partager le Google Sheet cible avec l'adresse e-mail du compte de service,
   en acces **Editeur**.
4. Renseigner dans `.env` (jamais committe) : `GOOGLE_SHEET_ID` (l'ID dans
   l'URL du classeur) et soit `GOOGLE_SERVICE_ACCOUNT_JSON` (le contenu JSON
   de la cle sur une seule ligne), soit `GOOGLE_SERVICE_ACCOUNT_FILE` (chemin
   vers un fichier de cle monte dans le conteneur).

OpenAI n'est jamais consomme par la synchronisation Sheets : les tokens
OpenAI ne servent qu'a `/demo_extraction` (extraction de document).

## Securite

- Liste blanche Telegram stricte (`ALLOWED_TELEGRAM_USER_IDS`), fail-closed.
- Aucun secret dans l'image ni dans le code source : tout provient de `.env`
  (voir `.env.example`), jamais committe.
- Aucun port publie : long polling uniquement, ne touche jamais aux ports
  80/443 ni au reverse proxy de l'hote.
- La cle de compte de service Google n'est jamais loggee ; toute erreur
  d'authentification/synchronisation Sheets est capturee et n'interrompt
  jamais le bot.

## Tests

```
pip install -r requirements-dev.txt
pytest -q
```

Aucun test n'envoie de message Telegram reel ni n'appelle l'API OpenAI
reelle (mocks systematiques).
