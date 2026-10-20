# Préparer un agent et sa connexion personnelle

Ce premier incrément du ticket 46 ajoute un parcours visible dans **Agents → Sur
le serveur → Préparer un agent serveur**. Il prépare les préférences et réutilise
la création de profil existante. Il ne termine pas le provisioning ni la
connexion depuis le navigateur. Aucun fournisseur, compte ou modèle réel n’est
appelé par ce code.

## Comportement livré

Le propriétaire choisit Codex avec ChatGPT ou Claude Code et, facultativement,
un nom de modèle demandé. L’éditeur normal conserve personnalisation, permissions,
projets et idempotence. Le résumé sépare ces demandes de la connexion et de
l’accès au modèle, qui restent **sans attestation**. L’adaptateur textuel Codex
existe dans le dépôt ; celui de Claude Code reste à implémenter. Les fournisseurs
supplémentaires ne sont pas annoncés comme compatibles.

`GET /api/provider-onboarding`, avec `profile_id` facultatif, est réservé à la
session humaine du propriétaire. Il n’a aucun équivalent MCP et n’ajoute aucune
permission à un appareil. Il expose un catalogue fermé, l’hôte actif de ce
propriétaire, au plus 16 cibles réellement publiées, et les seules préférences du
profil demandé. Il ne lit ni credentials, instructions privées, conversations,
politiques de routage locales ni historique fournisseur. Les réponses gardent
la limite JSON existante de 64 Kio et le `no-store` des routes privées. Les erreurs
de paramètres ont des codes fixes, sans renvoyer l’entrée invalide.

La publication d’une cible atteste son identité de lancement et son workflow
`messages` ou `ticket_text`. Le protocole 45 ne publie pas de fournisseur connecté,
de catalogue de modèles ni d’éligibilité d’abonnement. Ni `ready`, ni
`can_start`, ni une préférence correspondante n’en constituent une preuve.
L’interface n’infère donc aucun de ces états.

L’identifiant de l’hôte est affiché dans **Configurer le gestionnaire initial**,
avec sélection/copie. Après avoir lié son propre appareil dans l’interface,
l’humain peut remettre cet identifiant à l’administrateur qui prépare le
gestionnaire sous le bon compte Unix. Cet identifiant n’est pas un token.
Le code actuel ne prépare toujours pas automatiquement une cible nouvelle.
Les commandes versionnées/idempotentes 45 restent inchangées et n’autorisent
que des cibles déjà publiées par le gestionnaire.

## Parcours officiels et limites

Les documents suivants ont été relus le 20 octobre 2026 :

- [Authentification Codex](https://learn.chatgpt.com/docs/auth) : la CLI propose
  une connexion personnelle ChatGPT, notamment un flux device-code sur machine
  sans navigateur. L’autorisation de ce flux dépend des réglages du compte ou
  de son workspace. Une clé API désigne une facturation différente.
- [Codex app-server](https://learn.chatgpt.com/docs/app-server) : son
  authentification n’est pas un mécanisme à réemployer pour un service hébergé.
  Ce projet ne crée donc pas un `account/login/start` app-server derrière un
  bouton du Board. Sign in with ChatGPT est une intégration distincte.
- [SIWC sur VM personnelle](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms) :
  la procédure prévoit une identité d’hôte stable, une inscription personnelle
  et des credentials protégés. Elle ne constitue pas une autorisation générale
  de partage d’abonnement. Notre prochain flux n’importera ni ne copiera une
  authentification existante pour un autre compte ou processus.
- [Authentification Claude Code](https://code.claude.com/docs/en/authentication) :
  la connexion abonnement via la CLI et les credentials Console/API sont
  distincts. Le résultat de `setup-token` est un secret ; il ne doit pas être
  envoyé au Board ou à un modèle. Aucun de ces parcours n’est implémenté ici.

Ces descriptions n’établissent pas l’éligibilité de ce déploiement, des modèles
actuellement accessibles ou un SSO commun à tous les fournisseurs. Un mode ne
peut être proposé comme utilisable qu’après vérification de ses conditions et
de son adaptateur concret. Aucun fallback vers une API payante n’est permis.

## Contrat proposé pour le prochain incrément durable

Le contrat suivant précise la suite à développer et à revoir. Il n’est pas
un protocole déjà activé, ni un nouveau schéma ajouté par ce premier incrément.

### Autorité et plan fixe

Un administrateur prépare une fois un compte Unix distinct sans privilèges par
personne, son domicile privé et les sources/outils publics vérifiés. Un humain
autorise explicitement le gestionnaire correspondant à son compte Board.
L’autorité de provisioning/login doit être un grant séparé du contrôle 45 :
les appareils 45 existants n’obtiennent pas implicitement le droit de créer des
connexions ou de modifier leurs fichiers privés.

Le navigateur choisit uniquement un template annoncé par ce gestionnaire, avec
`host_id`, `expected_catalog_version`, `profile_id`, `profile_version`,
`template_id`, `template_revision`, `provider_id`, `requested_model` et une
`idempotency_key`. Les seuls premiers templates envisagés sont les workflows
textuels personnels Codex messages et ticket_text. Claude n’entre dans ce
catalogue qu’après son implémentation/revue propre. Un template détermine côté
hôte les outils pinés, dossiers privés, bornes et arguments ; aucun shell,
exécutable, chemin, UID, endpoint fournisseur ou variable d’environnement libre
n’est accepté du navigateur.

Le serveur authentifie l’humain propriétaire et scelle une requête durable
liée à la version exacte de l’hôte, du profil, du template et de la source.
Le gestionnaire lit seulement ses requêtes autorisées. L’ACK signifie
« intention enregistrée localement », jamais « connexion réussie ». Les états
et rapports sont CAS/versionnés, avec clés stables sur réponse perdue. Une
révision différente ne permet pas de réutiliser une ancienne intention.

### État privé et configuration

Le gestionnaire conserve un journal de provisioning distinct, sous le verrou
exclusif du propriétaire. Les budgets locaux et les 16 cibles maximales sont
réservés avant tout effet. La création utilise un répertoire exclusivement neuf
et des fichiers privés. Une interruption ou un résultat ambigu reste
`needs_review` ; elle ne permet pas d’adopter des partiels ni de réinitialiser
un budget. La simple inspection ne reconstruit aucune autorité.

HostConfig et ses journaux 45 restent immuables. Ajouter un profil exige un plan
explicite de nouvelle révision de configuration, comparé à l’ancien digest,
avec frontières sûres, absence de travail non réconcilié et validation de tous
les bindings. On ne modifie pas silencieusement l’ancien journal pour faire
apparaître une cible. Aucune ancienne opération n’est reclassée en succès.
Le mécanisme de transition de configuration est une dépendance à implémenter.

### Connexion exclusivement humaine

La demande de login est un nouvel acte humain séparé de la création de profil.
Seul un adaptateur officiel revu peut lancer une commande de connexion fixe,
sous l’UID du propriétaire, hors des workers/modèles et avec sa propre identité
de processus vérifiable. La CLI officielle gère ses credentials dans ce
domicile privé ; ils ne sont ni copiés, ni lus par le Board, ni prêtés.

Les URL/codes de challenge transitent uniquement dans un canal humain privé,
avec session humaine propriétaire, contrôle exact de l’UID/hôte/intention,
`no-store` et `no-referrer`. Ce canal doit être distinct des méthodes MCP et
des conversations. Ses réponses ne sont pas persistées dans les audits, tickets,
messages, résultats de modèles ou journaux partagés. Le Board ne garde que
référence opaque, phase et expiration. Aucun endpoint distant libre ni sortie
brute CLI ne peut être relayé ; le parseur du challenge et les domaines attendus
doivent être pinés à la version de l’adaptateur. Le transport de ce canal et
sa confidentialité doivent être revus avant activation.

Expiration, annulation, perte du gestionnaire et redémarrage invalident le
challenge. Ils ne relancent pas automatiquement un login. Une reprise exige
un nouvel acte humain ; aucun ancien code ou résultat ne devient une preuve.
Annuler ou déconnecter ne peut agir que sur ses propres processus/credentials.
L’arrêt/nettoyage reste possible après retrait des droits de travail. Une
incertitude ne libère jamais Runtime et empêche une relance implicite.

### Observations et activation séparées

Les états à exposer séparément sont : profil demandé, provisioning demandé,
cible préparée, login en attente, connexion vérifiée/expirée, modèles listés,
accès modèle testé/non testé, puis instance demandée/observée. Une liste n’est
pas un test d’accès ; un test de connectivité n’est pas une qualification
Linux ou une promesse de quota futur.

Toute observation fournisseur est bornée, datée, liée au propriétaire/hôte/
connexion/outil/modèle exacts, et contient seulement les champs autorisés.
Un refus/quota/expiration invalide l’état pertinent. Aucun compte ne peut
hériter d’une observation d’un autre propriétaire. Les vérifications qui
consomment un tour exigent un acte humain et un budget explicites.

Un rapport de connexion ne démarre rien. Le premier démarrage conserve toutes
les règles 45 (droits actuels, source, génération, budget, CAS et nettoyage),
ainsi que les réservations Runtime. Le développement Linux reste soumis au
lot 0.30 et à ses qualifications exactes, sans adoption d’une preuve textuelle.

## Dépendances et recette avant achèvement du ticket 46

Il reste à livrer le grant et la queue durables, la transition HostConfig, le
canal de challenges humains, l’adaptateur de login Codex personnel, l’adaptateur
Claude, les observations fournisseur/modèles, l’annulation/déconnexion et leur
interface. Une intégration SIWC demanderait son client enregistré et son
parcours officiel distinct ; elle n’est pas déduite de l’existence de la CLI.

L’administrateur devra préparer les comptes/permissions Unix et le service
gestionnaire initial avec ses sources/outils exacts. Chaque personne devra
autoriser sa propre connexion dans le fournisseur et, si nécessaire, le flux
device-code de son workspace. Aucune de ces actions n’a été faite par ce lot.

La recette finale devra couvrir deux propriétaires, refus croisés, duplications,
réponses perdues, crash entre intention et effet, expiration/annulation de
challenge, édition de profil, révocation de droits, quota et reprise froide.
Elle devra vérifier la confidentialité des challenges et credentials, puis un
premier démarrage réel autorisé. Les tests synthétiques de ce premier incrément
ne permettent pas d’annoncer ce parcours terminé.
