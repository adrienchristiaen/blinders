# Contributing

Thanks for looking. The project is small on purpose: standard library only (plus `tomli` on Python 3.10, and Textual for the optional UI).

## Run the tests

```bash
pip install -e ".[ui]"        # without the ui extra, the UI tests are skipped
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

CI runs the same on Python 3.10 to 3.13.

## Where things live

| Area | File |
|---|---|
| Find repos, build the index | `src/blinders/scan.py` |
| Links between repos (build and deploy references) | `src/blinders/relations.py` |
| Pick repos for a prompt | `src/blinders/select.py` |
| How each agent CLI is started | `src/blinders/adapters.py` |
| Gemini per-session home | `src/blinders/geminihome.py` |
| Model names the CLI offers | `src/blinders/models.py` |
| Starting points inside a repo | `src/blinders/hints.py` |
| Walking files, shared | `src/blinders/files.py` |
| Commands and launch flow | `src/blinders/cli.py` |

## Adding support for another agent CLI

Add an entry to `src/blinders/adapters.py` (binary, how to add a directory, how to pass the first prompt), or declare it in `[adapters.<name>]` in the config. Check the flags against the CLI's own `--help`, and add a test that builds the command with `--dry-run`. Say in the pull request how you verified it.

## Principles

- **Local and free at runtime**: no model call, no network, no telemetry.
- **Say what is verified**: if you did not run it against the real CLI, mark it experimental in the README.
- **Explain every decision**: when blind opens or hides something, there should be a reason the user can see.
- **No company-specific assumptions**: use generic names in code and tests.
- **No word lists**: no keyword, file-name or stack tables in the code. If a behavior needs to know something, learn it from the user's repos (see `text.ubiquitous` and `files.iter_files`) so it works for any stack and any language.
- **Tests first**: a change starts with a test that fails for the right reason.
