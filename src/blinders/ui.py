"""Full-screen launcher (optional, needs ``pip install "blinders[ui]"``).

A prompt field and three checklists (repos, MCP servers, skills). The lists are ticked from the prompt as
you type and you can override any box; Enter on the prompt (or Ctrl+L anywhere) closes the screen and
returns the choice, and the caller then starts the real CLI. The screen holds no selection logic of its
own: it calls the functions it is given, so the same engine serves the text mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, Header, Select, SelectionList, Static, TextArea
from textual.widgets.selection_list import Selection

DEBOUNCE = 0.25


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
    info: list[str] = field(default_factory=list)   # related repos, notes


@dataclass
class UiResult:
    cli: str
    prompt: str
    repos: list[str]
    mcp: list[str]
    skills: list[str]


PlanFn = Callable[[str, str], UiPlan]                 # (cli, prompt) -> automatic plan
HintsFn = Callable[[str, str, list[str]], list[str]]  # (cli, prompt, repo names) -> hint lines

CSS = """
Screen { background: #0c1a30; color: #ece9e1; }
Header { background: #112343; color: #ffb84d; }
Footer { background: #112343; }
#top { height: auto; padding: 0 1; margin-top: 1; }
#cli { width: 18; margin-right: 1; }
#prompt { width: 1fr; height: auto; min-height: 3; max-height: 12; background: #112343; border: tall #2b4373; }
#prompt:focus { border: tall #ffb84d; }
#status { height: 1; padding: 0 2; color: #8fa3c4; }
#lists { height: 1fr; padding: 0 1; }
.panel { width: 1fr; border: round #2b4373; margin-right: 1; border-title-color: #8fa3c4; border-title-style: bold; }
.panel:focus-within { border: round #ffb84d; border-title-color: #ffb84d; }
SelectionList > .selection-list--button { color: #0c1a30; background: #1b2f55; }
SelectionList > .selection-list--button-selected { color: #ffb84d; background: #1b2f55; text-style: bold; }
SelectionList > .selection-list--button-highlighted { color: #0c1a30; background: #1b2f55; }
SelectionList > .selection-list--button-selected-highlighted { color: #ffb84d; background: #1b2f55; text-style: bold; }
.panel > SelectionList { background: #0c1a30; border: none; }
#info { height: auto; max-height: 10; margin: 0 2; padding: 0 1; border-left: tall #ffb84d; color: #c9d3e6; }
Select { background: #112343; }
"""


class PromptArea(TextArea):
    """Multi-line prompt. A pasted paragraph is kept whole (a single-line input would keep only its first line).

    Enter launches while the prompt is one line; once it has several lines, Enter adds a new line
    and Ctrl+L launches."""

    class Submit(Message):
        pass

    async def _on_key(self, event) -> None:
        if event.key == "enter" and "\n" not in self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.Submit())
            return
        await super()._on_key(event)


def _label(item: Item) -> Text:
    t = Text(item.name)
    if item.locked:
        t.append("  always", style="dim")
    elif item.note:
        t.append(f"  {item.note}", style="dim")
    return t


class BlindApp(App[UiResult | None]):
    CSS = CSS
    TITLE = "blind"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+l", "launch", "Launch", priority=True),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(self, clis: list[str], cli: str, prompt: str, plan_fn: PlanFn, hints_fn: HintsFn | None = None,
                 status: str = "", total_repos: int = 0) -> None:
        super().__init__()
        self.clis, self.cli, self.initial_prompt = clis, cli, prompt
        self.plan_fn, self.hints_fn, self.status_text, self.total_repos = plan_fn, hints_fn, status, total_repos
        self.plan = UiPlan()
        self.overrides: dict[str, dict[str, bool]] = {"repos": {}, "mcp": {}, "skills": {}}
        self._timer = None
        self._computed_for: tuple[str, str] | None = None

    # --- layout ---------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="top"):
            yield Select([(c, c) for c in self.clis], value=self.cli, allow_blank=False, id="cli")
            yield PromptArea(self.initial_prompt, id="prompt", soft_wrap=True, tab_behavior="focus",
                             placeholder="Your prompt, paste as much as you like (empty = fully blind session)")
        yield Static(self.status_text, id="status")
        with Horizontal(id="lists"):
            for key, title in (("repos", "Repos"), ("mcp", "MCP servers"), ("skills", "Skills")):
                with Vertical(classes="panel", id=f"panel-{key}"):
                    yield SelectionList(id=key)
        yield Static("", id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Enter launches a one-line prompt  ·  Ctrl+L always  ·  Tab move  ·  Space tick  ·  Esc cancel"
        for key, title in (("repos", "Repos"), ("mcp", "MCP servers"), ("skills", "Skills")):
            self.query_one(f"#panel-{key}").border_title = title
        self.query_one("#prompt", PromptArea).focus()
        self._recompute()

    # --- recompute on prompt / CLI change ---------------------------------------------------------
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(DEBOUNCE, self._recompute)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value != self.cli:
            self.cli = str(event.value)
            self.overrides["mcp"].clear()
            self.overrides["skills"].clear()
            self._recompute()

    def _recompute(self) -> None:
        prompt = self._prompt()
        cli = self.cli
        self.run_worker(lambda: self._work(cli, prompt), thread=True, exclusive=True, group="plan")

    def _work(self, cli: str, prompt: str) -> None:
        plan = self.plan_fn(cli, prompt)
        self.call_from_thread(self._apply, cli, prompt, plan)

    def _apply(self, cli: str, prompt: str, plan: UiPlan) -> None:
        if cli != self.cli:
            return  # a newer CLI choice is already pending
        self.plan = plan
        self._computed_for = (cli, prompt)
        for key in ("repos", "mcp", "skills"):
            items: list[Item] = getattr(plan, key)
            widget = self.query_one(f"#{key}", SelectionList)
            widget.clear_options()
            widget.add_options([
                Selection(_label(i), i.name, i.locked or self.overrides[key].get(i.name, i.selected)) for i in items
            ])
            if widget.option_count:
                widget.highlighted = 0
            self._title(key)
        self._refresh_info()

    def _title(self, key: str) -> None:
        widget = self.query_one(f"#{key}", SelectionList)
        names = {"repos": "Repos", "mcp": "MCP servers", "skills": "Skills"}
        total = len(getattr(self.plan, key))
        self.query_one(f"#panel-{key}").border_title = f"{names[key]}  {len(widget.selected)}/{total}"

    # --- user toggles are remembered, so a new prompt does not undo them ----------------------------
    def on_selection_list_selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        key = event.selection_list.id or ""
        if key not in self.overrides:
            return
        name = str(event.selection.value)
        item = next((i for i in getattr(self.plan, key) if i.name == name), None)
        if item and item.locked:
            event.selection_list.select(name)
            return
        self.overrides[key][name] = name in event.selection_list.selected
        self._title(key)
        if key == "repos":
            self._refresh_info()

    def _refresh_info(self) -> None:
        self._show_info(list(self.plan.info))
        if self.hints_fn:
            names = [str(v) for v in self.query_one("#repos", SelectionList).selected]
            prompt, cli = self._prompt(), self.cli
            self.run_worker(lambda: self._hints_work(cli, prompt, names), thread=True, exclusive=True, group="hints")

    def _hints_work(self, cli: str, prompt: str, names: list[str]) -> None:
        lines = self.hints_fn(cli, prompt, names) if names else []
        self.call_from_thread(self._show_info, list(self.plan.info) + lines)

    def _show_info(self, lines: list[str]) -> None:
        info = self.query_one("#info", Static)
        info.display = bool(lines)
        info.update(Text("\n".join(lines)))

    def _prompt(self) -> str:
        return self.query_one("#prompt", PromptArea).text.strip()

    # --- leaving -----------------------------------------------------------------------------------
    def result(self) -> UiResult:
        def picked(key: str) -> list[str]:
            return [str(v) for v in self.query_one(f"#{key}", SelectionList).selected]
        return UiResult(self.cli, self._prompt(), picked("repos"), picked("mcp"), picked("skills"))

    def action_launch(self) -> None:
        self._sync_then_exit()

    def action_cancel(self) -> None:
        self.exit(None)

    def on_prompt_area_submit(self, event: PromptArea.Submit) -> None:
        self._sync_then_exit()

    def _sync_then_exit(self) -> None:
        # Make sure the lists match the prompt as it is now before reading them (the debounce may not have fired).
        prompt = self._prompt()
        if self._computed_for != (self.cli, prompt):
            if self._timer is not None:
                self._timer.stop()
            self._apply(self.cli, prompt, self.plan_fn(self.cli, prompt))
        self.exit(self.result())


def run_ui(clis: list[str], cli: str, prompt: str, plan_fn: PlanFn, hints_fn: HintsFn | None = None,
           status: str = "", total_repos: int = 0) -> UiResult | None:
    return BlindApp(clis, cli, prompt, plan_fn, hints_fn, status, total_repos).run()
