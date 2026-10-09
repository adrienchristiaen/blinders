# Changelog

## 0.12.1
- **Fewer stray repos.** A repo joins a repo the prompt already names only if several prompt words agree with it, or one of them is a word of its own name. A single loose word found in some README ("faut", "modifier") no longer opens it, so it can no longer pull in its own neighbours either. A neighbour of the named repo still opens on one matching word, since the link is already evidence.
- Common grammar words (`faut`, `doit`, `need`, `also`...) are ignored.
- **The launcher no longer shows an outdated selection.** A slow computation for an earlier version of the prompt could finish after a newer one and overwrite it; such results are now dropped.
- The launcher shows the full reason for the highlighted line under the lists (the list truncates it on a narrow terminal).
- A dotted name such as `raw_ugc_inbound.acme.com` is also searched by its identifier parts.

## 0.12.0
- **Exact identifiers are searched in every repo.** Words of the prompt that look like identifiers (`snake_case`, `camelCase`, letters mixed with digits, or anything in quotes or backticks) are looked up with `rg` (a bounded Python walk if `rg` is missing). Repos that contain them open right after the repos named in the prompt, within `max_repos`. Only repo names come back; nothing is sent to a model. An identifier found in half your repos or more is ignored, and repo names are not searched. This is how a schema change finds the deploy repo and the batch job nobody named.
- `[grep] enabled = false` turns it off; `max_literals` (default 8) caps the identifiers searched.

## 0.11.1
- The launcher shows a pill per helper on the right of the prompt: **green** = found and used in this launch, **amber** = found but not used now (rtk with Claude Code, or switched off in the config), **red** = not found. Hover a pill for the reason and the install command. The text-mode launch line ends with the same states (`graphify on, rtk missing`).

## 0.11.0
Architecture simplified: nothing in blind knows a language, a stack or a word list any more; everything is learned from your repos.
- **Selection** is in two lanes: repos named in the prompt, then the repos the *rest* of the prompt describes. Words are compared as stems, and a word found in half your repos or more is ignored (computed from your repos).
- **Related repos** open when the rest of the prompt matches what they contain, not when a hard-coded "deploy" or "data" word appears. Roles (`app`/`deploy`/`data`) are gone.
- **Relations** between repos are read from any small text file near the top of a repo, not from a list of build file names; a repo is known by its folder name and by the name its root files declare. A repo mentioned by nearly all the others weighs less.
- **Level 2** (starting points inside a repo) also works without a code graph, from file names and the names declared in YAML: dbt, SQL, notebooks. Test files are no longer treated specially.
- **Models**: the keyword router (light / standard / strong, `blind model`, `[models] auto`) is removed. Set a model with `--model`, the launcher selector or `[models.<cli>] default`. `[models.<cli>] light/standard/strong` are ignored.
- MCP launcher words (`npx`, `uvx`...) are no longer a built-in list: they are the words most of your own servers share.
- `blind list` no longer prints roles; the index no longer shows stack markers.

## 0.10.1
- The index now includes folder and file names and, for dbt projects, the model, source and table names declared in YAML. A repo with no README (only SQL and YAML) is found by what it contains.
- `blind --help` lists the everyday commands; the advanced ones are named at the bottom and keep their own `-h`.
- English quick start in `README.md`; the full reference stays in `README.fr.md`.
- MIT license, CI on Python 3.10 to 3.13, contributing guide.

## 0.10.0
- `blind models` lists the models your CLI offers; no default light model; warning about Gemini's own router call.

## Earlier
Four-step full-screen launcher, per-session Gemini home, model tiers, RTK hook, skills and MCP filtering, related repos, anonymous stats. See `git log`.
