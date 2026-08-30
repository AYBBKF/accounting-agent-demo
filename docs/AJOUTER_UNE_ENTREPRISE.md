# Ajouter une entreprise a l'agent comptable — sans toucher au code

L'agent multi-entreprises ne connait que les societes declarees par
l'exploitant. Aucun email, aucun sujet, aucun nom de fichier ne peut en
creer une : c'est la garantie centrale du systeme, et cette procedure est
donc le SEUL chemin d'ajout.

## 1. Preparer la declaration

Chaque entreprise est un objet JSON dans la variable d'environnement
`COMPANIES_JSON` (une liste). Champs :

| Champ | Obligatoire | Role |
|---|---|---|
| `company_id` | oui | identifiant technique, minuscules/chiffres/tirets, immuable |
| `inbound_aliases` | oui | alias Gmail de reception (ex. `boite+societe@gmail.com`) — un alias n'appartient qu'a UNE entreprise |
| `legal_name` | pour activer | raison sociale exacte |
| `ice` | recommande | identifiant commun de l'entreprise |
| `country`, `currency` | pour activer | ex. `MA`, `MAD` |
| `allowed_vat_rates` | pour activer | ex. `["0","7","10","20"]` |
| `telegram_chat_id` | pour activer | canal de notification |
| `status` | non | `PENDING_CONFIGURATION` par defaut ; `ACTIVE` seulement volontairement |
| `sheet_id`, `drive_folder_id` | non | laisser vides : le bootstrap les cree |
| `allowed_admin_senders` | non | expediteurs autorises a utiliser le tag `[ACCOUNTING:<id>]` |

Exemple d'ajout de la societe `nouvelle-sarl` a une configuration qui
sert deja `xblaste` :

```json
[
  {"company_id": "xblaste", "inbound_aliases": ["faridrani438+xblaste@gmail.com"], "...": "declaration existante inchangee"},
  {
    "company_id": "nouvelle-sarl",
    "inbound_aliases": ["faridrani438+nouvellesarl@gmail.com"],
    "legal_name": "NOUVELLE SARL",
    "ice": "001234567000089",
    "country": "MA",
    "currency": "MAD",
    "allowed_vat_rates": ["0", "7", "10", "20"],
    "telegram_chat_id": "999653395"
  }
]
```

## 2. Poser la variable et redemarrer

Dans Hostinger (ou l'hebergeur du conteneur), mettre a jour
`COMPANIES_JSON` puis redeployer le projet. Au demarrage, l'agent :

1. journalise chaque entreprise declaree (tracabilite de l'ajout) ;
2. **copie** le classeur modele (`TEMPLATE_SHEET_ID`) — l'original n'est
   jamais modifie ni vide — et ne garde de la copie que la structure :
   onglets, en-tetes, formules, validations, formats ;
3. cree le dossier Drive de la societe sous `DRIVE_ROOT_FOLDER_ID` ;
4. enregistre `sheet_id` et `drive_folder_id` au registre ;
5. verifie les onglets obligatoires ;
6. passe l'entreprise a `ACTIVE` seulement si TOUT a reussi.

Le bootstrap est idempotent : un redemarrage ou une reprise apres panne
reutilise le classeur et le dossier deja crees, jamais de doublon.

## 3. Verifier

Dans les journaux de demarrage, chercher :

    Entreprise declaree par l'exploitant : nouvelle-sarl (alias : ...)
    Entreprise activee : nouvelle-sarl
    Etat multi-tenant : N declaree(s), M ecrivable(s) : ...

Si l'entreprise reste `PENDING_CONFIGURATION`, la ligne d'avertissement
donne la liste exacte des champs manquants — les fournir puis
redemarrer. Tant qu'elle n'est pas ecrivable, ses emails partent en
quarantaine sans aucune ecriture : rien n'est perdu, rien n'est invente.

## Ce que la procedure ne permet PAS

- Reactiver par simple redeclaration une entreprise `SUSPENDED` : le
  statut est une decision, il se change par action explicite.
- Rattacher un alias deja utilise par une autre societe : la base le
  refuse (contrainte d'unicite).
- Modifier `company_id` : l'identifiant est structurellement immuable.
- Faire naitre une entreprise depuis un email, meme d'un administrateur.
