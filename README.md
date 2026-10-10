# blinders (`blind`)

Start your coding agent **blind**: it sees a short index of your repos, not their code, and opens only the repos your prompt needs.

[English](README.md) · [Français (référence complète)](README.fr.md)

Coding agents (Claude Code, Gemini CLI, Codex, Mistral Vibe) pay for everything they load, on every request. Started at the root of a folder with dozens of repos, they load a pile of `GEMINI.md` / `CLAUDE.md` files, MCP servers and skills that have nothing to do with your question. `blind` decides what to load **before** the agent starts, locally, with no model call and so no extra tokens.

## How it works

1. `blind` indexes your repos once, from what is in them: README head, folder and file names, names declared in small YAML files, which repo mentions which. It does not index your source code.
2. For each prompt it picks the repos named in it, then the repos the rest of the prompt describes (name the app and talk about a schema, and the schema repo joins), plus the repos that literally contain an identifier from your prompt (`invoice_vat_rate`, `OrderTotal`, anything quoted; searched with `rg`, only repo names come back), then checks in the files of those repos that your prompt's words are really there (repos that only match by name are listed, not opened), plus the ones they are tied to (a Kubernetes repo that deploys the service, for example), and the MCP servers and skills that match.
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

In the full-screen launcher, a pill next to the prompt shows each optional helper: green = found and used, amber = found but not used in this launch, red = not found (hover for the reason).

To see what it would do without starting anything:

```bash
blind select "add a column to the orders table"   # repos that would open, related repos, and why
blind doctor                                      # what blind can see on this machine
```

To check the selection against prompts you know the answer to, write cases in a TOML file (see `examples/cases.toml`) and run `blind eval cases.toml`; `blind bench "prompt"` times each stage on your repos.

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
| Choosing a model | You choose: `--model`, the launcher selector, or `[models.<cli>] default`. blind does not guess it from the words of a prompt. `blind models` lists what your CLI offers. |
| Hiding MCP servers and skills | Works from the agents' documented options; not observed in every real session. |
| Multi-repo tasks (app + deploy + schema repos) | Repos the rest of the prompt describes are opened; repos that reference the app are listed with the reason, to tick in the launcher. Learning from git history is next. |
| Optional helpers | Graphify code graphs (`uv tool install graphifyy`) and [RTK](https://github.com/rtk-ai/rtk) output trimming, used only if installed. |
| Windows | Not tested. |

## Privacy

Everything runs on your machine. `blind` makes no network call of its own and sends nothing anywhere. The only network access is the `git fetch` it runs to update the repos it opens (`--no-sync` turns it off). Its usage numbers stay in `~/.cache/blinders` and hold no repo names, paths or prompts. Your real `~/.gemini` is never modified.

## Limits worth knowing

- Selection is lexical, not semantic: a prompt with no word in common with a repo will not open it. There is no list of words, stacks or file names inside blind, so it adapts to your repos; it also means it only knows what your repos say. Name the repo, declare a `[groups]` entry, or add a short `.blinders/*.md` map file.
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
