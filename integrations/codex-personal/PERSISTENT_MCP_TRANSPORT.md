# Transport MCP persistant — adaptateur 0.24

`BoardReader` conserve au plus une connexion HTTP/1.1 par instance. Une réponse
entièrement consommée peut réutiliser cette connexion pour le prochain RPC
explicite. Les méthodes, arguments, identifiants JSON-RPC, en-têtes de session et
contrôles de droits restent ceux du lecteur existant. Aucune photographie de
contexte, autorité, réservation ou permission n'est mise en cache par ce transport.

## Propriété et fermeture

Les instances ne partagent aucun pool global. Un client Runtime construit à
partir d'un worker possède son propre lecteur et sa propre connexion. Les commandes
et les deux hôtes enregistrent immédiatement les clients créés dans leur gestion
de ressources ; la sortie normale, un refus ou une exception ferme les clients
déjà construits. Les wrappers conservent aussi la référence du client brut dont
ils doivent fermer la connexion.

`BoardReader.close()` est idempotent et définitif. Son contexte `with` ferme le
lecteur à la sortie ; un nouveau RPC sur le lecteur fermé refuse avec
`connection_closed`. La fermeture d'un propriétaire peut interrompre son échange
en cours, sans relancer sa requête. Le lecteur reste synchrone ; il ne devient pas
un client multiprocessus ou un mécanisme de transactions distribuées.

## Coupures, TLS et bornes

- Une seule tentative HTTP est effectuée par appel explicite. Une coupure avant
  réponse, `RemoteDisconnected`, `BrokenPipe`, un timeout ou une réponse partielle
  invalide la connexion. Aucun POST n'est rejoué automatiquement, même si la
  connexion persistante paraissait seulement expirée.
- Un appel explicite ultérieur peut ouvrir une nouvelle connexion. Cela ne prouve
  pas que l'effet de l'appel précédent n'a pas eu lieu : les contrôleurs durables
  doivent utiliser leur recherche de reçu et leur clé exacte, sans créer une
  nouvelle intention pour contourner une ambiguïté.
- Les redirections sont refusées. Aucun proxy d'environnement n'est utilisé.
  HTTPS garde la vérification normale des certificats et du nom d'hôte ; HTTP
  reste limité aux adresses loopback déjà autorisées par le lecteur.
- Le transport vérifie le budget restant avant la connexion et après TCP/TLS,
  avant les premiers octets HTTP, puis avant les envois et chaque lecture de
  socket, y compris les en-têtes. Un connecteur revenu trop tard n'envoie aucun
  POST. Le résolveur DNS système peut néanmoins bloquer au-delà du timeout socket
  selon l'OS ; cette implémentation ne garantit pas une interruption stricte d'un
  résolveur système bloqué.
- La requête reste bornée à 64 Kio et la réponse à 2 Mio par le lecteur. Une
  réponse de longueur annoncée incomplète n'est pas acceptée, même si son préfixe
  contient du JSON valide. Une réponse rejetée ne conserve pas sa connexion.

Les diagnostics restent des codes fixes. Le transport ne publie ni jeton,
contenu brut, mesure de quota ou sortie de modèle.

## Mesures du 19 octobre 2026

Chaque paire compare le même nombre de RPC et les mêmes paramètres. Les durées
sont des observations ponctuelles, sans seuil de vitesse dans les tests.

| Échantillon | RPC mesurés par variante | Connexions neuves / persistantes | Durée neuve | Durée persistante |
|---|---:|---:|---:|---:|
| TLS loopback synthétique | 12 | 12 / 1 | 0,018812 s | 0,003153 s |
| Tableau distant, lecture seule | 12, après un warmup par variante | Non instrumentées côté serveur | 5,138580 s | 2,757605 s |

La mesure distante utilisait `get_ticket`, sans inscription, mutation ou modèle.
La médiane était de 0,440947 s avec une nouvelle connexion et de 0,255702 s avec
persistance ; la durée totale de cet échantillon diminue d'environ 46 %. La
mesure porte sur le transport final, y compris sa garde de fermeture pendant
connexion ; ses empreintes figurent dans le [reçu expurgé](docs/persistent-mcp-transport-20261019.json).
Une mesure précédente observait 39 % sur le même réseau : ces valeurs varient
avec les conditions du serveur et du réseau. Aucun résultat ne garantit le
budget du prochain pilote, le débit futur du serveur ou un gain identique sur
un autre réseau. La persistance ne réduit pas le nombre de contrôles distants.

## Qualification ciblée sans modèle

Depuis `integrations/codex-personal`, sur macOS ou Linux avec Python 3.10+, les
dépendances de test du dépôt, `openssl` et un accès loopback autorisé :

```sh
python3 -m pytest -q -s tests/test_persistent_transport.py tests/test_board_reader.py tests/test_owner_client_lifecycle.py tests/test_small_owner_client_lifecycle.py
```

La fixture crée un certificat temporaire et un jeton factice. Elle teste TLS de
bout en bout, refus d'un certificat non approuvé, connexion réutilisée,
authentification/session identiques par requête, coupures sans rejeu, réponse
partielle, redirection, taille maximale, lenteur de handshake/en-têtes/corps,
budget expiré au retour de connexion et fermeture définitive. Le benchmark
affiche ses chronos mais n'impose que le nombre de requêtes et de connexions.
Les tests de propriété couvrent les clients Runtime séparés, les constructions
partielles et les fermetures sur exception.

Exécuter exactement cette recette sur chaque OS ciblé. Si le sandbox du lanceur
interdit les sockets loopback, autoriser seulement cette fixture locale ; ce
refus d'environnement ne constitue pas une qualification positive. Aucun jeton
réel, serveur distant, compte Codex ou modèle n'est nécessaire. Cette recette
ne qualifie pas le backend natif de tests, l'isolation d'un modèle ou un pilote
familial via l'abonnement.
