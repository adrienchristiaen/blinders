# blinders (Foveate)

Lance Claude Code, Gemini CLI, Codex ou Mistral Vibe **aveugles par défaut** : au démarrage, la CLI ne voit aucun code source, seulement un petit index markdown de tes repos. Seuls les repos utiles à ton prompt sont ensuite ouverts, avec les repos liés et les serveurs MCP dont il a besoin.

Pas de hook, pas de patch du harnais : la CLI est démarrée dans un dossier jetable qui ne contient que l'index, avec les repos choisis ajoutés par les options natives (`--add-dir`, `--include-directories`).

> **Nom.** « Foveate » est le nom proposé pour le projet (c'est le nom utilisé sur le site, dans [`site/`](site/index.html)). Le repo reste `blinders` et la commande reste `blind` tant que le renommage n'est pas décidé.

## Installation

```bash
pip install -e .          # Python >= 3.10 (tomli est installé automatiquement sur 3.10)
blind                     # le premier lancement pose les questions (voir ci-dessous)
```

## Utilisation

`blind` est l'étape avant la vraie CLI. Elle tourne en local, sans appel de modèle, donc sans token : Gemini ou Claude démarre déjà avec seulement ce qui a été retenu.

```bash
blind                       # assistant, puis prompt, plan, Entrée pour lancer
blind gemini                # idem, avec la CLI choisie
blind gemini "ton prompt"   # lance directement (--confirm pour voir le plan avant)
blind setup                 # relance l'assistant (dossiers, index, graphes)
```

Au premier lancement, `blind` demande les dossiers qui contiennent tes repos, les indexe, puis propose de construire un graphe Graphify pour chacun (local, sans LLM ; il faut `graphify` installé, sinon il l'indique et continue). Ensuite, pour chaque session :

1. tu tapes ton prompt (vide = session entièrement aveugle) ;
2. il affiche ce qu'il garde et ce qu'il écarte : repos ouverts, repos liés fermés, MCP gardés, skills gardés ;
3. Entrée lance la CLI ; `+repo` ou `-repo` ajuste les repos ouverts, `q` annule.

Pour ne rien changer à tes habitudes : `alias gemini='blind gemini'`. Le prompt tapé à la ligne de commande lance directement, sans confirmation.

Mesurer avec et sans `blind` (chaque lancement est enregistré, en chiffres seulement) :

```bash
blind gemini --plain "même prompt"   # la CLI telle quelle, dans le dossier courant, enregistrée pour comparaison
blind gemini "même prompt"           # avec blind
blind stats                          # tableau anonyme : repos, MCP, skills, tokens de démarrage estimés, tokens du 1er tour lus dans la session de la CLI
blind stats note <id> first_turn_tokens=21000 input_tokens=90000 output_tokens=4000   # ajouter à la main (ex. depuis /stats)
```

`blind stats` ne contient aucun nom de repo, chemin, prompt ni contenu de fichier : tu peux le coller tel quel. Les tokens réels sont lus dans les fichiers de session de la CLI (seulement les champs numériques `usage`). Format Claude Code vérifié ; format Gemini CLI supposé, non vérifié : si rien n'est trouvé, la colonne reste vide et `stats note` permet de saisir les chiffres.

Commandes détaillées :

```bash
blind run gemini "corrige le DAG airflow qui charge BigQuery"   # comme `blind gemini "..."`, ouvre seulement les repos pertinents
blind run gemini "comment sales-api-java est déployé sur kubernetes"  # ouvre aussi le repo de déploiement lié
blind run claude -r loopdex,coolpot                              # repos choisis à la main
blind run claude                                                 # entièrement aveugle (index seul)
blind run claude --primary -r jira-cli                           # démarre dans jira-cli (garde ses skills/MCP projet)
blind run gemini --dry-run "..."                                 # affiche la commande sans lancer
blind run gemini "..." -- --yolo                                 # options passées telles quelles à la CLI

blind sync                                  # fetch + checkout de la branche racine + fast-forward de tous les repos, puis graphes à jour
blind sync sales-api-java --safe            # un repo, sans changer de branche
blind status                                # branche, repo modifié ou non, graphe absent ou périmé (local, sans réseau)

blind select "lineage de sales-api-java"    # repos ouverts + repos liés (fermés), avec les scores
blind list                                  # repos indexés (rôle, chemin)
blind mcp --cli gemini "mets à jour le jira" # quels MCP seraient gardés
blind run claude --skills none "..."        # skills visibles : auto | all | none | a,b
blind graph --all                           # graphes Graphify (optionnel)
blind audit ~/work ~/work/loopdex           # estime ce que chaque dossier charge au démarrage
blind clean                                 # supprime les workspaces aveugles
```

Pour ouvrir un repo en cours de session : `/add-dir <chemin>` (Claude Code) ou `/directory add <chemin>` (Gemini CLI). L'index généré l'explique à l'agent, qui te le demande au lieu de deviner des chemins. Pour Codex et Vibe, relance avec `blind run <cli> -r <repo>`.

## Fonctionnement

1. **Index** (`blind setup` ou `blind init`, rafraîchi tous les jours) : trouve les repos git sous tes `roots` et lit le début du README, les noms de dossiers de premier niveau, des marqueurs (`pom.xml`, `dbt_project.yml`, `Chart.yaml`...) et des fichiers de carte optionnels (`graphify-out/*.md`). Il lit aussi, en quantité bornée, les fichiers de build et de déploiement (voir plus bas). Le code applicatif n'est jamais ouvert.
2. **Sélection** : un repo nommé dans le prompt passe en tête (le nom le plus long gagne : « sales-api-java » n'ouvre pas aussi `sales-api`). Sinon, score de type TF-IDF entre le prompt et chaque repo. Aucun score, aucun repo ouvert : la session reste aveugle. Quelques millisecondes pour des centaines de repos.
3. **Repos liés** : voir ci-dessous.
4. **MCP** : voir ci-dessous.
5. **Workspace** : un dossier (droits `0700`) dans `~/.cache/blinders/sessions/` avec `AGENTS.md`, `CLAUDE.md` et `GEMINI.md` identiques (repos ouverts, repos liés fermés avec la raison, repos fermés, MCP non chargés, comment en ouvrir un). Supprimé après 7 jours.
6. **Lancement** : `chdir` dans le workspace puis `exec` de la CLI avec les repos choisis.

### Repos liés

À l'indexation, les fichiers de build et de déploiement (Helm, Kubernetes, Kustomize, Docker, CI, Terraform, `pom.xml`, `package.json`, `pyproject.toml`...) sont lus pour trouver les repos qui en citent un autre par son nom ou son nom d'artefact. S'y ajoutent les noms proches (`sales-api` / `sales-api-java`) et des groupes déclarés dans `config.toml`.

À la sélection, les voisins des repos choisis sont :

- **ouverts** si leur rôle (`deploy`, `data`) correspond à l'intention du prompt (« déploie », `helm`, `kubernetes` ; « lineage », `dbt`, `bigquery`...), dans la limite de `max_related_open` ;
- **listés dans l'index** avec la raison sinon, pour quelques dizaines de tokens : « platform-k8s : it references sales-api-java in its build/deploy files ».

`--related all` ouvre les voisins les plus liés dans tous les cas, `--related none` ignore les relations.

### MCP selon le prompt

Les serveurs MCP du niveau utilisateur sont comparés au prompt (nom, commande, arguments, adresse, `description`, `[mcp.keywords]`). Ceux qui ne correspondent pas ne sont pas chargés, et l'index les liste pour que l'agent puisse les demander.

| CLI | Filtrage | Par défaut |
|---|---|---|
| `gemini` | `--allowed-mcp-server-names` répété, le reste de ta config est intact | oui |
| `claude` | `--strict-mcp-config --mcp-config <fichier>` avec seulement les serveurs gardés (fichier `0600` dans le workspace : il peut contenir des jetons) | non, seulement avec `--mcp` |
| `codex`, `vibe` | non géré | n/a |

Sur Claude Code, `--strict-mcp-config` ignore aussi les serveurs absents du fichier (plugins, connecteurs), d'où l'activation explicite. `--mcp all|none|a,b` force un choix.

### Skills selon le prompt

Les skills installés au niveau utilisateur (`~/.claude/skills`, `~/.gemini/skills`, `~/.agents/skills`) sont comparés au prompt : un mot du nom (ou de `[skills.keywords]`) suffit, sinon il faut au moins deux mots de la description en commun, et au plus `max_skills` (5) restent visibles. Les autres sont cachés au modèle pour cette session, jamais supprimés, et l'index les liste.

| CLI | Filtrage | Par défaut |
|---|---|---|
| `claude` | `--settings <fichier>` avec `skillOverrides` en `user-invocable-only` : cachés au modèle, mais encore utilisables à la main avec `/nom` | oui |
| `gemini` | `.gemini/settings.json` écrit dans le workspace aveugle (`skills.disabled`) | oui |
| `codex`, `vibe` | non géré | n/a |

Les skills fournis par des plugins ou des extensions ne sont pas touchés. `--skills all|none|a,b` force un choix.

### Deux niveaux : repo, puis fichiers

1. **Repo** : quels repos ouvrir (voir plus haut).
2. **Dans le repo** : pour chaque repo ouvert qui a un graphe Graphify, `blind` lit `graphify-out/graph.json` en local (quelques ms, aucun modèle) et note dans l'index quelques fichiers et symboles proches du prompt, plus les fichiers reliés dans le graphe :

```
- sales-api-java: /home/toi/work/sales-api-java
  Starting points from the code graph (hints, not a verdict; check before relying on them):
  - src/main/java/.../PricingRule.java: PricingRule (L12), applyDiscount (L40)
  - connected to those: src/main/java/.../CheckoutService.java
```

Ce sont des points de départ, pas des réponses : 3 à 4 fichiers au plus, jamais de contenu. Les noms des symboles comptent plus que les docstrings, les tests passent après le code sauf si le prompt parle de tests. Pas de pistes si rien ne correspond. `--no-hints` les désactive. La recherche est lexicale (noms de symboles et de fichiers), pas sémantique.

### Mise à jour avant lecture

Au lancement, pour les repos ouverts seulement :

1. `git fetch`, puis fast-forward de la branche racine (`origin/HEAD`, sinon `main`, `master`, `develop`) ;
2. si le graphe est absent ou construit depuis un autre commit (lu dans `GRAPH_REPORT.md`, sans ouvrir `graph.json`) : `graphify update` (ou `extract` + `cluster-only` la première fois), toujours local et sans LLM.

Garde-fous : jamais avec des modifications non commitées, un merge ou rebase en cours, ou un HEAD détaché ; jamais de merge commit (`--ff-only`) ; une branche qui a divergé est signalée et laissée telle quelle ; aucun mot de passe demandé. Le mode par défaut du lancement est `safe` : un repo déjà sur sa branche racine est mis à jour, un repo sur une branche de travail est laissé dessus (son graphe est construit depuis cette branche). `[sync] on_launch = "switch"` ou `--sync switch` fait le checkout de la branche racine, comme `blind sync`. `--no-sync` ou `on_launch = "off"` désactive tout. Les `--dry-run` ne touchent à rien. `graphify-out/` est ajouté à `.git/info/exclude` de chaque repo (local, jamais commité) pour ne pas salir `git status`.

### Graphify (optionnel)

`blind graph <repos...>` ou `blind graph --all` lance `graphify extract <repo> --code-only --global --as <nom>` (analyse locale par AST, sans clé API, avec fusion dans le graphe global de Graphify), puis `graphify cluster-only <repo> --no-label --no-viz` (c'est lui qui écrit `GRAPH_REPORT.md`, `--no-label` évite tout appel à un modèle), et `graphify update <repo>` quand le graphe existe déjà et est périmé (ou avec `--update`). Quand un repo a un `graphify-out/GRAPH_REPORT.md`, l'index le signale à l'agent. Rien n'est obligatoire.

## Configuration

`~/.config/blinders/config.toml` :

```toml
roots = ["~/work", "~/perso"]
scan_depth = 3
max_repos = 3              # repos ouverts automatiquement au maximum
relative_threshold = 0.4   # garde les repos dont le score >= 40 % du meilleur
default_cli = "gemini"     # CLI utilisée par un `blind` seul (sinon détection / question)
max_skills = 5             # skills gardés visibles quand le prompt correspond
max_related_open = 2       # repos liés ouverts automatiquement

[sync]
on_launch = "safe"         # off | safe | switch
timeout = 60               # secondes par commande git réseau
workers = 8                # repos synchronisés en parallèle (blind sync)
root_branches = ["main", "master", "develop"]   # si origin/HEAD est inconnu

[hints]
enabled = true
max_files = 4
max_related_list = 5       # repos liés fermés affichés dans l'index
index_max_closed = 40      # repos fermés listés dans l'index (le reste: `blind list`)
map_globs = ["graphify-out/*.md", ".blinders/*.md"]

[groups]                   # « ces repos vont ensemble », sans rien deviner
billing = ["sales-api-java", "warehouse-etl"]

[mcp]
always = ["github"]        # toujours gardés

[mcp.keywords]             # mots qui rendent un serveur pertinent
bigquery = ["lineage", "table", "dataset"]

[skills]
always = ["commit"]        # toujours visibles
[skills.keywords]
slides-builder = ["keynote"]

[adapters.vibe]            # adapter une CLI ou en ajouter une
binary = "vibe"
dir_style = "link"         # repeat | comma | link
```

| CLI | Ouverture des repos | Prompt initial |
|---|---|---|
| `claude` | `--add-dir <repo>` répété | argument positionnel (avant les options variadiques) |
| `gemini` | `--include-directories a,b` | `-i` |
| `codex` | `--add-dir <repo>` répété | argument positionnel |
| `vibe` | liens symboliques dans le workspace | n/a |

## Limites connues

- **Options vérifiées pour Claude Code et Gemini CLI** contre leur `--help` (`--add-dir`, `--mcp-config`, `--strict-mcp-config` ; `-i`, `--include-directories`, `--allowed-mcp-server-names`). Les adaptateurs `codex` et `vibe` ne le sont pas : vérifie avec `--dry-run` et corrige via `[adapters.*]`.
- Les commandes `/add-dir` et `/directory add` citées dans l'index n'ont pas été testées dans une session réelle.
- L'aide de Gemini ne montre pas de valeur « aucun MCP » : quand aucun serveur ne correspond, `blind` passe un nom de serveur inexistant (`blinders-no-mcp`) à la liste d'autorisation. À vérifier sur ta version.
- L'aveuglement vient de la construction (la CLI démarre dans un dossier sans code), pas d'un blocage : si tu ajoutes un repo ou si l'agent lit `~`, il le voit.
- Les fichiers de contexte d'un repo ajouté par `--add-dir` ne sont pas forcément chargés par la CLI. Pour garder ses skills, MCP projet et `CLAUDE.md`, utilise `--primary` (démarre dans le premier repo, sans index).
- La sélection est lexicale, pas sémantique : un prompt sans mot commun avec un repo ne l'ouvrira pas. Nomme le repo, déclare un groupe ou ajoute un fichier de carte (`.blinders/*.md`).
- Les relations viennent de noms cités dans les fichiers de build et de déploiement ; un nom ambigu (même artefact dans deux repos) est ignoré.
- **Graphify** : `GRAPH_REPORT.md` n'est écrit que par `cluster-only` (l'ancienne version de `blind graph` ne le lançait pas : relance `blind sync` ou `blind graph --all --update`). Format de `graph.json` observé sur Graphify 0.9.80 ; une autre version peut changer les champs lus (`label`, `source_file`, `source_location`, `file_type`, `links`).
- Les pistes dépendent de la qualité du graphe et des mots du prompt ; un mauvais indice est possible, d'où le libellé « hints ». Non mesuré : c'est ce que `blind stats` doit établir sur tes repos.
- Les filtres MCP et skills ne couvrent que le niveau utilisateur, pas les plugins ni les extensions.
- **Filtre de skills non testé en session réelle.** Pour Claude Code, `skillOverrides` vient de la documentation et du suivi d'issues (le réglage est peu documenté, et des issues signalent que `off` n'empêche pas l'appel explicite d'un skill). Pour Gemini, la clé `skills.disabled` et son effet dans les réglages du workspace n'ont pas été vérifiés, et Gemini n'applique les réglages d'un workspace que dans un dossier de confiance. Vérifie avec `blind gemini --dry-run` puis dans la session (`/skills`).
- `blind audit` estime en caractères / 4, pas avec un vrai tokenizer, et ne mesure pas la taille des schémas d'outils MCP (seulement leur nombre).

## Site

`site/index.html` : page unique et autonome, sans build. Pour la publier, par exemple avec GitHub Pages ou Vercel, sers le dossier `site/`.

## Tests

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```
