# Revue bancaire : pilote multi-entreprises

Activer `BANK_REVIEW_ENABLED=true` sur un dossier de test avec la console
opérateur. Le défaut `false` conserve le rapprochement historique. Le mode
pilote remplace la nouvelle affectation automatique par une proposition :
même un paiement intégral nécessite une décision du comptable. Les lignes
déjà rapprochées restent inchangées. Ce mode concerne les dossiers
multi-entreprises MA/MAD, pas le moteur de démonstration mono-entreprise.

## Scénarios pris en charge

- Paiement partiel : facture 1 200 MAD, débit 500 MAD, référence exacte.
  Après approbation : payé 500, statut `Partiellement payee`.
- Deuxième paiement : débit 700 MAD avec la même référence. Une nouvelle
  proposition part du payé 500 ; après approbation, payé 1 200 et `Payee`.
- Paiement groupé : références explicites séparées par `;`, `,`, `|` ou
  retour à la ligne. Les factures doivent concerner le même tiers et la
  somme exacte de leurs restes à payer doit correspondre au mouvement.
- Une facture manquante arrive plus tard : les opérations non rapprochées
  sont relues après import et à chaque cycle pour produire une proposition.

Aucune affectation par ressemblance de montant. Débit = achats, crédit = ventes.
Le montant doit être positif, fini et exprimé au centime. Référence ambiguë,
surpaiement, facture déjà soldée, plusieurs tiers, opération répétée ou sens
indéterminé empêchent l'approbation. Un paiement groupé partiel ou des frais
déduits d'un règlement restent à résoudre par le comptable. Les avoirs ne
sont pas déduits automatiquement du reste à payer.

## Circuit de décision

1. Ouvrir la console locale authentifiée (`app.operations serve`).
2. Déplier la proposition, lire les références, montants et cibles.
3. Saisir un motif, puis approuver ou refuser. La décision, le compte
   opérateur, la révision et le contenu approuvé sont conservés dans
   `bank_review_audit`. La console utilise le compte partagé `operator` ;
   elle ne fournit pas encore des identités individuelles ou rôles clients.
4. Le prochain cycle vérifie à nouveau la banque et chaque facture avant
   écriture. Une modification exige une nouvelle revue. Les comptes banque
   et tiers doivent être configurés, sinon aucune écriture de paiement.

Alternative administrateur :

```sh
python -m app.operations --db /app/data/demo.db bank-decision \
  --company client-demo --bank TX-0001 --revision REVISION_AFFICHEE \
  --decision approve --note "Reference et justificatif verifies"
```

Les décisions HTTP exigent authentification, jeton anti-CSRF et appartenance
au dossier actif affiché. Les exports HTML restent sans formulaire d'écriture.
Ne pas exposer le serveur sur Internet ; utiliser le tunnel SSH existant.

## Reprise et limites

Une proposition conserve le payé initial et le payé cible. Les écritures
Sheets sont absolues ; les pièces de journal sont propres à chaque mouvement
et facture. Après interruption, une reprise accepte uniquement les montants
initiaux ou les cibles approuvées. Le journal Sheets est vérifié et complété
si son insertion a échoué après la persistance SQLite. Les écritures ne sont
pas une transaction distribuée : des effets partiels peuvent être visibles
jusqu'à la reprise. Un changement après début d'application passe en
`conflict`, conservant le plan figé pour investigation, sans réaffectation.

La lecture est limitée à 5 000 lignes par onglet ; atteindre cette borne
interrompt la revue plutôt que de conclure sur une vue tronquée. Chaque cycle
actif lit les trois onglets et chaque application les relit : vérifier les
quotas Google lors du pilote. Aucun nouvel appel IA n'est nécessaire.

Le rapprochement final est visible dans les colonnes L/M du relevé, le payé
O/P des factures, le journal et la console. Ce pilote n'ajoute pas de ligne
à l'ancien onglet `08_RAPPROCHEMENT` et ne déclenche aucun transfert bancaire.

## Recette avant activation générale

Tester un dossier isolé : paiement partiel, deuxième règlement, groupe de
factures du même tiers, facture tardive, refus, changement après accord,
relecture du relevé et interruption au milieu du journal. Vérifier payé,
statuts, journal équilibré et absence de doublons. Sauvegarder la base avant
déploiement et effectuer un test réel des connecteurs dans ce dossier.
