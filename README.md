# blinders (Foveate)

Lance Claude Code, Gemini CLI, Codex ou Mistral Vibe **aveugles par défaut** : au démarrage, la CLI ne voit aucun code source, seulement un petit index markdown de tes repos. Seuls les repos utiles à ton prompt sont ensuite ouverts, avec les repos liés et les serveurs MCP dont il a besoin.

Pas de hook, pas de patch du harnais : la CLI est démarrée dans un dossier jetable qui ne contient que l'index, avec les repos choisis ajoutés par les options natives (`--add-dir`, `--include-directories`).

> **Nom.** « Foveate » est le nom proposé pour le projet (c'est le nom utilisé sur le site, dans [`site/`](site/index.html)). Le repo reste `blinders` et la commande reste `blind` tant que le renommage n'est pas décidé.

## Installation

```bash
pip install -e .          # Python >= 3.10 (tomli est installé automatiquement sur 3.10)
blind init ~/work ~/perso # dossiers qui contiennent tes repos (écrit ~/.config/blinders/config.toml)
```

## Utilisation

```bash
blind run gemini "corrige le DAG airflow qui charge BigQuery"   # ouvre seulement les repos pertinents
blind run gemini "comment sales-api-java est déployé sur kubernetes"  # ouvre aussi le repo de déploiement lié
blind run claude -r loopdex,coolpot                              # repos choisis à la main
blind run claude                                                 # entièrement aveugle (index seul)
blind run claude --primary -r jira-cli                           # démarre dans jira-cli (garde ses skills/MCP projet)
blind run gemini --dry-run "..."                                 # affiche la commande sans lancer
blind run gemini "..." -- --yolo                                 # options passées telles quelles à la CLI

blind select "lineage de sales-api-java"    # repos ouverts + repos liés (fermés), avec les scores
blind list                                  # repos indexés (rôle, chemin)
blind mcp --cli gemini "mets à jour le jira" # quels MCP seraient gardés
blind graph --all                           # graphes Graphify (optionnel)
blind audit ~/work ~/work/loopdex           # estime ce que chaque dossier charge au démarrage
blind clean                                 # supprime les workspaces aveugles
```

Pour ouvrir un repo en cours de session : `/add-dir <chemin>` (Claude Code) ou `/directory add <chemin>` (Gemini CLI). L'index généré l'explique à l'agent, qui te le demande au lieu de deviner des chemins. Pour Codex et Vibe, relance avec `blind run <cli> -r <repo>`.

## Fonctionnement

1. **Index** (`blind init`, rafraîchi tous les jours) : trouve les repos git sous tes `roots` et lit le début du README, les noms de dossiers de premier niveau, des marqueurs (`pom.xml`, `dbt_project.yml`, `Chart.yaml`...) et des fichiers de carte optionnels (`graphify-out/*.md`). Il lit aussi, en quantité bornée, les fichiers de build et de déploiement (voir plus bas). Le code applicatif n'est jamais ouvert.
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

### Graphify (optionnel)

`blind graph <repos...>` ou `blind graph --all` lance `graphify extract <repo> --code-only --global --as <nom>` (analyse locale par AST, sans clé API, avec fusion dans le graphe global de Graphify), puis `graphify update <repo>` avec `--update`. Quand un repo a un `graphify-out/GRAPH_REPORT.md`, l'index le signale à l'agent. Rien n'est obligatoire.

## Configuration

`~/.config/blinders/config.toml` :

```toml
roots = ["~/work", "~/perso"]
scan_depth = 3
max_repos = 3              # repos ouverts automatiquement au maximum
relative_threshold = 0.4   # garde les repos dont le score >= 40 % du meilleur
max_related_open = 2       # repos liés ouverts automatiquement
max_related_list = 5       # repos liés fermés affichés dans l'index
index_max_closed = 40      # repos fermés listés dans l'index (le reste: `blind list`)
map_globs = ["graphify-out/*.md", ".blinders/*.md"]

[groups]                   # « ces repos vont ensemble », sans rien deviner
billing = ["sales-api-java", "warehouse-etl"]

[mcp]
always = ["github"]        # toujours gardés

[mcp.keywords]             # mots qui rendent un serveur pertinent
bigquery = ["lineage", "table", "dataset"]

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
- Les skills ne sont pas filtrés, et le filtre MCP ne couvre que les serveurs du niveau utilisateur.
- `blind audit` estime en caractères / 4, pas avec un vrai tokenizer, et ne mesure pas la taille des schémas d'outils MCP (seulement leur nombre).

## Site

`site/index.html` : page unique et autonome, sans build. Pour la publier, par exemple avec GitHub Pages ou Vercel, sers le dossier `site/`.

## Tests

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```
