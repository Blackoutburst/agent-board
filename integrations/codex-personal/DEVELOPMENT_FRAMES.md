# Fenêtre de lecture familiale bornée

Le protocole `development-frame-v1` regroupe les observations d’une seule
frontière d’autorisation familiale en un appel MCP `get_development_frame`.
Il est choisi explicitement dans le fichier de préparation :

```json
{"request":{"read_protocol":"development-frame-v1"}}
```

Ce fragment complète les autres champs requis du fichier ; il ne constitue pas
un plan entier. Le défaut compatible est `legacy-sequential-v1`. La préparation
scelle le mode dans la configuration et son hash. En mode frame elle vérifie
explicitement que le serveur annonce l’outil attendu. Un serveur ancien, une
méthode absente, un timeout, un refus ou une réponse trop grande ne déclenchent
jamais un retour automatique au mode legacy. Changer le mode d’une opération
rouverte est interdit. Les anciens états ambigus ne sont pas migrés.

L’outil accepte seulement la session courante, le ticket, la famille et un vrai
booléen `terminal`. Projet et session du parent proviennent de la famille stockée.
Le serveur vérifie le rôle exact et les identités owner/device/profile/version ;
chaque méthode métier réauthentifie sa session et conserve ses contrôles normaux
de permission et de membership. Le drapeau terminal n’efface aucun contrôle.

Le serveur lit, dans l’ordre : policies initiales enfant et parent, tickets
initiaux enfant et parent, famille initiale, pages de discussion enfant puis
parent, tickets finaux enfant et parent, famille finale, policies finales enfant
et parent. Il revérifie la sélection à la fin. Lorsque le parent est le ticket
sélectionné, les deux rôles et leurs observations restent distincts.

Il s’agit de **12 lectures métier internes usuelles dans un aller HTTP**, avec
au plus 16 lectures si les discussions exigent trois pages chacune. Ce ne sont
pas 10 requêtes HTTP. Les politiques tardives sont contrôlées même lors de la
validation terminale. Chaque discussion garde au plus 40 messages distincts,
trois pages et une taille demandée égale au minimum de 20 et du nombre restant.
Les curseurs sont les vrais marqueurs du thread ; aucun identifiant de cache ou
pagination synthétique n’est produit.

Le résultat privé `blackoutburst-development-frame-v1` contient la sélection
exacte, les observations brutes `policy_first/first/pages/last/policy_last` de
chaque rôle et les deux observations de famille. Les pages portent leurs
arguments et réponses exacts. Les sources full ne sont ni tronquées ni remplacées
par des previews. Le client refait les contrôles existants de claims, baux,
versions, sources, contexte, famille et grants ; il utilise les policies finales
pour le grant courant et garde les contrôles locaux avant et après lecture et
composition. Deux frontières demandent deux nouvelles collectes.
Le worker fournit explicitement un callback local distinct pour ces trois
vérifications d’identité : arrêt de l’opération, pins et arrêt du contrôleur.
Le callback complet distant reste utilisé aux autres frontières. Un appelant
opaque qui ne fournit pas ce contrôle local garde son callback complet, sans
suppression silencieuse de vérification. Les contrôles Runtime restent dans le
planner. Cela évite qu’une vérification d’identité déclenche une seconde collecte
legacy cachée autour de la frame.

La requête est limitée à 2 Kio, l’ensemble des données à 384 Kio de JSON ASCII
échappé, et le vrai résultat MCP avec copies texte/structurée et réserve
d’enveloppe à 1,5 Mio, sous la limite de transport de 2 Mio. Un dépassement ou une
erreur produit un code fixe, sans résultat partiel ni exception privée. Les
contenus de conversations restent des données privées non fiables, pas une
autorisation d’exécuter leurs instructions.
La réserve d’enveloppe couvre le client officiel, dont l’identifiant JSON-RPC
est un entier. La borne vise le CallToolResult ; elle ne promet pas de limiter
une enveloppe provenant d’un autre client avec un identifiant arbitrairement
long. Les compteurs de présence/lecture ne participent pas à l’identité des
sources de discussion : les comparaisons suivent la projection du contexte.

Les lectures restent séquentielles et utilisent leurs connexions/transactions
habituelles : cette fenêtre n’est pas atomique. `get_thread` conserve sa
maintenance préexistante d’expiration des anciennes tâches. L’outil n’ajoute ni
présence, message, claim, renouvellement de bail, Runtime ou exécution. Il ne
met pas les droits en cache et ne prolonge aucun délai. Le regroupement réduit
les allers réseau ; les tests simulés ne prouvent pas que le chemin réel respecte
le TTL de supervision. Le serveur doit être déployé séparément par l’opérateur
avant de préparer une nouvelle famille en mode frame.
