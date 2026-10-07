"""Full-screen launcher (optional, needs ``pip install "blinders[ui]"``).

``blind`` takes over and shows four steps as they happen:

1. update repos   (checkout of the root branch, pull, every repo)
2. code graphs    (one Graphify graph per repo)
3. select         (repos, MCP servers and skills kept for this request; tick boxes to override)
4. map            (starting points inside each chosen repo)

Steps 1 and 2 start at once and run while you type the prompt. Enter (one-line prompt) or Ctrl+L
validates step 3, step 4 runs, then the screen closes and the caller starts the real CLI. The screen holds
no selection logic: it calls a ``Backend`` and shows what comes back.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, Header, RichLog, Select, SelectionList, Static, TextArea
from textual.widgets.selection_list import Selection

from .pipeline import STEP_TITLES, Emit, Event  # noqa: F401  (Emit re-exported for callers)
from .uimodel import Backend, Item, UiPlan, UiResult

DEBOUNCE = 0.25
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR = 14
AMBER, GREEN, RED, DIM = "#ffb84d", "#7fd18b", "#ff7b72", "#6f86ad"


CSS = """
Screen { background: #0c1a30; color: #ece9e1; }
Header { background: #112343; color: #ffb84d; }
Footer { background: #112343; }
#top { height: auto; padding: 0 1; margin-top: 1; }
#cli { width: 18; margin-right: 1; }
#prompt { width: 1fr; height: auto; min-height: 3; max-height: 10; background: #112343; border: tall #2b4373; }
#prompt:focus { border: tall #ffb84d; }
#steps { height: auto; margin: 1 2 0 2; padding: 0 1; border: round #2b4373; }
#body { height: 1fr; padding: 0 1; }
#log { height: 1fr; margin: 0 1; border: round #2b4373; background: #0c1a30; scrollbar-background: #0c1a30; scrollbar-color: #2b4373; scrollbar-color-hover: #ffb84d; }
#lists { height: 1fr; }
.panel { width: 1fr; border: round #2b4373; margin-right: 1; border-title-color: #8fa3c4; border-title-style: bold; }
.panel:focus-within { border: round #ffb84d; border-title-color: #ffb84d; }
.panel > SelectionList { background: #0c1a30; border: none; }
SelectionList > .selection-list--button { color: #0c1a30; background: #1b2f55; }
SelectionList > .selection-list--button-selected { color: #ffb84d; background: #1b2f55; text-style: bold; }
SelectionList > .selection-list--button-highlighted { color: #0c1a30; background: #1b2f55; }
SelectionList > .selection-list--button-selected-highlighted { color: #ffb84d; background: #1b2f55; text-style: bold; }
#info { height: auto; max-height: 9; margin: 0 2; padding: 0 1; border-left: tall #ffb84d; color: #c9d3e6; }
Select { background: #112343; }
"""


class PromptArea(TextArea):
    """Multi-line prompt. A pasted paragraph is kept whole (a single-line input keeps only its first line).

    Enter validates while the prompt is one line; once it has several lines, Enter adds a new line
    and Ctrl+L validates."""

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


@dataclass
class StepState:
    status: str = "pending"     # pending | running | done | warn | error
    detail: str = ""
    done: int = 0
    total: int = 0
    current: str = ""


class BlindApp(App[UiResult | None]):
    CSS = CSS
    TITLE = "blind"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+l", "launch", "Validate", priority=True),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(self, clis: list[str], cli: str, prompt: str, backend: Backend, status: str = "") -> None:
        super().__init__()
        self.clis, self.cli, self.initial_prompt, self.backend, self.status_text = clis, cli, prompt, backend, status
        self.plan = UiPlan()
        self.overrides: dict[str, dict[str, bool]] = {"repos": {}, "mcp": {}, "skills": {}}
        self.steps = {i: StepState() for i in STEP_TITLES}
        self.phase = "fleet"               # fleet -> select -> map -> done
        self.queued = False                # validated before the graphs were ready
        self._stop = threading.Event()
        self._timer = None
        self._tick = 0
        self._computed_for: tuple[str, str] | None = None

    # --- layout ---------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="top"):
            yield Select([(c, c) for c in self.clis], value=self.cli, allow_blank=False, id="cli")
            yield PromptArea(self.initial_prompt, id="prompt", soft_wrap=True, tab_behavior="focus",
                             placeholder="Type or paste your prompt while the repos are prepared (empty = fully blind session)")
        yield Static("", id="steps")
        with Vertical(id="body"):
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
            with Horizontal(id="lists"):
                for key in ("repos", "mcp", "skills"):
                    with Vertical(classes="panel", id=f"panel-{key}"):
                        yield SelectionList(id=key)
            yield Static("", id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.status_text or "four steps, then your CLI"
        for key, title in (("repos", "Repos"), ("mcp", "MCP servers"), ("skills", "Skills")):
            self.query_one(f"#panel-{key}").border_title = title
        self.query_one("#lists").display = False
        self.query_one("#info").display = False
        self.query_one("#prompt", PromptArea).focus()
        self.set_interval(0.1, self._spin)
        self._render_steps()
        self.run_worker(self._fleet, thread=True, group="fleet")

    # --- steps 1 and 2 run at once, in a thread ---------------------------------------------------
    def _emit(self, ev: Event) -> None:
        try:
            self.call_from_thread(self._on_event, ev)
        except Exception:  # noqa: BLE001 - the app is closing
            pass

    def _fleet(self) -> None:
        try:
            self.backend.sync(self._emit, self._stop.is_set)
            if not self._stop.is_set():
                self.backend.graphs(self._emit, self._stop.is_set)
        except Exception as exc:  # noqa: BLE001 - show it, keep going with what exists
            self._emit(Event(2 if self.steps[1].status == "done" else 1, "done", f"error: {exc}", level="error"))
        if not self._stop.is_set():
            try:
                self.call_from_thread(self._enter_select)
            except Exception:  # noqa: BLE001
                pass

    def _on_event(self, ev: Event) -> None:
        st = self.steps[ev.step]
        if ev.kind == "start":
            st.status, st.detail, st.done, st.total = "running", ev.text, 0, ev.total
        elif ev.kind == "progress":
            st.done, st.total, st.current = ev.done, ev.total or st.total, ev.text
        elif ev.kind == "line":
            color = {"ok": GREEN, "warn": AMBER, "error": RED}.get(ev.level, DIM)
            mark = {"ok": "✔", "warn": "▲", "error": "✖"}.get(ev.level, "•")
            line = Text(f"{ev.step}  ")
            line.stylize(DIM)
            line.append(f"{mark} ", style=color)
            line.append(ev.text)
            self.query_one("#log", RichLog).write(line)
        elif ev.kind == "done":
            st.status = {"warn": "warn", "error": "error"}.get(ev.level, "done")
            st.detail, st.current = ev.text, ""
            if ev.total:
                st.done, st.total = ev.total, ev.total
        self._render_steps()

    def _spin(self) -> None:
        self._tick += 1
        if any(s.status == "running" for s in self.steps.values()) or self.queued:
            self._render_steps()

    def _render_steps(self) -> None:
        out = Text()
        for i, title in STEP_TITLES.items():
            st = self.steps[i]
            running = st.status == "running"
            icon, color = {
                "pending": ("○", DIM), "running": (SPINNER[self._tick % len(SPINNER)], AMBER),
                "done": ("✔", GREEN), "warn": ("▲", AMBER), "error": ("✖", RED),
            }[st.status]
            out.append(f" {icon} ", style=color)
            out.append(f"{i}  {title:<24}", style="bold" if st.status != "pending" else DIM)
            if running and st.total:
                filled = int(BAR * st.done / st.total)
                out.append("█" * filled, style=AMBER)
                out.append("░" * (BAR - filled), style=DIM)
                out.append(f" {st.done}/{st.total}  ", style="bold")
                out.append(st.current or st.detail, style=DIM)
            elif st.detail:
                out.append(st.detail, style=DIM if st.status in ("done",) else color)
            if i < len(STEP_TITLES):
                out.append("\n")
        self.query_one("#steps", Static).update(out)

    # --- step 3: selection --------------------------------------------------------------------------
    def _enter_select(self) -> None:
        self.phase = "select"
        self.steps[3].status, self.steps[3].detail = "running", "tick what you want, then Enter (or Ctrl+L)"
        self._render_steps()
        self.query_one("#log").display = False
        self.query_one("#lists").display = True
        self._recompute()
        if self.queued:
            self._begin_map()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self.phase != "select":
            return
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(DEBOUNCE, self._recompute)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value != self.cli:
            self.cli = str(event.value)
            self.overrides["mcp"].clear()
            self.overrides["skills"].clear()
            if self.phase == "select":
                self._recompute()

    def _recompute(self) -> None:
        if self.phase != "select":
            return
        prompt, cli = self._prompt(), self.cli
        self.run_worker(lambda: self._plan_work(cli, prompt), thread=True, exclusive=True, group="plan")

    def _plan_work(self, cli: str, prompt: str) -> None:
        plan = self.backend.plan(cli, prompt)
        self.call_from_thread(self._apply, cli, prompt, plan)

    def _apply(self, cli: str, prompt: str, plan: UiPlan) -> None:
        if cli != self.cli:
            return
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
        names = [str(v) for v in self.query_one("#repos", SelectionList).selected]
        prompt, cli = self._prompt(), self.cli
        self.run_worker(lambda: self._hints_work(cli, prompt, names), thread=True, exclusive=True, group="hints")

    def _hints_work(self, cli: str, prompt: str, names: list[str]) -> None:
        lines = self.backend.hints(cli, prompt, names) if names else []
        self.call_from_thread(self._show_info, list(self.plan.info) + lines)

    def _show_info(self, lines: list[str]) -> None:
        info = self.query_one("#info", Static)
        info.display = bool(lines) and self.phase == "select"
        info.update(Text("\n".join(lines)))

    # --- validating, step 4, leaving ---------------------------------------------------------------------
    def _prompt(self) -> str:
        return self.query_one("#prompt", PromptArea).text.strip()

    def _picked(self, key: str) -> list[str]:
        return [str(v) for v in self.query_one(f"#{key}", SelectionList).selected]

    def action_launch(self) -> None:
        self._validate()

    def on_prompt_area_submit(self, event: PromptArea.Submit) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.phase in ("map", "done"):
            return
        self.queued = True
        if self.phase == "fleet":
            self.steps[3].detail = "queued, continues when the graphs are ready"
            self._render_steps()
            return
        self._begin_map()

    def _begin_map(self) -> None:
        prompt = self._prompt()
        if self._computed_for != (self.cli, prompt):   # the debounce may not have fired yet
            if self._timer is not None:
                self._timer.stop()
            self._apply(self.cli, prompt, self.backend.plan(self.cli, prompt))
        self.phase = "map"
        repos = self._picked("repos")
        st = self.steps[3]
        st.status = "done"
        st.detail = (f"{len(repos)} of {len(self.plan.repos)} repos, {len(self._picked('mcp'))} MCP servers, "
                     f"{len(self._picked('skills'))} skills")
        self.query_one("#lists").display = False
        self.query_one("#info").display = False
        log = self.query_one("#log", RichLog)
        log.clear()
        log.display = True
        self._render_steps()
        self.run_worker(lambda: self._map_work(prompt, repos), thread=True, group="map")

    def _map_work(self, prompt: str, repos: list[str]) -> None:
        try:
            self.backend.map(prompt, repos, self._emit)
        except Exception as exc:  # noqa: BLE001
            self._emit(Event(4, "done", f"error: {exc}", level="error"))
        self.call_from_thread(self._finish)

    def _finish(self) -> None:
        self.phase = "done"
        self.set_timer(0.7, lambda: self.exit(self._result()))

    def _recap(self) -> list[str]:
        mark = {"done": "✔", "warn": "▲", "error": "✖", "pending": "○", "running": "…"}
        return [f"{mark[self.steps[i].status]} {i}  {title:<22}{self.steps[i].detail}" for i, title in STEP_TITLES.items()]

    def _result(self) -> UiResult:
        return UiResult(self.cli, self._prompt(), self._picked("repos"), self._picked("mcp"), self._picked("skills"), self._recap())

    def action_cancel(self) -> None:
        self._stop.set()
        self.exit(None)


def run_ui(clis: list[str], cli: str, prompt: str, backend: Backend, status: str = "") -> UiResult | None:
    return BlindApp(clis, cli, prompt, backend, status).run()
