# Proposition locale d’ajout d’une cible messages

`HostJournal.stage_messages_append(candidate, expected_configuration_sha256=…,
expected_catalog_sha256=…, idempotency_key=…)` enregistre uniquement une
proposition privée. Cette primitive est réservée à un futur orchestrateur
explicite. Aucun CLI, HTTP, MCP, service ou appel fournisseur ne l’utilise.

La configuration active n’est jamais modifiée : il n’existe aucun chemin
`apply`, de publication de catalogue ou de création d’une cible exécutable.
La barrière de provisioning Board et le binding personnel du nouveau slot ne
peuvent pas être prouvés localement. Le résultat indique donc toujours
`activation_allowed=false`, `board_fence_verified=false`, cible désactivée et
budget de travail nul. Une empreinte de proposition n’est ni un reçu Board,
ni une preuve de connexion, de qualification ou de non-lancement d’un processus.

La méthode exige le véritable contexte `owner_lock` de ce journal : PID,
thread et acquisition courante. Le contrôle et toutes les cibles doivent être
désactivés, les dispatchs `complete`, les commandes `applied`/`superseded` et
les effets `complete`/`superseded`. Les CAS portent sur les empreintes exactes
de la configuration et du catalogue existants ; les lignes du catalogue sont
recoupées avec ce dernier. Une deuxième connexion qui change l’état entre les
deux transactions provoque un refus.

Le candidat reprend les champs globaux et toutes les anciennes cibles dans
leur ordre, avec égalité des octets JSON canoniques, puis ajoute exactement
une cible à la fin. Son profil, ID, session, workspace et chemin de connexion
doivent être distincts ; `start_at` vaut `now`. Il est déjà normalisé selon le
contrat messages existant. La primitive n’ouvre pas les credentials, ne vérifie
pas le contenu d’un slot personnel et ne crée pas de workspace : leurs chemins
et empreintes restent des données attendues par la proposition. Les vérifications
de slot, de chemins/FD et de droits Board restent des préconditions de la future
application, jamais des preuves produites ici.

Les deux tables optionnelles sont versionnées explicitement en version 1.
Leur forme SQL exacte, métadonnée de version et au plus 32 lignes sont validées
avant toute utilisation. Les tailles sont contrôlées en SQLite avant adoption
des textes JSON ; chaque ligne est lue et décodée de manière bornée. Le schéma
HostJournal reste 2 : un ancien consommateur peut ignorer ces propositions
inertes, car elles ne changent aucune autorité d’exécution. Il ne doit jamais
les traiter comme des tests, commandes, configurations actives ou nettoyages.

Une première transaction écrit l’intention durable. La seconde relit les
conditions locales et son prédécesseur avant d’enregistrer le résultat de
staging. Elle exige également un objet privé émis pour cet appel, lié au
journal et à l’acquisition ; il n’est pas sérialisable et est invalidé même
quand le retour de la première transaction est perdu. Une inspection, une
copie JSON ou une nouvelle acquisition ne peut reconstruire cet objet.

Toute intention interrompue bloque une nouvelle proposition, même avec une
autre clé. Elle n’est ni effacée, ni reprise, ni reclassée automatiquement.
Un résultat terminal peut uniquement être rejoué comme métadonnée historique,
si son corps et les conditions locales exactes correspondent encore. Un nouveau
catalogue/configuration, une même clé avec corps changé, une modification des
compteurs ou du journal pendant la préparation sont refusés. Aucun compteur
n’est remboursé et aucun état ancien ne peut remplacer l’état vivant.

Les tables existantes, `instance_id`, activations, générations, budgets,
historiques, lecteurs et feeds ne sont jamais écrits. Les journaux enfants ne
sont pas ouverts. Ce contrat suppose le contrôleur Python coopératif et le
verrou de propriétaire existant ; il ne prétend pas protéger contre du code
Python arbitraire dans le même processus, une restauration administrative de
la base entière ou une perte matérielle non signalée par SQLite.

La suite exige une barrière Board réelle, un grant de provisioning séparé,
une identité de slot personnel déjà appairé, un protocole de publication CAS
et une reprise après ambiguïté revus ensemble. Ce lot n’en simule aucun reçu
et ne modifie ni les contrôles 45 ni l’exécuteur Linux 0.30.
