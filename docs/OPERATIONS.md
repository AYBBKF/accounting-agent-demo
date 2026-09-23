# Exploitation et revue des dossiers

Cette version ajoute une activation contrôlée des nouveaux dossiers, une console
locale de revue en lecture seule et un suivi durable des cycles Gmail. Les
connexions Google et les données existantes restent celles du déploiement.

## Nouveaux dossiers

`AUTO_PROVISION_REQUIRE_APPROVAL=true` est le défaut du constructeur utilisé en
production. Quand la création automatique est activée, le nouvel alias reçoit
un classeur et un dossier Drive, mais reste `PENDING_CONFIGURATION`. La raison
sociale et l'ICE ne sont pas déduits de l'alias. Aucun document n'est traité
avant validation. Les entreprises déjà actives ne sont pas suspendues.

Le paramètre `require_approval=False` du constructeur Python conserve le contrat
historique pour les appels explicites et les anciens tests. Le contournement
par configuration `AUTO_PROVISION_REQUIRE_APPROVAL=false` est réservé à une
démonstration isolée, pas au déploiement commercial.

Depuis le conteneur ou un poste opérateur disposant de la base :

```sh
python -m app.operations --db /app/data/demo.db snapshot --company client-demo
```

Après vérification de l'identité et du paramétrage, préparer un JSON contenant
exactement `legal_name`, `ice`, `country`, `currency`, `allowed_vat_rates`,
`telegram_chat_id` et `account_mapping`. Les six rôles du journal doivent être
explicitement fournis : client, fournisseur, achat, vente, tva_collectee,
tva_deductible. Les comptes bancaires sont optionnels ; leur absence ne doit
pas être masquée. Les numéros de compte sont des chaînes ou des paires
`[numero, libelle]`, validés par le comptable.

```sh
python -m app.operations --db /app/data/demo.db approve \
  --company client-demo --config /chemin/configuration-validee.json \
  --revision REVISION_RETOURNEE_PAR_SNAPSHOT
```

La commande vérifie la révision, la préparation du classeur et l'unicité des
destinations. Activation et événement d'audit sont atomiques. L'acteur est le
compte système exécutant la commande : cet outil suppose un accès administrateur
au serveur. Ce n'est pas une API multi-utilisateur. Le contrôle d'ICE vérifie
sa forme ; il ne consulte aucun registre officiel. MA/MAD est le périmètre
actuellement accepté par cette validation, pas une promesse internationale.

L'email initial n'est pas rejoué par la commande. Il reste éligible à la recherche
Gmail configurée ; ne pas changer sa fenêtre de recherche avant sa reprise.

## Console de revue

```sh
python -m app.operations --db /app/data/demo.db export \
  --company client-demo --output /chemin/console-client.html
```

L'export contient les données du client et doit être protégé comme son dossier.
Il affiche les états, exceptions, liens Drive/Sheets, consommation en tokens et
notes opérateur. Il n'appelle ni Gmail, ni OCR, ni LLM. Les coûts ne sont pas
présentés comme nuls quand la tarification manque. La file est limitée aux 200
plus anciennes exceptions par société ; le total reste exact et la limite est
affichée. `--all` ouvre explicitement la vue administrateur.

Pour une vue actualisée, fournir `OPERATIONS_TOKEN` via l'environnement (au moins
32 caractères aléatoires), puis :

```sh
python -m app.operations --db /app/data/demo.db serve --company client-demo
```

Ouvrir `http://127.0.0.1:8765`, utilisateur `operator`, mot de passe : le token.
Le serveur écoute exclusivement sur loopback ; utiliser un tunnel SSH pour le
consulter à distance. Ne pas l'exposer publiquement. Il refuse les écritures
HTTP et ne charge aucun script externe. Le scope société est fixé au démarrage,
pas choisi dans une URL par le visiteur.

Pour conserver une note sans modifier l'écriture :

```sh
python -m app.operations --db /app/data/demo.db note \
  --company client-demo --document CLE_DOCUMENT --text "Demander une photo lisible."
```

Cette version ne propose pas encore de correction de montants ni de validation
comptable dans le navigateur. Les notes n'effacent pas la quarantaine. La revue
utilise les motifs existants et les originaux archivés ; elle ne refait pas une
extraction pour remplir l'écran.

## Santé et reprise

Le fichier voisin `demo.cycles.json` décrit le dernier cycle, les échecs
consécutifs et la dernière réussite. Trois échecs consécutifs déclenchent une
alerte opérateur ; le premier succès suivant signale la reprise. Le fichier
survit au redémarrage et le dashboard distingue un suivi absent, dégradé ou
périmé. Aucun message d'exception ni secret n'y est écrit. Le heartbeat Docker
continue à mesurer la vie du processus, et non la réussite métier.

La disponibilité de Telegram conditionne la remise des alertes. L'état local
reste consultable si Telegram est indisponible. Un simple redémarrage ne doit
pas être utilisé pour masquer les échecs d'authentification ou les quotas.

Les reprises et notifications sont limitées au tenant, même si plusieurs
sociétés partagent un chat. Le cache des workers revérifie l'état et la
configuration du registre. Une suspension interdit donc une nouvelle reprise.

## Préparation non destructive des classeurs

Seule une copie créée et identifiée par cette version peut être initialisée.
Une copie interrompue avant initialisation peut terminer cette étape ; un
classeur déjà initialisé, fourni par l'opérateur ou précédemment activé n'est
jamais vidé. Les onglets BOT et sauvegardes de quarantaine d'un modèle vivant
sont aussi nettoyés dans la copie neuve pour ne pas transporter ses documents.
Le nettoyage utilise les plages A2:Z sans limite de lignes : choisir un modèle vierge et
vérifié, sans transactions hors de ces plages ni données client dans les
onglets de configuration. La séparation de fichiers ne garantit pas à elle
seule un modèle propre.

Une société SUSPENDED ou DISABLED ne peut pas être réactivée par le bootstrap.
Les nouveaux champs SQLite sont additifs. Un ancien digest respecte le statut
PENDING, mais pourrait réactiver une entreprise au bootstrap : le rollback
doit conserver l'exclusion des sociétés en attente et être testé isolément.

## Vérification avant déploiement

1. Sauvegarder SQLite avec l'API backup et vérifier la restauration sur une
   copie ; enregistrer le digest précédent et le volume utilisé.
2. Exécuter la suite sans supprimer ni neutraliser de test. Sous Windows,
   `python scripts/test_windows.py -q --basetemp=CHEMIN_NEUF` collecte les
   connexions SQLite inaccessibles avant la suppression des fixtures. Le
   répertoire parent de CHEMIN_NEUF doit exister. CI Linux reste la référence.
3. Tester un nouveau dossier et un arrêt/reprise sur un volume isolé avec des
   copies vierges et des emails exclus de la production.
4. Vérifier avant/après les compteurs, empreintes et notifications des tenants
   existants. Ne déployer qu'un digest immuable après CI test/build verte.
5. Aucun port de console n'est ajouté au conteneur de production. La console
   est un outil opérateur optionnel, pas un portail client public.
