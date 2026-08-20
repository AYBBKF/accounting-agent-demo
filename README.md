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
  systematique sur needs_human_review en cas d'erreur/ambiguite.
- Modele unique : gpt-5.6-luna (OPENAI_MODEL), effort de raisonnement
  none (OPENAI_REASONING_EFFORT). Aucun fallback vers un autre modele :
  en cas d'erreur API, la demo retombe sur needs_human_review, jamais sur
  un autre modele.

## Commandes Telegram

- /start, /help, /status
- /demo_facture - genere 5 factures fictives
- /demo_releve - genere un releve bancaire fictif lie aux factures
- /tva - simule la TVA (HT -> TVA -> TTC) sur les factures generees
- /rapprochement - rapproche factures et releve bancaire
- /export - genere et envoie un rapport Excel (.xlsx)
- /demo_extraction - teste l'extraction OpenAI (Responses API, gpt-5.6-luna)
  sur une facture fictive

## Securite

- Liste blanche Telegram stricte (ALLOWED_TELEGRAM_USER_IDS), fail-closed.
- Aucun secret dans l'image ni dans le code source : tout provient de .env
  (voir .env.example), jamais committe.
- Aucun port publie : long polling uniquement, ne touche jamais aux ports
  80/443 ni au reverse proxy de l'hote.

## Tests

```
pip install -r requirements-dev.txt
pytest -q
```

Aucun test n'envoie de message Telegram reel ni n'appelle l'API OpenAI
reelle (mocks systematiques).
