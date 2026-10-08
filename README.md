# blinders (`blind`)

Start your coding agent **blind**: it sees a short index of your repos, not their code, and opens only the repos your prompt needs.

[English](README.md) · [Français (référence complète)](README.fr.md)

Coding agents (Claude Code, Gemini CLI, Codex, Mistral Vibe) pay for everything they load, on every request. Started at the root of a folder with dozens of repos, they load a pile of `GEMINI.md` / `CLAUDE.md` files, MCP servers and skills that have nothing to do with your question. `blind` decides what to load **before** the agent starts, locally, with no model call and so no extra tokens.

## How it works

1. `blind` indexes your repos once (names, README head, top-level folders, build and deploy files; never your source code).
2. For each prompt it picks the repos that match, plus the ones they are tied to (a Kubernetes repo that deploys the service, for example), and the MCP servers and skills that match.
3. It starts your agent in a throwaway folder that contains only a short index, with the chosen repos added through the agent's own options (`--add-dir`, `--include-directories`). No hook, no patch of the agent.

## Install

```bash
pipx install "blinders[ui] @ git+https://github.com/adrienchristiaen/blinders"
# or from a clone:  pip install -e ".[ui]"
```

Python 3.10 or newer. The `ui` extra adds the full-screen launcher (Textual); without it everything works in plain text. Not on PyPI yet.

## Use

```bash
blind                        # first run: asks where your repos live, indexes them; then pick what to open
blind gemini "where is the retry logic of billing-api?"   # start Gemini CLI with only the repos this needs
blind claude "add a column to the orders table"            # same for Claude Code
```

To see what it would do without starting anything:

```bash
blind select "add a column to the orders table"   # repos that would open, related repos, and why
blind doctor                                      # what blind can see on this machine
```

To check the gain on your own repos, run the same prompt both ways and compare:

```bash
blind gemini --plain "same prompt"    # the agent as-is, recorded for comparison
blind gemini "same prompt"
blind stats                           # anonymous numbers: no repo names, paths or prompts
```

One user's first measurements: context per request 23 to 28 % lower on a multi-repo setup, and much lower input and output tokens on a short question about one repo. These come from a single setup; measure yours.

## What is supported

| | Status |
|---|---|
| Claude Code, Gemini CLI | Flags checked against their `--help`. Gemini also gets a per-session home so unrelated `GEMINI.md` files are not loaded (checked on Gemini CLI 0.63). |
| Codex, Mistral Vibe | **Experimental**: flags not verified. Check with `--dry-run`, adjust in `[adapters.*]`. |
| Choosing a model per prompt | **Experimental**: a keyword heuristic. No light model is picked unless you set one. `blind model "prompt"` explains each decision; `[models] auto = false` turns it off. |
| Hiding MCP servers and skills | Works from the agents' documented options; not observed in every real session. |
| Multi-repo tasks (app + deploy + schema repos) | Related repos are found from build and deploy files and from `[groups]` in the config. Improving this is the current work. |
| Optional helpers | Graphify code graphs (`uv tool install graphifyy`) and [RTK](https://github.com/rtk-ai/rtk) output trimming, used only if installed. |
| Windows | Not tested. |

## Privacy

Everything runs on your machine. `blind` makes no network call of its own and sends nothing anywhere. The only network access is the `git fetch` it runs to update the repos it opens (`--no-sync` turns it off). Its usage numbers stay in `~/.cache/blinders` and hold no repo names, paths or prompts. Your real `~/.gemini` is never modified.

## Limits worth knowing

- Selection is lexical, not semantic: a prompt with no word in common with a repo will not open it. Name the repo, declare a `[groups]` entry, or add a short `.blinders/*.md` map file.
- "Blind" comes from how the agent is started, not from a block: if you add a repo or the agent reads `~`, it sees it.
- Slash commands to add a repo in a running session (`/add-dir`, `/directory add`) are mentioned in the index but not tested in a real session.

## Configuration

Zero config is the goal; the first run writes `~/.config/blinders/config.toml` with your `roots`. Everything else is optional (`max_repos`, `[groups]`, `[mcp]`, `[skills]`, `[models]`, `[gemini]`, `[sync]`). The full reference, with every option, is in [README.fr.md](README.fr.md) (French for now).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run the tests with:

```bash
pip install -e ".[ui]"
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

MIT licensed ([LICENSE](LICENSE)).
