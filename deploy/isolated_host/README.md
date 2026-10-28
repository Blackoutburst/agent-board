# Préparer deux hôtes textuels isolés sur le serveur existant

Ce paquet prépare l'exécution personnelle de TheScreamer et de Blackoutburst sur
le serveur Linux choisi. Il n'active rien pendant la préparation ou
l'installation. Le Board reste dans son service actuel ; les agents utilisent
deux nouveaux comptes Unix sans privilèges : `bb-agent-thescreamer` et
`bb-agent-blackout`. Leur connexion Codex et leurs appareils Board sont séparés.

Il s'agit du **host textuel** : réponses à des conversations explicitement
choisies (configuration 2), ou activité de tickets explicitement choisis
(configuration 1). Une configuration fixe un seul de ces modes pour chaque
propriétaire. Le parcours de développement/famille Linux reste refusé ; ce paquet
ne qualifie ni son sandbox ni le nettoyage de ses tests. Il ne démarre pas
l'ancien listener, ne sélectionne pas de nouveaux tickets et ne déplace pas les
journaux d'un pilote déjà actif.

Le relevé du 19 octobre 2026 indique Ubuntu, Linux 5.15.0-198-generic x86_64,
Python 3.10.12 et systemd 249.11. Ce relevé n'est pas une preuve d'exécution des
outils fournis. L'installation vérifie à nouveau Linux, l'architecture, Python et
systemd. Le lancement vérifie les empreintes des fichiers.

## 1. Préparer le paquet sans accès aux comptes

La préparation utilise seulement une révision Git **propre et revue**, les
sources publiques de l'adaptateur et de son connecteur, ainsi que deux outils
Linux explicitement fournis par l'opérateur : Codex et son helper `bwrap`.
L'inventaire n'inclut aucun fichier JSON de connexion, cache fournisseur,
historique, capture ou fichier d'authentification. Aucun téléchargement, modèle,
login ou outil fourni n'est exécuté par `prepare`.

Prérequis à relever explicitement :

- les deux identifiants propriétaires du Board, qui doivent être différents ;
- l'adresse HTTPS exacte du MCP ;
- une CLI Codex Linux et son helper de la **même version officielle vérifiée** ;
- le chemin réel de Python système sur le serveur, par exemple
  `/usr/bin/python3.10`, et son SHA-256 ; ne pas utiliser le lien `/usr/bin/python3` ;
- la version CLI annoncée par l'opérateur, conservée comme provenance et
  vérifiée plus tard par `doctor`.

Si les outils Linux manquent, la commande retourne `linux_tools_required` et ne
crée pas de bundle. Un binaire Mach-O du Mac, une architecture différente, un
script, un lien symbolique ou une source modifiée sont refusés. Le contrôle de
l'en-tête ELF et des hashes établit l'identité du paquet, pas sa qualification
Linux ni l'éligibilité de l'abonnement à un service hébergé.

Depuis la racine de la révision revue, remplacer les valeurs entre chevrons :

```sh
python3 -m deploy.isolated_host.manage prepare \
  --source /absolute/clean/agent-board \
  --output /absolute/private/owner-host-bundle \
  --codex /absolute/public-linux-tools/codex \
  --bwrap /absolute/public-linux-tools/codex-resources/bwrap \
  --codex-version <VERSION_EXPLICITE> --architecture x86_64 \
  --python /usr/bin/python3.10 --python-sha256 <SHA256_DU_SERVEUR> \
  --board-endpoint https://board.example.com/mcp \
  --thescreamer-owner <OWNER_THE_SCREAMER> --blackout-owner <OWNER_BLACKOUT>
```

Le résultat privé contient un manifeste et un inventaire SHA-256. Le nom de la
release installée est l'empreinte de ce manifeste. Ne pas insérer de jeton dans
les arguments ; ces paramètres ne contiennent aucune connexion personnelle.

## 2. Examiner puis appliquer la première installation

```sh
python3 -m deploy.isolated_host.manage install --bundle /absolute/private/owner-host-bundle
```

Cette commande affiche le plan uniquement. L'opérateur administrateur pourra
ensuite appliquer exactement le paquet examiné, sur le serveur :

```sh
sudo /usr/bin/python3 -m deploy.isolated_host.manage install \
  --bundle /absolute/private/owner-host-bundle --apply
```

L'installateur :

1. refuse les comptes, domiciles, unités ou liens d'activation préexistants ;
2. copie et revérifie les sources/outils sous
   `/opt/blackoutburst-agent-host/releases/<BUNDLE_ID>`, appartenant à root et
   non modifiables par les agents ;
3. crée les deux comptes système à mot de passe verrouillé et groupe distinct,
   sans accès sudo et sans ajouter de clé SSH ;
4. crée leurs domiciles sous
   `/var/lib/blackoutburst-agent-host/owners/<COMPTE>`, avec répertoires `0700` ;
5. écrit deux unités systemd et recharge seulement les métadonnées des unités.

Il ne crée aucun journal host, connexion Board, dossier d'authentification Codex,
configuration active ou budget. Il ne lance jamais `systemctl start`, `enable`
ou `restart`. Les unités ont `Restart=no`. Le reçu root privé se trouve sous
`/var/lib/blackoutburst-agent-host/installations/`.

C'est un installateur **initial**, pas un outil de mise à niveau. En cas
d'interruption, le reçu indique les comptes/unités déjà créés ; une nouvelle
tentative refuse leur adoption. L'opérateur inspecte l'état partiel avant de
continuer. Aucun compte, domicile, connexion ou journal n'est effacé
automatiquement pour simuler un rollback réussi.

## 3. Connexion personnelle de chaque propriétaire

L'administrateur donne à chaque personne accès à son compte Unix séparé, par
une session de connexion appropriée. Une session peut être ouverte par
`sudo -iu bb-agent-thescreamer` ou `sudo -iu bb-agent-blackout`, mais la personne
concernée effectue elle-même l'authentification. Ne pas remplacer `HOME` ou
`CODEX_HOME`, copier une authentification du Mac ni partager le compte `ubuntu`
comme exécuteur commun. Le launcher refuse un domicile ou une variable
`CODEX_HOME` qui ne correspond pas à ce parcours standard.

Chacun ouvre la connexion officielle de la CLI Codex installée avec son propre
compte ChatGPT, selon le flux proposé par cette version. Aucun login n'est
automatisé par ce paquet. Aucun secret ne doit être collé dans le Board, un
ticket ou cette conversation. L'authentification et son renouvellement restent
gérés par la CLI officielle.

La connexion Board est **distincte** : dans son interface humaine, le
propriétaire crée son appareil et un code d'appairage à usage unique. Dans son
compte Unix, il exécute le connecteur installé et saisit ce code dans l'invite
masquée. Exemple avec un répertoire propre à un profil :

```sh
/usr/bin/python3.10 -I -B /opt/blackoutburst-agent-host/releases/<BUNDLE_ID>/source/connect.py \
  --client other --name "Serveur personnel · profil A" \
  --config-dir /var/lib/blackoutburst-agent-host/owners/<COMPTE>/.config/blackoutburst-board/profil-a
```

`--client other` crée une connexion `other.json` pour l'exécuteur sans ajouter de
MCP au fichier de configuration Codex. Le propriétaire lie ce nouvel appareil
au profil et aux projets permis dans le Board. Pour une révocation indépendante
par profil, apparier un appareil distinct : une clé d'appareil peut utiliser
tous les profils qui lui sont explicitement liés. L'isolation Unix sépare les
deux propriétaires ; elle ne crée pas une isolation cryptographique entre les
profils du même compte Unix. Les administrateurs du serveur restent de confiance.

## 4. Diagnostic et configuration, toujours désactivée

Toutes les commandes suivantes s'exécutent dans le compte Unix concerné, avec
le même préfixe :

```sh
/usr/bin/python3.10 -I -B /opt/blackoutburst-agent-host/releases/<BUNDLE_ID>/owner_entry.py \
  --release /opt/blackoutburst-agent-host/releases/<BUNDLE_ID> --owner <COMPTE> doctor
```

`doctor` vérifie le mode d'authentification officiel et ne lance pas de modèle.
Une fois sa réussite examinée, `probe --model <MODELE_ACCESSIBLE> --timeout 120`
demande séparément une réponse minimale : cette action consomme une petite
part de l'abonnement. La préparer ou vérifier un hash ne vaut pas preuve de
connexion. Aucun compte, modèle ou fournisseur de secours n'est choisi.

Créer ensuite le JSON privé `0600` à
`<DOMICILE>/.config/blackoutburst-agent-host/host.json`, d'après
[MESSAGE_HOST.md](../../integrations/codex-personal/MESSAGE_HOST.md) ou
[HOST.md](../../integrations/codex-personal/HOST.md). Le propriétaire, l'adresse,
le modèle, les profils, sessions et conversations/tickets sont choisis
explicitement. Commencer avec un budget d'une opération, un modèle mesuré et
Runtime limité à une exécution par propriétaire. Garder `start_at: now` pour
le premier pilote de réponses afin de ne pas traiter l'historique.

Chaque cible doit utiliser :

- le Codex de cette release, sous `tools/codex` ;
- un fichier privé dans `<DOMICILE>/.config/blackoutburst-board/` ;
- un dossier de travail propre sous `<DOMICILE>/workspaces/`, en mode `0700` ;
- un modèle explicite, sans routage dans cette première installation.

Avec le préfixe ci-dessus, exécuter `configure`, puis `check` et `status`.
`configure` conserve la configuration immuable et laisse l'hôte **désactivé**.
`check` vérifie les pins et les chemins locaux ; il ne prétend pas avoir vérifié
l'abonnement ou les autorisations distantes. Un token du mauvais propriétaire
sera refusé par l'enregistrement et les contrôles Runtime avant exécution.

## 5. Activation bornée puis démarrage supervisé

L'autorisation du budget et le démarrage restent deux opérations distinctes :
le propriétaire utilise `enable --revision <REVISION_DE_STATUS>` ; l'opérateur
démarre ensuite son unité, par exemple
`systemctl start blackoutburst-agent-host-bb-agent-thescreamer.service`.
Une installation seule ne donne aucune de ces autorisations.

Le service utilise le compte Unix correct, `UMask=0077`, un système en lecture
seule hors de son domicile, aucun privilège supplémentaire et un groupe de
processus géré par systemd. Les limites initiales sont 2 Gio, 64 tâches et un
cœur CPU ; elles sont des bornes opérationnelles à mesurer, pas une garantie
de ressources suffisantes pour toute CLI. L'accès réseau sortant reste requis
pour le Board et le fournisseur. Le launcher vérifie les sources, outils,
interpréteur et chemins avant de charger l'adaptateur.

`Restart=no` laisse l'état arrêté après `needs_review`, erreur, budget épuisé ou
arrêt demandé. Le service n'est pas activé au démarrage de la machine par
l'installateur. Un éventuel démarrage au boot doit être décidé après la recette ;
il ne doit jamais exécuter `enable`, recréer un état ou renouveler son budget.

## 6. Arrêt, reprise et restauration

Le propriétaire peut lancer `stop` avec le même préfixe. Cette commande reste
disponible si le fichier initial de configuration ou la connexion est devenu
invalide. `systemctl stop` envoie SIGTERM ; le host désactive alors son budget
durablement et demande l'annulation coopérative. Après 330 secondes, systemd
peut tuer son groupe de processus ; cela ne fabrique pas un reçu de nettoyage
Runtime. Une incertitude doit rester `needs_review`.

Après coupure, inspecter les états privés et Runtime, rapprocher les enfants
exacts selon les guides existants, puis utiliser `host reconcile` si nécessaire.
Ne jamais effacer le journal, le verrou canonique ou remplacer le dossier par
un état vierge. Le verrou propriétaire ne coordonne qu'un compte Unix sur une
machine : conserver un seul hôte désigné pour ce propriétaire.

Pour une mise à jour ou restauration, arrêter les deux coordinateurs et conserver
ensemble la release, les configurations, tous les journaux et les verrous
canoniques, avec leurs propriétaires et permissions. Garder cette sauvegarde
privée ; ne pas copier de fichier d'authentification fournisseur vers un autre
compte/exécuteur. Une reconnexion officielle peut être nécessaire. Le service
Board et sa base se sauvegardent séparément. Aucun rollback local ne retire
des messages déjà publiés ni ne rend une inférence inconnue rejouable.

## Recette à réaliser avant de clore les tickets

Les tests du paquet simulent les commandes système et utilisent des répertoires
temporaires. Ils n'installent aucun compte/service et ne lancent aucun modèle.
La recette distante reste à faire avec chacun des deux propriétaires :
connexion officielle, réponse minimale, deux profils, message adressé avec les
ordinateurs personnels éteints, absence d'inférence à vide, arrêt, coupure,
reprise sans doublon, révocation et saturation Runtime. Une preuve de réponses
textuelles ne valide pas le développement Linux ni le projet entier.

```sh
python3 -m pytest tests/test_isolated_host_package.py tests/test_isolated_host_entry.py
```
