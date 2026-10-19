# Contrat privé des entrées Linux — incrément inactif

`blackoutburst_agents/linux_input_snapshot.py` prépare uniquement la copie privée
et bornée des fichiers texte explicitement présents dans le manifeste du broker.
Il ne crée ni plan d'exécution, backend, reçu de qualification ou capacité du
worker. Les refus Linux de l'adaptateur 0.24 restent inchangés. Les prototypes
Linux et leurs reçus ne sont pas utilisés par ce module.

Le contrôleur autorisé fournit les objets exacts `DevelopmentBroker` et
`DevelopmentFence`, ainsi que le binding et l'identifiant d'opération déjà
réservés. `capture_input_snapshot` acquiert lui-même le vrai verrou du journal
broker ; une chaîne, un booléen ou un objet doté d'une méthode `lock` ne le
remplace pas. Le helper vérifie l'enregistrement durable du fence contre la
scope, la session, la racine et le journal du broker. Une réservation familiale
doit déjà être transférée au stade planner. Le contrôle complet de la famille,
les grants, la claim, les stops et les échéances restent à la charge du
contrôleur ; ce helper n'appelle aucun callback susceptible d'effectuer un RPC.

Le snapshot attendu est exactement la projection actuelle de
`DevelopmentPlanner._snapshot` : `{sha256, files: {chemin: SHA256}}`. Sa liste de
fichiers doit être celle du manifeste et son hash est recalculé. Les octets lus
produisent à leur tour cette même projection ; un hash valide portant sur un
autre contenu est refusé. Les patches existants sont donc acceptables uniquement
avec la projection actuelle du planner, jamais par réutilisation du contenu
initial du manifeste.

Les chemins canoniques, chaque ancêtre absolu, la racine, les parents relatifs,
les identités et signatures complètes des fichiers sont conservés dans les
métadonnées privées. Chaque lecture ouvre les composants sans suivre de lien
symbolique, exige des répertoires exclusifs sous la racine et des fichiers
réguliers appartenant au propriétaire, sans hardlink ni écriture de groupe ou
d'autrui. Les aliases d'identité et de noms Unicode/casse sont refusés. La
racine ne peut pas chevaucher le journal ou l'ancre privée du fence. Les règles
de chemin et de contenu privé du broker continuent de s'appliquer.

La copie est limitée à 128 fichiers, 32 composants relatifs, 1 Mio par fichier
(et au plafond plus bas du manifeste), 4 Mio au total (et au plafond de lecture
plus bas du manifeste) et 512 Kio de métadonnées sérialisées. La capture effectue
deux passes et exige les mêmes signatures, identités, ancêtres et contenu. Un
remplacement d'inode à contenu égal invalide donc le snapshot. Les mêmes
contrôles sont disponibles via `snapshot.revalidate()` et sont imposés à la
sortie normale du contexte. Avant un futur ACK de source puis après son résultat,
le backend devra explicitement appeler cette revalidation en tenant toujours le
contexte. Les méthodes ne fonctionnent plus à sa sortie ni depuis un autre
thread ; les octets sont uniquement retournés au contrôleur par `read(chemin)`.
Ils ne sont pas inscrits dans un journal ou un reçu. `document()` ne contient que
les métadonnées et rend une copie indépendante ; son résultat reste privé.

Ce verrou et ces contrôles coordonnent les écrivains qui utilisent ce broker et
son fence. Ils ne rendent pas plusieurs lectures atomiques face à un écrivain
extérieur, à un remplacement remis en place entre deux observations ou à un
administrateur. Le helper ne transmet ni permission Board ni preuve de test.
Un document dont le schéma est valide n'établit pas qu'un processus a réellement
utilisé ces entrées.

L'intégration future devra définir un `TestRequest` versionné qui lie
explicitement opération de test, plan Linux, manifeste, test nommé et digest de
ce snapshot ; garder le verrou pendant la copie dans le namespace et les
contrôles d'admission ; facturer durablement les lectures avant leur exécution ;
et qualifier les changements avant de sélectionner un backend Linux. Rien de
cela n'est activé dans cet incrément. Voir
[le contrat d'intégration restant](LINUX_NATIVE_INTEGRATION.md).

Le [codec de requête et de paquet d’entrées](LINUX_TEST_PROTOCOL.md) lie ces observations aux octets transmis. Il reste inerte et ne remplace ni la possession du verrou, ni un backend ou une qualification.
