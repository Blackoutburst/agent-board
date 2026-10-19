# Refus avant lancement et fenêtres de lecture familiales

L’adaptateur 0.24.1 corrige deux problèmes révélés par un pilote familial arrêté : des autorisations répétées multipliaient les lectures distantes ; un refus après dispatch broker mais avant l’écriture native pouvait perdre la preuve que le test n’avait pas été lancé. Le pilote historique reste échoué et n’est pas repris par ces changements.

## Admission native

Le backend écrit une admission privée liée à l’opération, à la requête, au manifeste, au plan, au contrôleur et à un nonce avant ses callbacks faillibles d’autorisation et de qualification. Juste avant son appel de lancement, il persiste la frontière `entered`. Une exception effectivement observée dans l’appel propriétaire avant cette frontière peut produire un reçu terminal `not_started` : aucun lancement, nettoyage confirmé et motif borné. Ce reçu permet au broker de terminer l’action et au planner de terminer son Runtime comme **bloqué**, sans réussite de test.

Une coupure, une écriture ambiguë, une ancienne intention, l’absence de fichier ou un événement après `entered` restent inconnus. Aucun PID de journal n’est adopté ou signalé. Un ancien reçu de qualification n’est jamais utilisé comme reçu du test nommé. Les formats historiques restent lisibles selon leur contrat ; ils ne deviennent pas des preuves de non-lancement.

`lookup_not_started` observe seulement un reçu terminal exact et ne fait ni autorisation distante ni lancement. Après l’écriture du reçu broker, le nettoyage par identité reste disponible sans nouvelle autorisation d’exécution. Si le processus disparaît entre le reçu natif et celui du broker, le rapprochement broker peut consommer cette preuve avec ses autorisations courantes, sans relancer le test ; la voie de nettoyage par identité ne reconstruit pas ce reçu orphelin après révocation. Ce dernier cas demeure en revue tant qu’il n’a pas de preuve exploitable.

Tout nouveau plan natif doit être préparé et qualifié avec ses sources et contrôleurs exacts. Cette correction n’active ni backend Linux de produit, ni modèle, ni service.

## Lectures familiales fraîches

Une vérification familiale concrète collecte les policies parent et enfant, leurs tickets et claims au début et à la fin, leurs pages de discussion bornées et les deux vues de famille. Les contextes sont composés avec les mêmes sélecteurs, hashes et limites qu’avant. Les contrôles locaux de source, de clôture, d’arrêt et de délai encadrent la collecte entière. Un callback de délégation personnalisé conserve son parcours complet.

Une seule page par discussion demande **10 lectures**, contre 20 auparavant ; la pagination ajoute au plus quatre lectures avec les limites actuelles. Lors de la publication terminale du parent, les deux policies sont aussi relues en fin de collecte : 12 lectures usuelles, au plus 16 avec pagination, car une famille déjà prête pour revue court-circuite son calcul serveur de droits. Une seconde frontière refait une nouvelle collecte. Il n’existe ni cache de droits, ni durée de validité réutilisable, ni transaction atomique annoncée côté serveur.

Le callback backend évite uniquement de rappeler un callback qui est exactement le même objet que celui déjà contrôlé par le broker. Deux callbacks distincts restent tous deux exécutés. Le broker conserve ses contrôles propres ; aucun contrôle opaque n’est déduit de sa seule ressemblance.

Les limites de temps et d’actions restent identiques. Le comptage mesure les lectures logiques de la collecte, pas une durée garantie de bout en bout. La réussite d’un nouveau pilote via abonnement reste une validation séparée.
