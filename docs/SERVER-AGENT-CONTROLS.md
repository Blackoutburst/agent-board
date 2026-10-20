# Contrôle des agents personnels sur le serveur

Le board conserve le registre, les commandes et les observations. Un contrôleur
distinct, dans le compte Unix personnel de chaque propriétaire, applique les
commandes à des profils déjà configurés. Le board ne transporte aucune commande
shell, chemin, instruction de modèle ou clé de fournisseur.

Cette première version utilise un processus par propriétaire et multiplexe au
plus seize profils configurés. Les travaux sont traités successivement ; le
nombre de profils enregistrés n’est pas un nombre de modèles simultanés. Les
limites Runtime existantes restent applicables.

Le propriétaire humain désigne explicitement un appareil comme contrôleur dans
Agents. Une inscription de profil ne lui accorde pas ce rôle. Le contrôleur
publie les identifiants de ses profils et les empreintes de sa configuration et
de sa version installée. Un nouveau catalogue commence avec un état inconnu ;
il faut une observation d’arrêt propre avant de demander un démarrage.

Le site distingue état demandé et état observé. Un acquittement signifie
seulement que la commande est enregistrée localement. Il ne confirme ni le
démarrage ni l’arrêt. Redémarrer exige d’abord un arrêt confirmé, puis une
nouvelle activation. Les clés, versions et générations empêchent qu’un nouvel
essai réseau crée un nouveau budget ou rejoue une ancienne activation.

Le canal de contrôle reste disponible après retrait des droits de travail pour
arrêter ou refuser une commande en attente. La révocation de l’appareil coupe
tout accès. Le contrôleur annule aussi le travail s’il perd le réseau ou si sa
collecte d’autorisation dépasse quinze secondes ; les échanges ultérieurs ne
prolongent pas l’âge de cette collecte. Un enfant ou un effet non rapproché
reste à vérifier et interdit un nouveau lancement. Les rapports ne modifient
jamais les preuves de nettoyage Runtime.

## Préparation initiale

Le déploiement du contrôleur et la connexion officielle du fournisseur sont des
étapes initiales distinctes. L’entrée protégée `owner_entry.py` vérifie le compte
Unix, l’interpréteur, tous les fichiers publics installés et la configuration
privée avant toute opération.

La configuration existante peut conserver le chemin du CLI d’une ancienne
version installée. L’entrée vérifie alors également cette ancienne version
protégée et exige les mêmes propriétaires, domaine, architecture, interpréteur,
version CLI et empreintes des deux outils. Le code du contrôleur provient de la
nouvelle version. Cette vérification ne modifie aucun fichier privé.

Après cette vérification, `managed-prepare --host-id …` prépare explicitement le
journal arrêté et propre, sans modèle ni connexion réseau. `managed-run` utilise
le même compte, le même journal, la même connexion locale épinglée et les mêmes
superviseurs de tickets/messages. Il ne remet pas en marche une activation
antérieure. L’ancien chemin `host run/tick` refuse un journal géré afin de ne
pas contourner le canal de contrôle. L’installation d’un service doit pointer
explicitement sur cette nouvelle entrée ; les services déjà préparés ne sont
pas migrés ou activés automatiquement.

Pour une installation existante, la [mise à jour explicite des services](../deploy/isolated_host/UPDATE.md)
vérifie la version précédente et conserve les outils, comptes et configurations.
Elle installe les nouvelles unités arrêtées et désactivées. L’autorisation du
contrôleur sur le board, la préparation de son journal et le démarrage du service
restent des étapes distinctes.

Le contrôleur n’appelle aucun modèle pour attendre des événements. Un démarrage
autorise le budget fini demandé, dans les limites de la configuration locale.
L’onglet « Sur le serveur » affiche aussi les places occupées du compte, lues
depuis le suivi Runtime existant. Un nettoyage non confirmé occupe toujours une
place ; le bouton de suivi permet de consulter ces exécutions. Une capacité
pleine grise seulement « Démarrer » pour une instance arrêtée et nettoyée, sans
modifier les droits ni les commandes d’arrêt ou de redémarrage. Une lecture
indisponible affiche « Capacité inconnue » ; le serveur garde l’admission finale.
Un budget supérieur à cette limite est refusé sur le site ; le gestionnaire
reste disponible pour une nouvelle demande corrigée. Une attente de réponse
humaine ou de confirmation réseau poursuit le même travail enregistré, sans
nouvelle activation ni recharge du budget. Après un redémarrage du processus,
un travail inachevé reste à vérifier et n’est pas repris automatiquement.
Un état inchangé envoie au plus un rapport par minute ; les commandes et les
changements d’état sont signalés immédiatement. Une simple lecture vide du
tableau laisse l’instance prête et ne la signale pas comme occupée.
Une tâche éventuelle conserve ses validations, son contexte et ses journaux
existants. La création d’un nouveau profil, sa configuration locale et le
parcours officiel de connexion au fournisseur sont traités séparément. Ce lot
ne qualifie pas l’exécution de développement Linux et n’active aucun accès API
payant de substitution.

Une cible de messages reste en démarrage jusqu’à la confirmation de son flux
pour l’activation courante. « Prêt » confirme ainsi que la frontière initiale
de `start_at=now` est établie ; un message envoyé avant cette confirmation peut
être antérieur au flux. Une lecture vide, même en cours, reste prête après cette
confirmation et ne devient occupée que lorsqu’un travail est réservé.
