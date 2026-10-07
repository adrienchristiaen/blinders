"""How to start each agent CLI with a set of extra directories and MCP servers.

Defaults are data, overridable per CLI in ``config.toml`` under ``[adapters.<name>]``
(keys: binary, dir_flag, dir_style, prompt_flag, mcp_style, mcp_flag, mcp_auto).

``dir_style`` is one of:

- ``repeat``: ``--flag a --flag b``
- ``comma``:  ``--flag a,b``
- ``link``:   no flag; opened repos are symlinked into the blind workspace

``mcp_style`` is one of:

- ``allowlist``: repeat ``mcp_flag <name>`` per kept server (Gemini CLI)
- ``config``:    ``--strict-mcp-config <mcp_flag> <file>`` with only the kept servers (Claude Code)
- ``none``:      this CLI's MCP servers are not touched

``mcp_auto`` says whether ``blind run`` filters MCP servers by prompt without being asked.
Gemini's allowlist leaves the rest of its config alone, so it is on by default; Claude's
``--strict-mcp-config`` also ignores servers that are not in the file (plugins, connectors),
so it only applies when you pass ``--mcp``.

``skills_style`` is one of:

- ``claude-settings``: ``skills_flag <file>`` with ``skillOverrides`` hiding the dropped skills from the model
- ``gemini-workspace``: ``.gemini/settings.json`` written in the blind workspace, disabling the dropped skills
- ``none``:             this CLI's skills are not touched

Flags checked against ``claude --help`` and ``gemini --help`` (Gemini CLI: ``-i``,
``--include-directories``, ``--allowed-mcp-server-names``; Claude Code: ``--add-dir``,
``--mcp-config``, ``--strict-mcp-config``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config

# Gemini has no "allow nothing" value; an allowlist naming a server that does not exist is the closest.
NO_MCP_SENTINEL = "blinders-no-mcp"


@dataclass
class Adapter:
    name: str
    binary: str
    dir_flag: str | None = None
    dir_style: str = "link"
    prompt_flag: str | None = None  # None: prompt is a positional argument
    mcp_style: str = "none"
    mcp_flag: str | None = None
    mcp_auto: bool = False
    skills_style: str = "none"
    skills_flag: str | None = None
    skills_auto: bool = True


DEFAULTS: dict[str, Adapter] = {
    "claude": Adapter("claude", "claude", "--add-dir", "repeat", None, "config", "--mcp-config", False,
                       "claude-settings", "--settings"),
    "gemini": Adapter("gemini", "gemini", "--include-directories", "comma", "-i", "allowlist", "--allowed-mcp-server-names", True,
                       "gemini-workspace"),
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
    for key in ("binary", "dir_flag", "dir_style", "prompt_flag", "mcp_style", "mcp_flag", "mcp_auto",
                "skills_style", "skills_flag", "skills_auto"):
        if key in override:
            setattr(adapter, key, override[key])
    if adapter.dir_style not in ("repeat", "comma", "link"):
        raise ValueError(f"adapter '{name}': dir_style must be repeat, comma or link")
    if adapter.dir_style != "link" and not adapter.dir_flag:
        raise ValueError(f"adapter '{name}': dir_flag is required for dir_style={adapter.dir_style}")
    if adapter.mcp_style not in ("allowlist", "config", "none"):
        raise ValueError(f"adapter '{name}': mcp_style must be allowlist, config or none")
    if adapter.mcp_style != "none" and not adapter.mcp_flag:
        raise ValueError(f"adapter '{name}': mcp_flag is required for mcp_style={adapter.mcp_style}")
    if adapter.skills_style not in ("claude-settings", "gemini-workspace", "none"):
        raise ValueError(f"adapter '{name}': skills_style must be claude-settings, gemini-workspace or none")
    if adapter.skills_style == "claude-settings" and not adapter.skills_flag:
        raise ValueError(f"adapter '{name}': skills_flag is required for skills_style=claude-settings")
    return adapter


def build_command(
    adapter: Adapter,
    prompt: str,
    dirs: list[str],
    extra: list[str],
    mcp_names: list[str] | None = None,
    mcp_file: str | None = None,
    settings_file: str | None = None,
) -> list[str]:
    """Positional prompt goes first: variadic flags such as ``--add-dir`` would swallow it otherwise.

    ``mcp_names`` (allowlist style) is the list of servers to keep; ``None`` leaves MCP alone.
    ``mcp_file`` (config style) is the path of a JSON file holding only the kept servers.
    """
    cmd = [adapter.binary]
    if prompt and not adapter.prompt_flag:
        cmd.append(prompt)
    if dirs and adapter.dir_style == "repeat":
        for d in dirs:
            cmd += [adapter.dir_flag, d]
    elif dirs and adapter.dir_style == "comma":
        cmd += [adapter.dir_flag, ",".join(dirs)]
    if adapter.mcp_style == "allowlist" and mcp_names is not None:
        for n in mcp_names or [NO_MCP_SENTINEL]:
            cmd += [adapter.mcp_flag, n]
    elif adapter.mcp_style == "config" and mcp_file:
        cmd += ["--strict-mcp-config", adapter.mcp_flag, mcp_file]
    if adapter.skills_style == "claude-settings" and settings_file:
        cmd += [adapter.skills_flag, settings_file]
    if prompt and adapter.prompt_flag:
        cmd += [adapter.prompt_flag, prompt]
    cmd += extra
    return cmd
