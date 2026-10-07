"""How to start each agent CLI with a set of extra directories.

Defaults are data, overridable per CLI in ``config.toml`` under ``[adapters.<name>]``
(keys: binary, dir_flag, dir_style, prompt_flag). ``dir_style`` is one of:

- ``repeat``: ``--flag a --flag b``
- ``comma``:  ``--flag a,b``
- ``link``:   no flag; opened repos are symlinked into the blind workspace
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass
class Adapter:
    name: str
    binary: str
    dir_flag: str | None = None
    dir_style: str = "link"
    prompt_flag: str | None = None  # None: prompt is a positional argument


DEFAULTS: dict[str, Adapter] = {
    "claude": Adapter("claude", "claude", "--add-dir", "repeat"),
    "gemini": Adapter("gemini", "gemini", "--include-directories", "comma", "-i"),
    "codex": Adapter("codex", "codex", "--add-dir", "repeat"),
    "vibe": Adapter("vibe", "vibe", None, "link"),
}


def get_adapter(name: str, cfg: Config) -> Adapter:
    base = DEFAULTS.get(name)
    override = cfg.adapters.get(name, {})
    if base is None and not override:
        known = ", ".join(sorted(set(DEFAULTS) | set(cfg.adapters)))
        raise KeyError(f"unknown CLI '{name}' (known: {known})")
    adapter = Adapter(name=name, binary=name) if base is None else Adapter(**vars(base))
    for key in ("binary", "dir_flag", "dir_style", "prompt_flag"):
        if key in override:
            setattr(adapter, key, override[key])
    if adapter.dir_style not in ("repeat", "comma", "link"):
        raise ValueError(f"adapter '{name}': dir_style must be repeat, comma or link")
    if adapter.dir_style != "link" and not adapter.dir_flag:
        raise ValueError(f"adapter '{name}': dir_flag is required for dir_style={adapter.dir_style}")
    return adapter


def build_command(adapter: Adapter, prompt: str, dirs: list[str], extra: list[str]) -> list[str]:
    """Positional prompt goes first: variadic flags such as ``--add-dir`` would swallow it otherwise."""
    cmd = [adapter.binary]
    if prompt and not adapter.prompt_flag:
        cmd.append(prompt)
    if dirs and adapter.dir_style == "repeat":
        for d in dirs:
            cmd += [adapter.dir_flag, d]
    elif dirs and adapter.dir_style == "comma":
        cmd += [adapter.dir_flag, ",".join(dirs)]
    if prompt and adapter.prompt_flag:
        cmd += [adapter.prompt_flag, prompt]
    cmd += extra
    return cmd
