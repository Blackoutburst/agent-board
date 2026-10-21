# Requête et paquet d’entrées Linux — codec inactif

Le [profil exécutable 0.30](LINUX_EXECUTION.md) réutilise ces primitives via un journal schema 3 distinct et des handles vivants. Le codec et le snapshot restent sans effet natif par eux-mêmes ; les états input-only/schema 2 et les preuves historiques ne sont jamais adoptés.

`blackoutburst_agents/linux_test_protocol.py` définit uniquement les données
privées d’une future tentative de test Linux. Il ne prépare pas de plan, ne
sélectionne pas de backend, ne lance pas de processus et n’écrit aucun journal,
reçu ou qualification. Son import et ses fonctions n’ouvrent aucun fichier et
n’effectuent aucun appel réseau. Les refus Linux, `TestRequest` Darwin et les
prototypes gelés restent inchangés.

## Liaison d’une requête

La dataclass immuable `LinuxTestRequest` est distincte du protocole Darwin. Son
document `blackoutburst-linux-test-request-v1` comporte des clés et types exacts :

- `operation_id` : clé SHA-256 de la tentative de test ; ce n’est pas une admission.
- `workflow_id` : UUID canonique de l’opération/fence du snapshot PR33.
- `manifest_sha256`, `scope_sha256` et `plan_sha256` : documents attendus exacts.
- `input_snapshot_sha256` : SHA-256 du document privé PR33 complet, incluant
  identités, ancêtres, signatures, limites et fence.
- `input_packet_sha256` : SHA-256 de tous les octets du paquet décrit ci-dessous.
- `planner_snapshot_sha256` : projection exacte courante de
  `DevelopmentPlanner._snapshot`.
- `root`, `root_identity`, `name`, `timeout_seconds`, `max_output_bytes` : valeurs
  exactes de la racine et du test nommé du manifeste.

`validate_linux_request` exige le manifeste, le snapshot PR33, la projection du
planner, le hash du plan attendu et les octets du paquet. Il les recoupe tous,
valide le contenu, puis rend une copie indépendante du document. Le backend du
test nommé doit avoir le nom `linux-python-v1-` suivi des 40 premiers caractères
du hash du plan ; la requête conserve et compare les 64 caractères. Dériver ce
nom ne crée aucune implémentation et n’atteste pas l’existence d’un plan.

Le contrôleur devra ultérieurement obtenir le hash attendu d’un vrai plan relu
et épinglé. Fournir un hash ou des métadonnées cohérentes ne prouve ni un hôte,
ni une permission Board, ni un verrou tenu, ni l’exécution d’un test. Ces données
ne sont pas destinées au modèle ou à la sortie publique.

## Conteneur binaire borné

`encode_input_packet` accepte seulement un dictionnaire exact des chemins du
snapshot vers des objets `bytes` immuables. Le format est déterministe :

1. préfixe fixe `BB_LINUX_INPUT_V1` suivi d’un saut de ligne ;
2. longueur d’en-tête sur huit octets non signés, ordre réseau ;
3. JSON UTF-8 canonique `blackoutburst-linux-test-inputs-v1`, avec les seules clés
   `schema`, `snapshot_sha256` et `files` ;
4. octets des fichiers concaténés, dans l’ordre lexical des chemins.

Chaque entrée contient exactement `path`, `offset`, `bytes` et `sha256`. Les
offsets sont contigus, à partir de zéro ; un fichier vide garde son entrée.
`decode_input_packet` refuse les clés dupliquées, le JSON non canonique ou non
fini, les types numériques approximatifs, les offsets chevauchants, les listes
différentes, les données tronquées ou ajoutées et toute divergence de hash.
Recalculer un hash dans l’en-tête ne permet pas de remplacer les octets attendus
par le snapshot. Les règles de chemin, d’alias et de contenu privé de PR33 et du
broker s’appliquent ; aucune entrée n’est ouverte, importée ou exécutée.

| Ressource | Plafond du codec |
| --- | --- |
| Requête canonique | 8 Kio |
| En-tête canonique | 128 Kio |
| Fichiers | 128 |
| Contenu d’un fichier | 1 Mio, ou plafond inférieur du manifeste |
| Contenu total | 4 Mio, ou plafond de lecture inférieur du manifeste |
| Fichiers + répertoires relatifs distincts + racine d’entrée | 256 entrées |
| Temps d’un test nommé | 1 à 60 secondes |
| Sortie d’un test nommé | 0 à 1 Mio |

La borne du paquet complet est le préfixe et ses huit octets, plus l’en-tête et
le contenu. Les limites de temps/sortie sont des valeurs attendues validées,
jamais des limites effectivement appliquées par ce codec.

## Frontières restant à raccorder

Le codec fonctionne dans le contexte du contrôleur qui a capturé le snapshot :
le validateur PR33 exige notamment le même UID effectif que les signatures des
fichiers et parents. Ce module ne constitue donc pas encore le décodeur d’un
processus après changement d’UID dans un user namespace. Le protocole enfant
devra séparer cette validation préalable des contrôles de copie effectivement
réalisés dans le namespace.

Le contrôleur doit encore tenir et revalider le vrai contexte de snapshot sous
verrou/fence, réserver durablement son coût de lecture, contrôler l’autorité aux
frontières prévues et vérifier le résultat avant toute admission. Une copie
de métadonnées survivant à la sortie du contexte n’atteste pas la possession du
verrou. Les contrôles PR33 restent coopératifs et ne promettent pas une capture
atomique face à un écrivain extérieur.

La persistance et ses transitions, le transfert scellé des octets, le vrai plan,
le superviseur, l’identité noyau, les gardes, les reçus, le nettoyage, la sélection
du backend et sa qualification restent des incréments distincts. Aucun journal
d’exécution artificiel n’est créé par cette étape. Les tests purs de codec ne
sont jamais des qualifications natives.

Voir [le contrat de capture](LINUX_INPUT_SNAPSHOT.md) et
[l’intégration restant à qualifier](LINUX_NATIVE_INTEGRATION.md).
