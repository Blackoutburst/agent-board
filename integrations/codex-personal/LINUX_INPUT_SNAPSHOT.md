# Contrat privé des entrées Linux — incrément inactif

Le [profil exécutable 0.30](LINUX_EXECUTION.md) réutilise ces primitives via un journal schema 3 distinct et des handles vivants. Le codec et le snapshot restent sans effet natif par eux-mêmes ; les états input-only/schema 2 et les preuves historiques ne sont jamais adoptés.

`blackoutburst_agents/linux_input_snapshot.py` prépare uniquement la copie privée
et bornée des fichiers texte explicitement présents dans le manifeste du broker.
Il ne crée ni plan d'exécution, backend, reçu de qualification ou capacité du
worker. Les refus d’activation Linux de l’adaptateur 0.27 restent inchangés. Les prototypes
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

## Raccord au verrou déjà détenu

Le contrôleur qui possède déjà `DevelopmentJournal.lock()` peut maintenant
demander `journal.held_lock_lease()` puis ouvrir
`capture_input_snapshot_locked(..., lock_lease=lease)`. Ce second contexte ne
reprend ni ne libère le verrou externe. Il impose les mêmes captures, limites,
pins, contrôles de fence et revalidations que le chemin public. L’acquisition
externe doit rester ouverte jusqu’à la sortie complète du contexte de capture.
Le chemin public `capture_input_snapshot` continue d’acquérir son propre verrou ;
l’appeler sous un verrou déjà détenu reste un refus de réentrance `broker_busy`.

Le bail privé est un objet vivant, lié à l’instance exacte du journal, au PID,
au thread, à cette acquisition et à ses descripteurs. `lock()` conserve sa valeur
de retour originale, le dictionnaire d’état. Aucun bail ou descripteur dupliqué
n’est créé pour un appel ordinaire qui ne demande pas ce raccord. Le bail peut
servir plusieurs captures/vérifications pendant la même acquisition ; il est
invalide avant toute libération du verrou et ne peut ni être copié, sérialisé,
hérité comme preuve par un autre PID, ni réutilisé lors d’une autre acquisition.
Une validation refusée l’invalide définitivement.

Un dup privé retient l’open-file description du verrou. Ce nouveau raccord
exige Linux et un procfs de confiance ; le journal ordinaire sans bail garde
son comportement sur Darwin. La preuve est une lecture non mutative de
`/proc/<pid>/fdinfo/<fd>` pour le dup et le fd original. Le lien fixe
`/proc/self` doit désigner le PID courant, afin de refuser un procfs exposant
un autre espace de PID. Chaque document est limité à 4 Kio et doit contenir
exactement les champs réguliers attendus et une unique ligne
`FLOCK ADVISORY WRITE`, liée au PID émetteur, au périphérique, à l’inode et à
la portée entière `0 EOF`. Les identités des fd sont vérifiées avant et après
lecture ; les observations doivent conserver les mêmes mount ID et flags.
Un document absent, malformé, inconnu ou ambigu, un autre mode de verrou ou
un changement de PID est un refus définitif. Aucun appel `flock` partagé ou
exclusif ne sert à valider ou nettoyer le bail : une vérification ne peut pas
reprendre le verrou perdu après le retrait d’un tiers.

Fermer puis rouvrir le fd original vers le même inode ne conserve pas son
verrou associé. Les pins d’acquisition, le répertoire privé et ses ancêtres,
les liens et permissions du verrou, ainsi que le manifeste sont aussi revérifiés.
Le nettoyage invalide d’abord le bail et ne déverrouille/ferme que les fd dont
la possession actuelle est positivement observée. Un numéro devenu étranger est
laissé intact et détaché du journal ; une possession impossible à confirmer
reste une incertitude, sans reçu de nettoyage. Un PID changé ne déverrouille
jamais le descripteur partagé du parent. Des fd en état inconnu peuvent rester
ouverts : ce module ne les récupère pas en inventant une preuve de possession.

L’observation prouve l’état à un instant sous un contrôleur coopératif ; elle
ne garantit pas la continuité face à du Python arbitraire dans le même processus
qui ferme, recrée ou libère et reprend des fd entre deux observations. Elle ne
vérifie pas à elle seule que le noyau, le montage procfs ou le système de fichiers
sont qualifiés. Les tests portables utilisent un modèle explicite d’OFD/fdinfo,
sans valeur de qualification Linux. Aucun backend n’est activé par ce raccord.
Les lectures de fichiers ne deviennent pas atomiques contre un écrivain extérieur.

Sources primaires recoupées : la [documentation actuelle de procfs](https://www.kernel.org/doc/html/latest/filesystems/proc.html)
décrit les champs et les verrous associés au fd ; [Linux 6.8, fs/locks.c](https://github.com/torvalds/linux/blob/v6.8/fs/locks.c#L2670)
filtre sur le `struct file` associé et son propriétaire sous verrou noyau ;
[fs/proc/fd.c actuel](https://github.com/torvalds/linux/blob/master/fs/proc/fd.c)
retient le fichier du fd pendant son observation. Ces sources expliquent le
mécanisme choisi ; elles ne remplacent pas la qualification du futur hôte.

Le codec [de requête Linux](LINUX_TEST_PROTOCOL.md) lie les octets attendus aux
documents privés ; il reste inerte. L'intégration future devra raccorder ce
contrat à un vrai backend qui lie
explicitement opération de test, plan Linux, manifeste, test nommé et digest de
ce snapshot ; garder le verrou pendant la copie dans le namespace et les
contrôles d'admission ; facturer durablement les lectures avant leur exécution ;
et qualifier les changements avant de sélectionner un backend Linux. Rien de
cela n'est activé dans cet incrément. Voir
[le contrat d'intégration restant](LINUX_NATIVE_INTEGRATION.md).

Le [codec de requête et de paquet d’entrées](LINUX_TEST_PROTOCOL.md) lie ces observations aux octets transmis. Il reste inerte et ne remplace ni la possession du verrou, ni un backend ou une qualification.


## Intégration de la fondation et portée de la preuve noyau

Cette branche réunit le raccord initial et son correctif de possession avant
publication : le premier correctif seul ne doit jamais être distribué. Le code
`dev_journal.py` et le snapshot intégré correspondent aux octets relus de
`72d3673166ab5af719de60b27f77d9a563f28785`. La validation et le nettoyage ne font
aucune acquisition/probe SH ou EX ; un verrou perdu reste définitivement refusé,
même après libération d’un verrou exclusif tiers. Le chemin ordinaire sans bail
conserve sa valeur de retour et ne consulte pas fdinfo sur Darwin.

Le reçu noyau du 19 octobre, résumé dans
[docs/linux-lock-lease-2026-10-19.json](docs/linux-lock-lease-2026-10-19.json),
a réellement passé huit cas synthétiques flock/fdinfo sous Ubuntu UID 1000,
Linux 5.15.0-198-generic/Python 3.10.12, avec nettoyage confirmé. Il porte les pins
exacts du prototype 72 et d’un lanceur tenu séparément ; ce résumé public ne sert
pas de reçu d’activation. Il ne qualifie ni UID 994, unité isolée, fork réel,
namespace, backend de tests, planner, autorisation Board ou tour modèle. Aucun
processus fournisseur n’était nécessaire à cette preuve noyau.

L’adaptateur passe à 0.27.0 pour cette API additionnelle et les nouveaux pins de
contrôleur. Board reste 2.13.0/schema 13. Les qualifications natives 0.26 antérieures
ne sont pas réémises ou adoptées : le journal et l’initialiseur ont changé. La
preuve noyau 72 ne devient pas non plus une qualification du package 0.27 complet
(son initialiseur appartenait à la distribution 0.25). Les refus existants dans
le backend/planner Darwin sont conservés ; aucun worker ne sélectionne un
exécuteur Linux dans cet incrément.

Le codec Linux existant conserve son format et ses limites. Deux tests de
raccord vérifient le paquet/requête sous le même bail réel déjà détenu et le
refus d’un ancien bail lors d’une autre acquisition. Valider un paquet après la
fin du contexte reste une opération de données inerte, jamais une preuve de
possession actuelle. Aucun callback d’autorisation, charge budgétaire implicite,
admission de test ou reçu d’exécution n’est ajouté par le codec. Le futur
contrôleur devra encore facturer les lectures avant capture et maintenir les
contrôles frais pendant son vrai test.
