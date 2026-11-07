"""Escapable modal prompts: secrets, confirmation, file editing and help."""

from __future__ import annotations

from typing import Any, TypeVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Input,
    LoadingIndicator,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.text_area import Selection

from .. import providers
from . import onboard

_T = TypeVar("_T")


def _point_to_offset(text: str, row: int, column: int) -> int:
    """Inverse of `_offset_to_point`: where a `(row, column)` pair starts."""
    lines = text.split("\n")
    return sum(len(line) + 1 for line in lines[:row]) + column


def _offset_to_point(text: str, offset: int) -> tuple[int, int]:
    """Line and column of a character offset. Pure, and the only place the
    buffer's newline arithmetic lives."""
    row = text.count("\n", 0, offset)
    column = offset - (text.rfind("\n", 0, offset) + 1)
    return row, column

# `Screen` as a selector does NOT match a `ModalScreen` in Textual 8.2: the
# subclass has its own DEFAULT_CSS (`layout: vertical`), and a type selector on the
# base class loses to it. Every modal was therefore pinned to the top-left corner
# despite the rule below reading correctly. `ModalScreen` matches the subclass, and
# `*` covers the concrete class in case a future Textual changes that.
_CSS = """
ModalScreen, * {
    align: center middle;
}

/* `05` §13.4: width follows the content, bounded by the terminal, so a one-line
   prompt is not stretched and the editor is not pinned narrower than its text. */
.prompt-box {
    width: 60;
    max-width: 100%;
    height: auto;
    border: thick $accent;
    padding: 1 2;
}

.prompt-title {
    text-style: bold;
}

.prompt-hint {
    color: $text-muted;
}
"""


class PromptBox(ModalScreen[_T]):
    """A modal that can always be left with Esc.

    `ModalScreen` sets `_modal`, which suppresses app-level bindings, so a modal
    without its own Esc binding traps the user and leaves the `push_screen_wait`
    worker running forever. Inheriting this class is what makes that impossible:
    a subclass's `BINDINGS` extend it rather than replace the guarantee.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = _CSS

    def action_cancel(self) -> None:
        self.dismiss(None)


class SecretPrompt(PromptBox[str | None]):
    """Masked one-value prompt for `05-bridge.md` §8 masked input."""

    BINDINGS = [Binding("enter", "submit", "Store", show=False)]
    CSS = _CSS

    #: Textual focuses this after `_compose` has mounted the children, using a
    #: non-raising `query`. Focusing from `on_mount` instead races the mount and
    #: raises `NoMatches` when the Input is not queryable yet.
    AUTO_FOCUS = "#secret-input"

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(f"secret: {self.key}", classes="prompt-title")
            yield Input(password=True, id="secret-input")
            yield Static("Enter: store | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#secret-input", Input).value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmPrompt(PromptBox[bool]):
    """Yes/no gate for `05-bridge.md` §7 `/tasks delete` confirmation."""

    BINDINGS = [
        Binding("y", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel", show=False),
    ]
    CSS = _CSS

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(self.question, classes="prompt-title")
            yield Static("y: confirm | n: cancel", classes="prompt-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class FilterPrompt(PromptBox[str | None]):
    """One narrowing line: a log filter, or a file search (`05` §5.4, §13.5).

    The two screens want the same widget with a different seed and title, so the
    seed is either a `LogFilter` (read for `tag`/`min_level`) or a plain string.
    """

    BINDINGS = [Binding("enter", "submit", "Apply", show=False)]
    CSS = _CSS
    AUTO_FOCUS = "#filter-input"

    def __init__(
        self,
        current: object = None,
        title: str = "log filter",
        placeholder: str = "tag[:level]",
    ) -> None:
        super().__init__()
        self.current = current
        self.title = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        if isinstance(self.current, str):
            seeded = self.current
        else:
            tag = getattr(self.current, "tag", "") or ""
            level = getattr(self.current, "min_level", "debug")
            seeded = f"{tag}:{level}" if level != "debug" else tag
        with Vertical(classes="prompt-box"):
            yield Static(self.title, classes="prompt-title")
            yield Input(value=seeded, placeholder=self.placeholder, id="filter-input")
            yield Static("Enter: apply | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#filter-input", Input).value.strip() or None)


class ValuePrompt(PromptBox[str | None]):
    """One line of text under a title: a `05` §5.7 config value, or a host path
    for the file transfers of §5.8.1. The title is the question, not a key."""

    BINDINGS = [Binding("enter", "submit", "Apply", show=False)]
    CSS = _CSS
    AUTO_FOCUS = "#value-input"

    def __init__(self, path: str, current: str) -> None:
        super().__init__()
        self.path = path
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(self.path, classes="prompt-title")
            yield Input(value=self.current, id="value-input")
            yield Static("Enter: apply | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # An emptied field is a cancel, not a request to blank the setting.
        self.dismiss(self.query_one("#value-input", Input).value.strip() or None)

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#value-input", Input).value.strip() or None)


def _thinking_levels(entries: list[Any], provider_id: str) -> list[str]:
    """The levels to offer for `provider_id`, or none.

    `05` §7.1: a provider whose `extra_body` takes no level gets no picker, because a
    level written where the member is rejected turns every call into a 400.
    """
    entry = next((p for p in entries if p.id == provider_id), None)
    return list(entry.thinking_levels) if entry is not None else []


class _StepBox(PromptBox[Any]):
    """Shared chrome for the three `/onboard` steps.

    `05` §13.6: a step asks only what its own test can check, shows the path it
    writes, and says what a refusal means. The test is the device answering, which is
    why the network step comes before the provider step and the provider step before
    the model step — each unlock is the previous step's proof.
    """

    BINDINGS = [
        Binding("ctrl+s", "submit", "Continue", show=False),
        Binding("ctrl+g", "pass_step", "Keep as is", show=False),
    ]
    CSS = (
        _CSS
        + """
.prompt-box.step-box {
    width: 84;
    max-width: 100%;
    max-height: 92%;
}
.step-title {
    text-style: bold;
    color: $accent;
}
.step-label {
    color: $text-muted;
}
.step-note {
    color: $text-muted;
}
"""
    )

    #: What the reader sees under the title: this step's own heading, the config path
    #: it writes, and what it proves.
    TITLE = ""
    PATH = ""
    #: What this step proves, stated before the fields so the reason for the question
    #: is not deferred to the result.
    PROVES = ""
    SUMMARY = (
        "Ctrl+S: continue | Ctrl+G: go on without writing | Esc: cancel | "
        "blank fields are left alone"
    )

    def __init__(self) -> None:
        super().__init__()
        #: Filled by `app.py` before the next step is shown: the line the previous
        #: step's own test produced, or empty on the first step.
        self.verdict = ""
        #: Set by `Ctrl+G`. The step is dismissed with its stored values and writes
        #: nothing, which is what makes re-running the wizard to fix one domain
        #: leave the other two alone.
        self.passed = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box step-box"):
            yield Static(f"First run — {self.TITLE}", classes="step-title")
            yield Static(self.PATH, classes="step-note")
            with VerticalScroll(id="step-body"):
                for widget in self.fields():
                    yield widget
            yield Static(self.SUMMARY, classes="step-note")
            yield Static(self.pass_note(), id="step-pass", classes="step-note")
            yield Static("", id="step-verdict", classes="step-note")

    def fields(self) -> Any:
        """The step's widgets. Each is yielded by its own subclass."""
        return []

    def on_mount(self) -> None:
        self.query_one("#step-verdict", Static).update(
            f"previous step: {self.verdict}" if self.verdict else ""
        )

    def action_submit(self) -> None:
        self.dismiss(self.answer())

    def action_pass_step(self) -> None:
        """Keep what the device has for this step and move on without writing.

        Always available. Refusing on an unconfigured step was indistinguishable from
        a broken key: the reader pressed it, nothing happened, and the screen did not
        say which half was missing. `pass_note` states the cost instead, before the
        key is pressed, and the action lets the reader decide.
        """
        self.passed = True
        self.dismiss(self.answer())

    def can_pass(self) -> bool:
        """Whether the stored config already answers this step, which `pass_note` says."""
        return False

    def pass_note(self) -> str:
        """One line naming what `Ctrl+G` would keep, and what it would leave unset."""
        return ""

    def answer(self) -> Any:
        return None

    @staticmethod
    def _select(options: list[tuple[str, str]], value: str, widget_id: str,
                **kwargs: Any) -> "Select":
        """A `Select` whose current value is real or absent.

        `Select.BLANK` is `False`, and the widget refuses a `False` value, so the value
        is passed only when there is one; `allow_blank` decides the rest.
        """
        if value:
            # Textual's `allow_blank` defaults on and prepends a `Select.NULL` row,
            # which the caller's own list already accounts for.
            kwargs.setdefault("allow_blank", False)
            return Select(options, value=value, id=widget_id, **kwargs)
        return Select(options, id=widget_id, **kwargs)

    @staticmethod
    def _options(values: list[str], current: str) -> list[tuple[str, str]]:
        """`Select` rows, with the current value present even if the list lacks it.

        A `Select` whose value is absent from its own options raises on mount, and `id`
        and an SSID are both free text, so a device configured elsewhere holds values
        no list here names.
        """
        rows = [(value, value) for value in values]
        if current and current not in values:
            rows.insert(0, (current, current))
        return rows

    @staticmethod
    def _existing(current: dict[str, Any], path: str) -> object | None:
        """One config value by dotted path, from either shape `app.py` may hold.

        `app.py` keeps the config flattened (`settings.flatten`), so a nested read
        finds nothing and every field opens empty. Both shapes are accepted because
        these screens are constructed directly in tests with a nested document.
        """
        current = current or {}
        if path in current:
            return current[path]
        value: object = current
        for segment in path.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(segment)
        return value


class NetworkStep(_StepBox):
    """`05` §7 step 1: the network *and* the time zone, proved by one `reload`.

    They are one step because they are one test. `reload` reruns the boot sequence,
    which is what applies both `wifi.*` and `ntp.timezone`, and `status` answers both
    facts in one reply — `wifi.state` and `time.tz`. Splitting them would mean two
    reloads, each blocking up to one `connect_timeout` per configured network, to
    learn what a single reply already says.
    """

    AUTO_FOCUS = "#step-ssid"
    TITLE = "Network and time"
    PATH = "writes wifi.networks[0], ntp.timezone and ntp.servers"
    PROVES = "reload, then status: did the station join, and which zone is held?"

    def __init__(self, current: dict[str, Any], scan: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.current = current
        self.scan = scan or []

    def fields(self) -> Any:
        networks = self._existing(self.current, "wifi.networks") or []
        first = networks[0] if isinstance(networks, list) and networks else {}
        current_ssid = str(first.get("ssid") or "") if isinstance(first, dict) else ""
        scanned = [str(n.get("ssid") or "") for n in self.scan if n.get("ssid")]
        return [
            Static("ssid (from the scan; type one the scan cannot see)",
                   classes="step-label"),
            self._select(
                self._options(scanned, current_ssid),
                current_ssid or (scanned[0] if scanned else ""),
                "step-ssid-select",
            ),
            Input(current_ssid, placeholder="or type an ssid", id="step-ssid"),
            Static("password (the nvs secret wifi_psk)", classes="step-label"),
            Input(password=True, id="step-password"),
            Static("region (derives the TZ string and its ntppool zone)",
                   classes="step-label"),
            Select(
                self._region_rows(),
                value=self._region_value(),
                allow_blank=False,
                id="step-region",
            ),
        ]

    def _region_rows(self) -> list[tuple[str, str]]:
        """Region names, plus the stored TZ as its own row when the table lacks it.

        A stored zone the table does not list is kept rather than replaced by the
        first region: it is a working value the reader chose, and a guess at a region
        could also replace a working `ntp.servers`.
        """
        rows = [(name, name) for name in providers.region_names()]
        tz = self._stored_tz()
        if tz and not providers.region_for_timezone(tz):
            rows.insert(0, (f"custom: {tz}", tz))
        return rows

    def _region_value(self) -> str:
        tz = self._stored_tz()
        return providers.region_for_timezone(tz) or (tz or self._region_rows()[0][1])

    def _stored_tz(self) -> str:
        ntp = self._existing(self.current, "ntp") or {}
        return str(ntp.get("timezone") or "") if isinstance(ntp, dict) else ""

    def _stored_ssid(self) -> str:
        networks = self._existing(self.current, "wifi.networks") or []
        first = networks[0] if isinstance(networks, list) and networks else {}
        return str(first.get("ssid") or "") if isinstance(first, dict) else ""

    def can_pass(self) -> bool:
        """A stored network and a stored zone: both halves of this step are answered."""
        return bool(self._stored_ssid() and self._stored_tz())

    def pass_note(self) -> str:
        """Name the half that is missing, not the step.

        "No stored network" on a device whose Wi-Fi is configured and whose zone is
        not is a lie the reader has to disprove by opening the settings screen.
        """
        missing = [
            label for label, value in
            (("network", self._stored_ssid()), ("zone", self._stored_tz()))
            if not value
        ]
        if not missing:
            return "network and zone stored; Ctrl+G keeps both"
        if len(missing) == 2:
            return "nothing stored; Ctrl+G leaves this step unconfigured"
        return f"no stored {missing[0]}; Ctrl+G leaves it unconfigured"

    def answer(self) -> onboard.Answers:
        typed = self.query_one("#step-ssid", Input).value.strip()
        chosen = self.query_one("#step-ssid-select", Select).value
        ssid = typed or (chosen if isinstance(chosen, str) else "")
        auth = next(
            (str(n.get("auth") or "") for n in self.scan if n.get("ssid") == ssid), ""
        )
        region = self.query_one("#step-region", Select).value
        if not isinstance(region, str) or not region:
            region = providers.region_names()[0]
        timezone, server = (
            providers.timezone_for(region)
            if region in providers.region_names()
            else (region, "")
        )
        return onboard.Answers(
            ssid=ssid,
            password=self.query_one("#step-password", Input).value.strip(),
            base_url="",
            api_key="",
            model="",
            auth=auth,
            timezone=timezone,
            ntp_server=server,
            skip_network=self.passed,
        )


class ProviderStep(_StepBox):
    """`05` §7 step 2: the catalog row, the base URL and the key, then the model list
    that key unlocks — fetched bridge-side, so the provider is proven before a model is
    named from its answer."""

    AUTO_FOCUS = "#step-api-key"
    TITLE = "Provider"
    PATH = "writes nothing yet: the list is the test, and the entry lands next step"
    PROVES = "the provider's model list, fetched with this key"

    def __init__(self, current: dict[str, Any], providers_: list[Any] | None = None) -> None:
        super().__init__()
        self.current = current
        self.providers = providers_ or []

    def _provider_rows(self, current: str) -> list[tuple[str, str]]:
        """The catalog with its labels, plus the configured id when it is not one."""
        rows = [(entry.label, entry.id) for entry in self.providers]
        if current and current not in {entry.id for entry in self.providers}:
            rows.insert(0, (current, current))
        return rows or [(current or "", current or "")]

    def fields(self) -> Any:
        rows = self._existing(self.current, "llm.providers") or []
        first = rows[0] if isinstance(rows, list) and rows else {}
        provider = first if isinstance(first, dict) else {}
        chosen = str(provider.get("id") or "")
        return [
            Static("provider (fills the base url)", classes="step-label"),
            self._select(self._provider_rows(chosen), chosen, "step-provider",
                         allow_blank=True),
            Static("base url", classes="step-label"),
            Input(
                str(provider.get("base_url") or ""),
                placeholder="https://api.openai.com/v1",
                id="step-base-url",
            ),
            Static("api key (the nvs secret llm_api_key)", classes="step-label"),
            Input(password=True, id="step-api-key"),
        ]

    def _stored(self) -> dict[str, Any]:
        rows = self._existing(self.current, "llm.providers") or []
        first = rows[0] if isinstance(rows, list) and rows else {}
        return first if isinstance(first, dict) else {}

    def can_pass(self) -> bool:
        """A stored provider with a base url is usable as it stands."""
        stored = self._stored()
        return bool(stored.get("base_url") and stored.get("model"))

    def pass_note(self) -> str:
        stored = self._stored()
        if self.can_pass():
            return f"{stored.get('id') or 'provider'} stored; Ctrl+G keeps it"
        if stored.get("base_url"):
            return "no stored model; Ctrl+G keeps the provider unconfigured"
        return "nothing stored; Ctrl+G leaves this step unconfigured"

    def on_select_changed(self, event: Select.Changed) -> None:
        """A provider choice fills the base url, so the reader does not retype it."""
        if event.select.id != "step-provider":
            return
        entry = next(
            (p for p in self.providers if p.id == event.value), None
        )
        if entry is not None and entry.base_url:
            self.query_one("#step-base-url", Input).value = entry.base_url

    def answer(self) -> onboard.Answers:
        chosen = self.query_one("#step-provider", Select).value
        provider_id = chosen if isinstance(chosen, str) else ""
        entry = next((p for p in self.providers if p.id == provider_id), None)
        base_url = self.query_one("#step-base-url", Input).value.strip()
        if not base_url and entry is not None:
            base_url = entry.base_url
        name = onboard.provider_id(base_url, entry)
        return onboard.Answers(
            ssid="",
            password="",
            base_url=base_url,
            api_key=self.query_one("#step-api-key", Input).value.strip(),
            model="",
            provider=name,
            token_param=providers.TOKEN_PARAM.get(name, ""),
            skip_provider=self.passed,
        )


class ModelStep(_StepBox):
    """`05` §7 step 3: the model, chosen from the list step 2 proved, with the
    thinking level where the provider accepts the member.

    The list is offered, not trusted: `writes_provider` needs a model, and this step
    is where the reader picks one the provider actually published."""

    AUTO_FOCUS = "#step-model"
    TITLE = "Model"
    PATH = "writes llm.providers[0] and the nvs secret llm_api_key"
    PROVES = "the chosen name is one the provider listed"

    def __init__(
        self,
        models: list[str] | None = None,
        provider: str = "",
        model: str = "",
        thinking: str = "none",
        entries: list[Any] | None = None,
    ) -> None:
        super().__init__()
        self.models = models or []
        self.provider = provider
        self.model = model
        self.thinking = thinking
        # The catalog the provider step was built from, so a custom entry's levels
        # are offered too: reading `BUILTIN` here made a host-file provider look like
        # one with no thinking member.
        self.entries = entries if entries is not None else [p for p in providers.BUILTIN]

    def fields(self) -> Any:
        levels = _thinking_levels(self.entries, self.provider)
        yield_this = [
            Static(
                f"{len(self.models)} models from {self.provider}"
                if self.models
                else "no list; type the model the provider publishes",
                classes="step-label",
            ),
            self._select(
                self._options(self.models, self.model),
                self.model or (self.models[0] if self.models else ""),
                "step-model-select",
                allow_blank=True,
            ),
            Input(self.model, placeholder="or type a model", id="step-model"),
        ]
        if levels:
            yield_this.append(
                Static("thinking (only where the provider accepts the member)",
                       classes="step-label")
            )
            yield_this.append(
                Select(
                    [(level, level) for level in levels],
                    value=self.thinking if self.thinking in levels else "none",
                    allow_blank=False,
                    id="step-thinking",
                )
            )
        return yield_this

    def answer(self) -> dict[str, Any]:
        typed = self.query_one("#step-model", Input).value.strip()
        chosen = self.query_one("#step-model-select", Select).value
        model = typed or (chosen if isinstance(chosen, str) else "")
        level_widget = self.query_one("#step-thinking", Select) if self.query(
            "#step-thinking"
        ) else None
        level = level_widget.value if level_widget is not None else "none"
        return {
            "model": model,
            "thinking": level if isinstance(level, str) else "none",
            "skip_model": self.passed,
        }

    def can_pass(self) -> bool:
        """A stored model is already a written provider; nothing left to choose."""
        return bool(self.model)

    def pass_note(self) -> str:
        if self.can_pass():
            return f"{self.model} stored; Ctrl+G keeps it"
        return "no stored model; Ctrl+G leaves the provider unconfigured"


class FilePrompt(PromptBox[str | None]):
    """Text editor for one sandbox file (`05` §5.8).

    `Ctrl+S` commits the whole buffer and `Esc` discards it, so a modal left open
    by mistake cannot save by itself.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("ctrl+f", "find", "Find", show=False),
        Binding("f3", "find_next", "Next", show=False),
        Binding("ctrl+z", "undo", "Undo", show=False),
        Binding("ctrl+y", "redo", "Redo", show=False),
    ]
    CSS = (
        _CSS
        + """
.prompt-box.file-box {
    width: 90%;
    max-width: 160;
    height: 80%;
}
#file-find { display: none; height: 3; }
#file-find.active { display: block; }
"""
    )
    AUTO_FOCUS = "#file-body"

    HINT = (
        "Ctrl+S: save | Ctrl+F: find/replace | F3: next | "
        "Ctrl+Z/Ctrl+Y: undo/redo | Esc: discard"
    )

    def __init__(self, path: str, text: str) -> None:
        super().__init__()
        self.path = path
        self.text = text
        #: The find bar is a two-step prompt: `finding` asks for the needle,
        #: `replacing` asks what to put there. A dict avoids a second modal and
        #: keeps the caret in the buffer while the query is typed.
        self._query = ""
        self._finding = False
        self._replacing = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box file-box"):
            yield Static(self.path, classes="prompt-title")
            area = TextArea(self.text, id="file-body")
            area.show_line_numbers = False
            yield area
            yield Input(placeholder="find...", id="file-find")
            yield Static(self.HINT, classes="prompt-hint")

    # -- find and replace -------------------------------------------------
    #
    # `TextArea.replace` takes an insertion string, not a callable, so a global
    # replace builds the joined result and applies it as one edit. That also
    # makes the whole cascade a single undo step, which is what a reader
    # expects from "replace all".

    def _area(self) -> TextArea:
        return self.query_one("#file-body", TextArea)

    def _bar(self) -> Input:
        return self.query_one("#file-find", Input)

    def action_find(self) -> None:
        self._finding = True
        self._replacing = False
        bar = self._bar()
        bar.placeholder = "find..."
        bar.add_class("active")
        bar.focus()

    def action_find_next(self) -> None:
        area = self._area()
        if not self._query:
            self.action_find()
            return
        text = area.text
        row, column = area.selection.end
        start = _point_to_offset(text, row, column)
        index = text.find(self._query, start)
        if index < 0:
            # Wrap: a search that stops at the end of the buffer reads as
            # "not found" even when the match is above the caret.
            index = text.find(self._query)
        if index < 0:
            return
        row, column = _offset_to_point(text, index)
        end_row, end_column = _offset_to_point(text, index + len(self._query))
        area.move_cursor((row, column))
        area.selection = Selection((row, column), (end_row, end_column))

    def action_undo(self) -> None:
        self._area().undo()

    def action_redo(self) -> None:
        self._area().redo()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "file-find":
            return
        event.stop()
        if self._replacing:
            needle, replacement = self._query, event.value
            if needle:
                self._replace_all(needle, replacement)
            self._close_find()
            return
        self._query = event.value
        self._finding = False
        self._replacing = True
        self._bar().value = ""
        self._bar().placeholder = f"replace {self._query!r} with..."
        if self._query:
            self.action_find_next()

    def on_key(self, event: Any) -> None:
        if event.key == "escape" and self._bar().has_class("active"):
            event.stop()
            self._close_find()

    def _close_find(self) -> None:
        self._finding = False
        self._replacing = False
        bar = self._bar()
        bar.remove_class("active")
        bar.value = ""
        self._area().focus()

    def _replace_all(self, needle: str, replacement: str) -> None:
        area = self._area()
        text = area.text
        if needle not in text:
            return
        end_row = text.count("\n")
        end_column = len(text.rsplit("\n", 1)[-1])
        area.replace(
            text.replace(needle, replacement), (0, 0), (end_row, end_column)
        )

    def action_save(self) -> None:
        # The body is the return value; `Esc` reaching `PromptBox`'s cancel
        # yields `None`, so a discarded edit is distinguishable from saving.
        self.dismiss(self.query_one("#file-body", TextArea).text)


class Working(PromptBox[None]):
    """What the reader watches while a step's test runs.

    Each step's proof is a round trip the device answers on its own schedule: a
    `reload` waits on `net_connect`, a model fetch waits on an HTTPS call. Without a
    line saying so, a wizard that is working looks exactly like a wizard that has
    hung, which is what made a slow-but-correct first step read as a failure.

    Not cancellable: the request is already in flight and the step that asked for it
    owns the result. `Esc` is bound to nothing on purpose — `PromptBox`'s cancel would
    dismiss this screen while its caller was still awaiting the device, and the two
    would then disagree about which screen the reader is on.
    """

    BINDINGS: list[Binding] = []
    CSS = _CSS + """
.prompt-box.working-box {
    width: 60;
    max-width: 100%;
}
.working-action {
    text-style: bold;
    color: $accent;
}
"""

    def __init__(self, action: str, detail: str) -> None:
        super().__init__()
        self.action_text = action
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box working-box"):
            yield Static(self.action_text, classes="working-action", id="working-action")
            yield LoadingIndicator(id="working-spinner")
            yield Static(self.detail, classes="prompt-hint", id="working-detail")
            yield Static("this cannot be cancelled; the device is answering",
                         classes="prompt-hint")


class HelpModal(ModalScreen[None]):
    """`05` §5.6: the command catalogue, one tab per group.

    A modal rather than a screen because help is consulted *while* composing: a
    screen swap loses the half-typed prompt, and `F1` is pressed mid-sentence far
    more often than it is pressed to browse.
    """

    CSS = _CSS + """
.help-box {
    width: 90%;
    height: 80%;
    border: thick $accent;
    padding: 0 1;
}
#help-tabs { height: 1fr; }
"""
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def __init__(self, categories: dict[str, str]) -> None:
        super().__init__()
        self.categories = categories

    def compose(self) -> ComposeResult:
        with Vertical(classes="help-box"):
            yield Static("Commands", classes="prompt-title")
            with TabbedContent(id="help-tabs"):
                for name, text in self.categories.items():
                    with TabPane(name, id=f"help-tab-{name.lower()}"):
                        yield Static(text, classes="help-body")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        # The first tab is the device catalogue, which is what a reader pressing
        # F1 is usually after.
        first = next(iter(self.categories))
        self.query_one("#help-tabs", TabbedContent).active = f"help-tab-{first.lower()}"
