"""Data the full-screen launcher and the CLI exchange. No Textual import: usable without the ``[ui]`` extra."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .pipeline import Emit, Event


@dataclass
class Item:
    name: str
    selected: bool
    note: str = ""
    locked: bool = False   # always kept (config), cannot be unticked


@dataclass
class UiPlan:
    repos: list[Item] = field(default_factory=list)
    mcp: list[Item] = field(default_factory=list)
    skills: list[Item] = field(default_factory=list)
    info: list[str] = field(default_factory=list)


@dataclass
class UiResult:
    cli: str
    prompt: str
    repos: list[str]
    mcp: list[str]
    skills: list[str]
    recap: list[str] = field(default_factory=list)


class Backend:
    """What the screen needs. The real one lives in cli.py; tests use fakes."""

    def sync(self, emit: Emit, stop: Callable[[], bool]) -> None:
        emit(Event(1, "done", "skipped", level="warn"))

    def graphs(self, emit: Emit, stop: Callable[[], bool]) -> None:
        emit(Event(2, "done", "skipped", level="warn"))

    def plan(self, cli: str, prompt: str) -> UiPlan:
        return UiPlan()

    def hints(self, cli: str, prompt: str, names: list[str]) -> list[str]:
        return []

    def map(self, prompt: str, names: list[str], emit: Emit) -> None:
        emit(Event(4, "done", "skipped", level="warn"))
