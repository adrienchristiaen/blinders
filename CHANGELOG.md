# Changelog

## 0.10.1
- The index now includes folder and file names and, for dbt projects, the model, source and table names declared in YAML. A repo with no README (only SQL and YAML) is found by what it contains.
- `blind --help` lists the everyday commands; the advanced ones are named at the bottom and keep their own `-h`.
- English quick start in `README.md`; the full reference stays in `README.fr.md`.
- MIT license, CI on Python 3.10 to 3.13, contributing guide.

## 0.10.0
- `blind models` lists the models your CLI offers; no default light model; warning about Gemini's own router call.

## Earlier
Four-step full-screen launcher, per-session Gemini home, model tiers, RTK hook, skills and MCP filtering, related repos, anonymous stats. See `git log`.
