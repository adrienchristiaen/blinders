# blinders

Lance Claude Code, Gemini CLI, Codex ou Mistral Vibe **aveugles par défaut** : au démarrage, la CLI ne voit aucun code source, seulement un petit index markdown de tes repos. Seuls les repos utiles à ton prompt sont ensuite ouverts.

Pas de hook, pas de patch du harnais : la CLI est démarrée dans un dossier jetable qui ne contient que l'index, avec les repos choisis ajoutés par les options natives (`--add-dir`, `--include-directories`).

## Installation

```bash
pip install -e .          # Python >= 3.10 (tomli est installé automatiquement sur 3.10)
blind init ~/work ~/perso # dossiers qui contiennent tes repos (écrit ~/.config/blinders/config.toml)
```

## Utilisation

```bash
blind run gemini "corrige le DAG airflow qui charge BigQuery"   # ouvre seulement les repos pertinents
blind run claude -r loopdex,coolpot                              # repos choisis à la main
blind run claude                                                 # entièrement aveugle (index seul)
blind run claude --primary -r jira-cli                           # démarre dans jira-cli (garde ses skills/MCP projet)
blind run gemini --dry-run "..."                                 # affiche la commande sans lancer
blind run gemini "..." -- --yolo                                 # options passées telles quelles à la CLI

blind select "fix the thermal simulation"   # quels repos seraient ouverts, avec les scores
blind list                                  # repos indexés
blind audit ~/work ~/work/loopdex           # estime ce que chaque dossier charge au démarrage
blind clean                                 # supprime les workspaces aveugles
```

Pour ouvrir un repo en cours de session : `/add-dir <chemin>` (Claude Code) ou `/directory add <chemin>` (Gemini CLI). L'index généré l'explique à l'agent, qui te le demande au lieu de deviner des chemins. Pour Codex et Vibe, relance avec `blind run <cli> -r <repo>`.

## Fonctionnement

1. **Index** (`blind init`, rafraîchi tous les jours) : trouve les repos git sous tes `roots` et lit au plus quelques Ko par repo (README, noms de dossiers de premier niveau, marqueurs `pom.xml`/`dbt_project.yml`/..., et fichiers de carte optionnels comme `graphify-out/*.md`). Le code source n'est jamais ouvert.
2. **Sélection** : score de type TF-IDF entre ton prompt et chaque repo. Un repo nommé dans le prompt passe en tête. Aucun score, aucun repo ouvert : la session reste aveugle. Quelques millisecondes pour des centaines de repos.
3. **Workspace** : un dossier dans `~/.cache/blinders/sessions/` avec `AGENTS.md`, `CLAUDE.md` et `GEMINI.md` identiques (repos ouverts, repos fermés, comment en ouvrir un). Les workspaces de plus de 7 jours sont supprimés automatiquement.
4. **Lancement** : `chdir` dans le workspace puis `exec` de la CLI avec les repos choisis.

## Configuration

`~/.config/blinders/config.toml` :

```toml
roots = ["~/work", "~/perso"]
scan_depth = 3
max_repos = 3              # repos ouverts automatiquement au maximum
relative_threshold = 0.4   # garde les repos dont le score >= 40 % du meilleur
index_max_closed = 40      # repos fermés listés dans l'index (le reste: `blind list`)
map_globs = ["graphify-out/*.md", ".blinders/*.md"]

[adapters.vibe]            # adapter une CLI ou en ajouter une
binary = "vibe"
dir_style = "link"         # repeat | comma | link
```

| CLI | Ouverture des repos | Prompt initial |
|---|---|---|
| `claude` | `--add-dir <repo>` répété | argument positionnel |
| `gemini` | `--include-directories a,b` | `-i` |
| `codex` | `--add-dir <repo>` répété | argument positionnel |
| `vibe` | liens symboliques dans le workspace | n/a |

## Limites connues

- **Options de CLI non vérifiées contre les vraies CLI.** Les tests utilisent un faux binaire pour valider le `chdir`, l'`exec` et la forme des commandes. Les noms d'options viennent de la documentation publique et peuvent changer : vérifie avec `--dry-run`, corrige via `[adapters.*]`. L'adaptateur `vibe` est le moins sûr.
- L'aveuglement vient de la construction (la CLI démarre dans un dossier sans code), pas d'un blocage : si tu ajoutes un repo ou si l'agent lit `~`, il le voit.
- Les fichiers de contexte d'un repo ajouté par `--add-dir` ne sont pas forcément chargés par la CLI. Pour les garder (skills, MCP, `CLAUDE.md` du projet), utilise `--primary`, qui démarre dans le premier repo et renonce à l'index.
- La sélection est lexicale, pas sémantique : un prompt sans mot commun avec le README ou les dossiers d'un repo ne l'ouvrira pas. Nomme le repo ou ajoute un fichier de carte (`.blinders/*.md`).
- `blind audit` estime en caractères / 4, pas avec un vrai tokenizer, et ne mesure pas la taille des schémas d'outils MCP (seulement leur nombre).

## Tests

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```
