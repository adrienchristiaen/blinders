"""Are the optional helpers there, and will they be used in this launch? No Textual import."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Callable

from .config import Config


@dataclass
class ToolStatus:
    name: str
    state: str    # on (found and used) | off (found, not used now) | missing
    detail: str


def tool_statuses(cfg: Config, gemini: bool, which: Callable[[str], str | None] = shutil.which) -> list[ToolStatus]:
    """``gemini``: whether the CLI about to start is Gemini (rtk's hook is wired to Gemini sessions only)."""
    out: list[ToolStatus] = []

    graphify = which(cfg.graphify_bin)
    out.append(ToolStatus("graphify", "on", f"{graphify}: builds the code graphs") if graphify
               else ToolStatus("graphify", "missing", "not installed: uv tool install graphifyy (starting points then use file names)"))

    rtk = which("rtk")
    if not rtk:
        out.append(ToolStatus("rtk", "missing", "not installed (optional): cargo install --git https://github.com/rtk-ai/rtk"))
    elif not cfg.gemini_rtk:
        out.append(ToolStatus("rtk", "off", f"{rtk}: switched off by [gemini] rtk = false"))
    elif not gemini:
        out.append(ToolStatus("rtk", "off", f"{rtk}: installed, but blind wires it to Gemini sessions only"))
    elif not cfg.gemini_isolate_home:
        out.append(ToolStatus("rtk", "off", f"{rtk}: needs the isolated Gemini home ([gemini] isolate_home = true)"))
    else:
        out.append(ToolStatus("rtk", "on", f"{rtk}: condenses shell output in this Gemini session"))
    return out
