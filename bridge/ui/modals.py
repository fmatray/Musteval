"""Escapable modal prompts: secrets, confirmation, file editing and help."""

from __future__ import annotations

import copy
from typing import Any, TypeVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import (
    Input,
    OptionList,
    LoadingIndicator,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection

from .. import providers
from ..commands import CommandError, todo_edit_tree, todo_init
from ..protocol import Frame
from . import onboard
from .render import _TODO_MARKS, sanitize

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

/* `17` §4: every modal keeps the same gutter from the terminal edge — one row above
   and below, two columns left and right — and the rest is centred. The gutter is a
   margin on the box, not a padding on the screen: Textual has no `calc()`, so a box
   that wants `terminal - 4` cannot say so, but `max-width: 100%` inside a margin
   resolves to exactly that. Before this line a box declared `width: 60` touched both
   edges of a 60-column terminal, and `width: 84` touched both at 80. */
.prompt-box, .pick-box, .info-box, .help-box {
    margin: 1 2;
    /* `17` §4: a box hugs its content on both axes. Height is the one that was
       wrong: `info-box` asked for 70% of the screen and `help-box` for 80%, so a
       diagnostic with eight lines filled a 46-row box at 170x68 and the reader read
       two thirds of a screen of border. `max-height` is the ceiling, not the size. */
    height: auto;
    max-height: 100%;
    border: thick $accent;
    padding: 1 2;
}

/* `17` §4: width follows the content, bounded by the terminal, so a one-line
   prompt is not stretched and the editor is not pinned narrower than its text. */
.prompt-box {
    width: auto;
    min-width: 40;
    max-width: 100%;
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
            yield Static(f"secret: {self.key}", classes="prompt-title", markup=False)
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
            yield Static(self.question, classes="prompt-title", markup=False)
            yield Static("y: confirm | n: cancel", classes="prompt-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class FilterPrompt(PromptBox[str | None]):
    """One narrowing line: a log filter, or a file search (`05` §5.4, `17` §5).

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
            yield Static(self.title, classes="prompt-title", markup=False)
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
            yield Static(self.path, classes="prompt-title", markup=False)
            yield Input(value=self.current, id="value-input")
            yield Static("Enter: apply | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # An emptied field is a cancel, not a request to blank the setting.
        self.dismiss(self.query_one("#value-input", Input).value.strip() or None)

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#value-input", Input).value.strip() or None)


def _thinking_levels(
    entries: list[Any], provider_id: str, stored: str = "", model: str = "",
    efforts: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """The levels to offer for `provider_id`, or none.

    `05` §7.1: a provider whose `extra_body` takes no level gets no picker, because a
    level written where the member is rejected turns every call into a 400. Where the
    provider published per-model efforts for `model`, that set wins: it is the only
    statement about the model actually chosen.
    """
    entry = next((p for p in entries if p.id == provider_id), None)
    if entry is None:
        return []
    published = (efforts or {}).get(model) if model else None
    levels = list(published) if published else list(entry.thinking_levels)
    # A level the row does not list but can express is still the reader's setting:
    # Ollama names its values per model, so `max` on a `glm` model is real and the
    # re-run must show it, not silently show `low` (`17` §6). `none` is not such a
    # setting here — it is what `level_of` reports for an absent member, and it is in
    # the list already wherever the row can express an off switch.
    if stored and stored != "none" and stored not in levels and entry.extra_body(stored):
        levels.append(stored)
    return levels


class _StepBox(PromptBox[Any]):
    """Shared chrome for the three `/onboard` steps.

    `17` §6: a step asks only what its own test can check, shows the path it
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
        # The box is the scroll view, and every child is inside it. A
        # `max-height: 100%` scroll body beside a title, a path, a summary and two
        # hint rows resolved its cap against the whole content area, so on a 20-row
        # terminal the last hint was painted one row below the border. As the root,
        # the box's content area is the scroll view's own height and nothing overhangs.
        with VerticalScroll(classes="prompt-box step-box"):
            yield Static(f"First run — {self.TITLE}", classes="step-title")
            yield Static(self.PATH, classes="step-note", markup=False)
            for widget in self.fields():
                yield widget
            yield Static(self.SUMMARY, classes="step-note", markup=False)
            yield Static(
                self.pass_note(), id="step-pass", classes="step-note", markup=False
            )
            yield Static("", id="step-verdict", classes="step-note", markup=False)

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

        `app.py` keeps the config flattened (`config.flatten`), so a nested read
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
        # `allow_blank=False` above, so the select always holds one of its rows: a
        # region name, or a stored zone the table lacks, kept as its own row.
        region = str(self.query_one("#step-region", Select).value)
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
        efforts: dict[str, tuple[str, ...]] | None = None,
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
        #: Per-model efforts the provider published, keyed by model name. Empty for
        #: every provider whose list says nothing, which is all but OpenRouter.
        self.efforts = efforts or {}

    def fields(self) -> Any:
        levels = _thinking_levels(
            self.entries, self.provider, self.thinking, self.model, self.efforts
        )
        yield_this = [
            Static(
                f"{len(self.models)} models from {self.provider}"
                if self.models
                else "no list; type the model the provider publishes",
                classes="step-label",
                markup=False,
            ),
            # Sorted by name, like `/model`'s list (`05` §7.1): the provider's
            # publication order is not a reading order. `_options` is shared with the
            # SSID list, which is strongest-first and must keep that order, so the
            # sort is here rather than inside it.
            self._select(
                self._options(sorted(self.models), self.model),
                self.model or (sorted(self.models)[0] if self.models else ""),
                "step-model-select",
                allow_blank=True,
            ),
            # The list can run to hundreds of names (`05` §7.1), so the field
            # completes from it instead of making the reader scroll a menu or retype
            # a name it already holds. A completion, not a filter: a model published
            # after the fetch is still submittable.
            Input(
                self.model,
                placeholder="or type a model",
                suggester=SuggestFromList(self.models, case_sensitive=False)
                if self.models
                else None,
                id="step-model",
            ),
        ]
        if levels:
            yield_this.append(
                Static("thinking (only where the provider accepts the member)",
                       classes="step-label")
            )
            yield_this.append(
                Select(
                    [(level, level) for level in levels],
                    # `none` is not a level every provider can express: Ollama's
                    # surface has no off switch, so its rows offer `low`/`high` only
                    # (`05` §7.2). A stored member this provider cannot map therefore
                    # seeds nothing, and the first row is shown — the level already
                    # on the device is left alone unless the reader changes it.
                    value=self.thinking if self.thinking in levels else levels[0],
                    allow_blank=False,
                    id="step-thinking",
                )
            )
        return yield_this

    def on_select_changed(self, event: Select.Changed) -> None:
        """Narrow the levels to the chosen model when the provider publishes them.

        `05` §7.1: levels are a property of the model, not just the provider. A
        reader who picks a model with no `xhigh` must not be offered one — the
        provider would answer 400 on the first prompt.
        """
        if event.select.id != "step-model-select" or not isinstance(event.value, str):
            return
        self._narrow(event.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        """The same narrowing for a name typed rather than picked.

        A name in the list is a choice whichever field carried it, and `answer()`
        prefers the typed one — so leaving the picker at the previous model's set
        would offer a level the model being written may not take.
        """
        if event.input.id != "step-model" or event.value not in self.efforts:
            return
        self._narrow(event.value)

    def _narrow(self, model: str) -> None:
        if not self.query("#step-thinking"):
            return
        levels = _thinking_levels(
            self.entries, self.provider, self.thinking, model, self.efforts
        )
        widget = self.query_one("#step-thinking", Select)
        current = widget.value if isinstance(widget.value, str) else ""
        widget.set_options((level, level) for level in levels)
        widget.value = current if current in levels else levels[0]

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


class ModelPicker(PromptBox[str | None]):
    """`05` §7.3 `/model`: the provider's published list on one tab, a typed name on
    the other.

    Two tabs, not a field beside a list. With both mounted the field was pre-seeded
    from the stored name and the answer was `typed or chosen`, so the field always
    won and the list could never be used. A tab holds exactly one answer, so Enter
    takes what the reader is looking at and nothing else.

    The list is offered, not trusted. The fetch runs without the API key — `06` §A1
    keeps it in the device's NVS — so a provider that requires auth on its model
    endpoint answers nothing and the typing tab is the whole picker.
    """

    BINDINGS = [
        Binding("tab", "cycle_tab", "Next tab", show=False),
        Binding("shift+tab", "cycle_tab", "Previous tab", show=False),
    ]
    CSS = (
        _CSS
        + """
/* `17` §4: content-sized, and capped at what the smallest supported terminal
   affords. Two hint lines, a two-row tab bar and a twelve-row list came to 17 rows
   inside a 14-row content area at 60x20, so the modal's own hint was drawn below the
   border; nine rows of list fit. The list is an `OptionList` and scrolls, so the cap
   costs no reach. `nowrap` on the hint keeps it one row: wrapped at 34 columns it
   took two. */
#step-model-tabs { height: auto; max-height: 100%; }
#step-model-list { height: auto; max-height: 9; }
.prompt-hint.one-line { text-wrap: nowrap; text-overflow: ellipsis; }
"""
    )

    def __init__(
        self, provider: str, label: str, models: list[str], current: str
    ) -> None:
        super().__init__()
        self.provider = provider
        self.label = label or provider
        self.models = models
        self.current = current
        # Focusing a widget inside a hidden `TabPane` makes `TabbedContent` switch to
        # that pane, so the auto-focus target is the widget on the tab the picker
        # opens on. Left at `#step-model` it silently moved the reader off the list.
        self.AUTO_FOCUS = "#step-model-list" if models else "#step-model"

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(f"model — {self.label}", classes="prompt-title", markup=False)
            if self.models:
                yield Static(
                    f"{len(self.models)} models from {self.label}",
                    classes="prompt-hint",
                    markup=False,
                )
            else:
                # The reason is the caller's line, in chat: this names the state.
                yield Static(
                    "no list from this provider; type the model name",
                    classes="prompt-hint",
                )
            with TabbedContent(
                initial="list" if self.models else "type", id="step-model-tabs"
            ):
                if self.models:
                    with TabPane("list", id="list"):
                        yield OptionList(
                            *[Option(name, id=name) for name in self._names()],
                            id="step-model-list",
                        )
                with TabPane("type", id="type"):
                    yield Input(
                        self.current,
                        placeholder="type a model name",
                        suggester=SuggestFromList(self.models, case_sensitive=False)
                        if self.models
                        else None,
                        id="step-model",
                    )
            yield Static(
                "Tab: switch | Enter: use | Esc: cancel",
                classes="prompt-hint one-line",
            )

    def _names(self) -> list[str]:
        """The provider's list, sorted by name, with the stored name in it.

        Sorted because a provider's publication order is its own business — Ollama's
        is roughly newest-first, OpenRouter's mixes families — and a reader looking
        for `gpt-oss:120b` scans a roll call, not a ranking (`05` §7.1).

        A stored name the fetch did not return — `gemma4:cloud` against the cloud
        list, which is the state of the first device this ran on — is added, and the
        cursor goes to it: Enter on an unvisited list must not adopt a model the
        reader never chose.
        """
        names = sorted(set(self.models) | ({self.current} if self.current else set()))
        return names

    def on_mount(self) -> None:
        # Sorting moved the stored name off row 0, and the highlight defaults to the
        # first row: without this, Enter on the list tab adopts whatever sorts first
        # — a model nobody chose, written silently to `llm.providers[0].model`.
        # `_names()` unions the stored name in, so it is always present here.
        if self.query("#step-model-list") and self.current:
            names = self._names()
            self.query_one("#step-model-list").highlighted = names.index(self.current)

    def _active(self) -> str:
        return self.query_one("#step-model-tabs", TabbedContent).active

    def action_cycle_tab(self) -> None:
        if not self.models:
            return  # one pane: nothing to switch to
        tabs = self.query_one("#step-model-tabs", TabbedContent)
        tabs.active = "type" if tabs.active == "list" else "list"
        self.query_one(
            "#step-model" if tabs.active == "type" else "#step-model-list"
        ).focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        self.dismiss(str(event.option.id) if event.option.id is not None else None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        if self._active() == "list":
            option = self.query_one("#step-model-list", OptionList).highlighted_option
            self.dismiss(str(option.id) if option is not None else None)
            return
        self.dismiss(self.query_one("#step-model", Input).value.strip() or None)


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
    max-height: 100%;
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
        #: The find bar is a two-step prompt: the first Enter takes the needle, the
        #: second what to put there. `_replacing` says which Enter this is, so no
        #: second modal is needed and the caret stays in the buffer.
        self._query = ""
        self._replacing = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box file-box"):
            yield Static(self.path, classes="prompt-title", markup=False)
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
/* `17` §4: `LoadingIndicator` claims four rows and paints one. In an `auto` box the
   claim is what counts, so the spinner's three empty rows pushed the detail and the
   cancel note past the border at every size. It is one line of braille, so it is
   given one row. */
#working-spinner {
    height: 1;
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
            yield Static(self.action_text, classes="working-action", id="working-action",
                         markup=False)
            yield LoadingIndicator(id="working-spinner")
            yield Static(
                self.detail, classes="prompt-hint", id="working-detail", markup=False
            )
            yield Static("this cannot be cancelled; the device is answering",
                         classes="prompt-hint")


class InfoModal(ModalScreen[None]):
    """`05` §5.10: `/sys status` and `/sys diag` in a scrollable box.

    One modal for both verbs, because they are two halves of one `status` frame: a
    reader comparing heap against the LLM's remaining quota should not have to close
    one box and open another. The section a verb asks for is the one that scrolls
    into view; the other stays there.

    A modal rather than a screen: the composer keeps its half-typed prompt, which is
    the `help` rule (`05` §5.6) and applies more strongly here, since `/sys status`
    is a glance rather than a place to work.
    """

    CSS = _CSS + """
.info-box {
    /* `17` §4: a readable measure, not a screen fraction. `80%` alone is 136
       columns of prose on a 170-column terminal, which the eye loses its place in;
       a flat `100` is wider than a 60-column terminal allows. A percentage resolves
       inside the `1 2` margin, so it is the half that keeps the gutter, and the
       column cap is the half that keeps the measure. */
    width: 80%;
    max-width: 100;
}
.info-heading { text-style: bold; }
"""
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def __init__(self, title: str, sections: "list[tuple[str, list[str]]]") -> None:
        super().__init__()
        self.title_text = title
        self.sections = sections

    def compose(self) -> ComposeResult:
        # The heading lives inside the scroll view, not beside it. A `max-height: 100%`
        # sibling resolves against the whole content area, so the heading's row was
        # spent twice and the body's last row landed on the box's own padding — the
        # overflow test caught it as one row painted past the border. Inside, the
        # content area is the scroll view's own height and nothing overhangs.
        with VerticalScroll(classes="info-box", id="info-body"):
            yield Static(self.title_text, classes="prompt-title")
            for index, (heading, lines) in enumerate(self.sections):
                if index:
                    yield Static("")
                if heading:
                    yield Static(heading, classes="info-heading")
                # Parsed as markup, like `StatusScreen._paint`: `render_status`
                # escapes every device-supplied value before splicing it, and the
                # verdict colours are half of what the grid says.
                yield Static(Text.from_markup("\n".join(lines)))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpModal(ModalScreen[None]):
    """`05` §5.6: the command catalogue, one tab per group.

    A modal rather than a screen because help is consulted *while* composing: a
    screen swap loses the half-typed prompt, and `F1` is pressed mid-sentence far
    more often than it is pressed to browse.
    """

    CSS = _CSS + """
.help-box {
    /* `17` §4: same measure as `.info-box`. Help is a two-column list of command
       and description, so it takes the wider of the two caps. */
    width: 90%;
    max-width: 104;
}
#help-tabs { height: auto; max-height: 100%; }
#help-tabs > ContentSwitcher { height: auto; max-height: 100%; }
#help-tabs TabPane { height: auto; max-height: 100%; }
#help-tabs TabPane > VerticalScroll { width: auto; height: auto; max-height: 100%; }
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
                        # `17` §4: `TabPane` is a plain `Widget`, not a scroll view,
                        # so a body taller than the box was drawn past the border and
                        # off the screen instead of scrolling inside it.
                        with VerticalScroll():
                            yield Static(text, classes="help-body")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        # The first tab is the device catalogue, which is what a reader pressing
        # F1 is usually after.
        first = next(iter(self.categories))
        self.query_one("#help-tabs", TabbedContent).active = f"help-tab-{first.lower()}"

class TodoEditor(PromptBox[list[Frame] | None]):
    """`05` §6.2 `/todo edit`: the whole tree, edited in place and sent as one `init`.

    The device has no op that moves, renames or inserts at a position, so every
    verb here rewrites a local copy and `bridge.commands.todo_edit_tree` turns it into a
    single `init` carrying each task's own status. That is why the editor must hold
    the real tree, not just its names: a rewrite built from names would reset every
    status to `pending`.
    """

    BINDINGS = [
        Binding("ctrl+e", "modify", "Modify", show=False),
        Binding("ctrl+n", "add_after", "Add after", show=False),
        Binding("ctrl+b", "add_before", "Add before", show=False),
        Binding("ctrl+d", "drop", "Drop", show=False),
        Binding("ctrl+up", "move_up", "Move up", show=False),
        Binding("ctrl+down", "move_down", "Move down", show=False),
        Binding("ctrl+s", "send", "Send", show=False),
    ]
    CSS = _CSS + """
/* `height: auto` makes the box hug the tree: a `1fr` child in an auto-height
   container expands to the screen, which is what made the modal full height.
   `max-height: 100%` keeps the box on screen at 12 rows, where the chrome plus
   five tree rows would otherwise overflow. */
TodoEditor .prompt-box {
    max-height: 100%;
}

#todo-tree {
    height: auto;
    /* Five rows: a phase header plus four tasks, the rest scrolls. The tree is
       the only variable part, so its cap is what sets the modal's height. */
    max-height: 5;
    min-height: 1;
    border: none;
}
"""
    AUTO_FOCUS = "#todo-tree"

    def __init__(self, phases: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        #: The working copy. The caller's tree is never touched: this is the only
        #: thing the editor mutates, and only through `bridge.commands.todo_edit`.
        self.phases: list[dict[str, Any]] = copy.deepcopy(phases or [])
        #: What the modal opened with, so `Ctrl+S` on an untouched tree sends nothing.
        self._opened = copy.deepcopy(self.phases)
        #: The verb awaiting its value in the input, `(op, phase, task, where)`.
        self._pending: tuple[str, str, str, str] | None = None
        #: Set by the first `Ctrl+D`, naming what a second one would drop.
        self._armed: str | None = None
        #: `(kind, phase, task)` per rendered row, in the order the rows were added.
        self._rows: list[tuple[str, str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static("todo edit", classes="prompt-title")
            yield OptionList(id="todo-tree")
            yield Input(placeholder="type the new text, then Enter", id="todo-value")
            yield Static("", id="todo-status", markup=False)
            # `.prompt-box` is 60 columns wide (`17` §4), so the legend is two
            # lines: one line would be clipped and drop the send key.
            yield Static(
                "Ctrl+E modify | Ctrl+N after | Ctrl+D drop\n"
                "Ctrl+B before | Ctrl+Up/Down move | Ctrl+S send",
                id="todo-hint",
                classes="prompt-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self._rebuild()
        self._say_hint()

    # -- selection ------------------------------------------------------

    def _selected(self) -> tuple[str, str, str] | None:
        """`(kind, phase, task)` of the highlighted row, or `None` for an empty tree."""
        index = self.query_one("#todo-tree", OptionList).highlighted
        if index is None or not 0 <= index < len(self._rows):
            return None
        return self._rows[index]

    def _select(self, pred) -> None:
        """Highlight the first row matching `pred`, keeping the cursor meaningful
        after an edit moved or removed the row it was on."""
        for index, row in enumerate(self._rows):
            if pred(row):
                self.query_one("#todo-tree", OptionList).highlighted = index
                return

    def _rebuild(self, keep: tuple[str, str, str] | None = None) -> None:
        tree = self.query_one("#todo-tree", OptionList)
        tree.clear_options()
        self._rows = []
        for phase in self.phases:
            name = str(phase.get("name") or "")
            self._rows.append(("phase", name, ""))
            tree.add_option(Option(sanitize(name), id=f"p:{len(self._rows) - 1}"))
            for task in phase.get("tasks") or []:
                content = str(task.get("content") or "")
                status = str(task.get("status") or "pending")
                mark = _TODO_MARKS.get(status, "[?]")
                self._rows.append(("task", name, content))
                tree.add_option(
                    Option(f"  {mark} {sanitize(content)}", id=f"t:{len(self._rows) - 1}")
                )
        if keep is not None:
            self._select(lambda row: row == keep)
        elif tree.highlighted is None and self._rows:
            tree.highlighted = 0

    # -- verbs ----------------------------------------------------------

    def _begin(self, op: str, where: str, seed: str) -> None:
        # An empty tree has no row to anchor on and the first phase needs none, so
        # no selection means a blank anchor rather than a refusal: refusing here left
        # a fresh list with no gesture that could seed it.
        _, phase, task = self._selected() or ("", "", "")
        field = self.query_one("#todo-value", Input)
        self._armed = None
        self._pending = (op, phase, task, where)
        field.value = seed
        field.focus()
        self._say_hint()

    def action_modify(self) -> None:
        selected = self._selected()
        if selected is None:
            self._say("select a phase or a task first")
            return
        kind, phase, task = selected
        op = "modify-phase" if kind == "phase" else "modify-task"
        self._begin(op, "", phase if kind == "phase" else task)

    def action_add_after(self) -> None:
        self._begin(self._add_op(), "after", "")

    def action_add_before(self) -> None:
        self._begin(self._add_op(), "before", "")

    def _add_op(self) -> str:
        """A phase row adds a phase; a task row adds a task to that task's phase.

        `_rebuild` always highlights a row when the tree has one, so no selection
        means an empty tree — where a phase is the only thing that can be added."""
        selected = self._selected()
        return "add-phase" if selected is None or selected[0] == "phase" else "add-task"

    def action_drop(self) -> None:
        selected = self._selected()
        if selected is None:
            self._say("select a phase or a task first")
            return
        kind, phase, task = selected
        target = phase if kind == "phase" else task
        if self._armed != target:
            self._armed = target
            self._say_hint()
            return
        self._armed = None
        self._apply("delete-phase" if kind == "phase" else "delete-task",
                    phase=phase if kind == "phase" else "", task=task)

    def action_move_up(self) -> None:
        self._move("up")

    def action_move_down(self) -> None:
        self._move("down")

    def _move(self, where: str) -> None:
        selected = self._selected()
        if selected is None:
            self._say("select a phase or a task first")
            return
        kind, phase, task = selected
        if kind == "phase":
            self._apply("move-phase", where=where, phase=phase, keep=("phase", phase, ""))
            return
        self._apply("move-task", where=where, task=task, keep=("task", phase, task))

    # -- applying -------------------------------------------------------

    def _apply(self, op: str, *, keep=None, **kwargs) -> bool:
        """Run one verb against the working tree; a refusal leaves it untouched."""
        try:
            # `todo_edit_tree` is the one owner of the rewrite, so the rows on
            # screen are what the frame will carry, by construction.
            self.phases = todo_edit_tree(self.phases, op=op, **kwargs)
        except CommandError as exc:
            self._say(sanitize(str(exc)))
            return False
        self._rebuild(keep)
        self._say_hint()
        return True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._pending is None:
            return
        op, phase, task, where = self._pending
        value = self.query_one("#todo-value", Input).value.strip()
        if op == "modify-phase":
            args = {"phase": phase, "value": value}
            keep = ("phase", value, "")
        elif op == "modify-task":
            args = {"task": task, "value": value}
            keep = ("task", phase, value)
        elif op == "add-phase":
            args = {"phase": phase, "value": value, "where": where}
            keep = ("phase", value, "")
        else:
            # The anchor is the row the verb began on: a task row inserts beside
            # it, a phase row appends to that phase.
            args = {"phase": phase, "task": task, "value": value, "where": where}
            keep = ("task", phase, value)
        # A refusal leaves its message on screen: this is the only path that does
        # not re-render the hint, and the reader needs the bound it names.
        if self._apply(op, keep=keep, **args):
            self._say_hint()
        self._pending = None
        self.query_one("#todo-value", Input).value = ""
        self.query_one("#todo-tree", OptionList).focus()

    def action_cancel(self) -> None:
        # Esc unwinds one step at a time, then leaves: a modal that quits on the
        # first Esc while a verb is open would throw the edit away by accident.
        if self._pending is not None:
            self._pending = None
            self._armed = None
            field = self.query_one("#todo-value", Input)
            field.value = ""
            self.query_one("#todo-tree", OptionList).focus()
            self._say_hint()
            return
        if self._armed is not None:
            self._armed = None
            self._say_hint()
            return
        self.dismiss(None)

    def action_send(self) -> None:
        if self._pending is not None:
            self._say("finish the pending edit first, or Esc to drop it")
            return
        if self.phases == self._opened:
            self._say("nothing to send; the tree is unchanged")
            return
        self.dismiss(todo_init(self.phases))

    # -- lines ----------------------------------------------------------

    def _say(self, text: str) -> None:
        # Hidden rather than emptied: an empty `Static` still costs a row, and
        # the common case is nothing to report.
        status = self.query_one("#todo-status", Static)
        status.update(text)
        status.display = bool(text)

    def _count(self, phase: str) -> int:
        for entry in self.phases:
            if str(entry.get("name") or "") == phase:
                return len(entry.get("tasks") or [])
        return 0

    def _say_hint(self) -> None:
        if self._armed is not None:
            kind, phase, task = self._selected() or ("task", "", self._armed)
            if kind == "phase":
                count = self._count(self._armed)
                self._say(
                    f"Ctrl+D again drops phase {sanitize(self._armed)} "
                    f"and its {count} task(s)"
                )
            else:
                self._say(f"Ctrl+D again drops {sanitize(self._armed)}")
            return
        if self._pending is not None:
            self._say(f"{self._pending[0]}: Enter applies, Esc steps back")
            return
        # Nothing transient to report: the legend lives in `#todo-hint`, so this
        # line clears rather than restating it.
        self._say("")


# --- `/wifi edit` and `/providers` ------------------------------------------
# Both editors are the same gesture: pick a row, then either fill a small form or
# answer one question about it. `05` §7.6 and §7.7 own the contracts; the modals own
# only the picking, and the app owns the frames.


class _PickBox(PromptBox[str | None]):
    """A keyboard-scrollable list of actions, answering the chosen label.

    Deliberately not a `Select`: a `Select` opens on its own Enter handling and cannot
    carry a per-row hint, and both editors need to say what each row will do before it
    is chosen.
    """

    BINDINGS = [
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("enter", "choose", "Choose"),
    ]
    CSS = (
        _CSS
        + """
.pick-box {
    width: 76;
    max-width: 100%;
    height: auto;
    max-height: 100%;
    border: thick $accent;
    padding: 1 2;
}
.pick-row {
    color: $text;
}
.pick-row-active {
    color: $accent;
    text-style: bold;
}
.pick-hint {
    color: $text-muted;
}
"""
    )

    TITLE = ""

    def __init__(
        self,
        rows: list[tuple[str, str]],
        header: list[str] | None = None,
        title: str = "",
    ) -> None:
        super().__init__()
        #: `(label, what the row does)`. The label is the answer; the hint is never
        #: parsed, because a hint that is also a value is one rename away from being a
        #: value the app misreads.
        self.rows = rows
        #: What the reader is choosing between, drawn as the box's heading. `/wifi
        #: edit` asks twice — which network, then what to do with it — and the second
        #: question is unreadable without the first one's answer on screen.
        self.title_text = title
        #: What the list is a list *of*, drawn above it. `05` §7.7 needs a provider's
        #: model, url and key ref visible before one is chosen, and those are not the
        #: answer, so they cannot ride a row label.
        self.header = header or []
        self.index = 0

    def compose(self) -> ComposeResult:
        with Vertical(classes="pick-box"):
            yield Static(self.title_text or self.TITLE, classes="prompt-title",
                         markup=False)
            for line in self.header:
                yield Static(line, classes="pick-hint", markup=False)
            for i, (label, hint) in enumerate(self.rows):
                yield Static(
                    f"{label}  —  {hint}",
                    classes="pick-row-active" if i == 0 else "pick-row",
                    markup=False,
                    id=f"pick-{i}",
                )
            yield Static(
                "Up/Down: move | Enter: choose | Esc: cancel", classes="pick-hint"
            )

    def action_move(self, delta: int) -> None:
        if not self.rows:
            return
        self.index = max(0, min(len(self.rows) - 1, self.index + delta))
        for i in range(len(self.rows)):
            self.query_one(f"#pick-{i}", Static).set_classes(
                "pick-row-active" if i == self.index else "pick-row"
            )

    def action_choose(self) -> None:
        if self.rows:
            self.dismiss(self.rows[self.index][0])


class WifiScanForm(PromptBox[dict[str, Any] | None]):
    """`05` §7.6: pick a heard network, type its passphrase.

    The SSID is only ever the `Select`, which is the scan: a typed ssid here would be
    a second way to name a network, and the one the radio cannot hear is exactly the
    kind a typo invents. A hidden network is `WifiCustomForm`'s job, where the reader
    is told the ssid is theirs to spell.

    `Ctrl+R` dismisses with `{"refresh": True}` rather than rescanning in place: the
    form cannot reach the device, and a refresh that closed the list would hide the
    network that appeared while the reader was looking at it.
    """

    BINDINGS = [
        Binding("enter", "submit", "Apply", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]
    CSS = _CSS
    AUTO_FOCUS = "#wifi-pick"

    def __init__(
        self, title: str, scan: list[dict[str, Any]] | None = None, note: str = ""
    ) -> None:
        super().__init__()
        self.title = title
        self.scan = scan or []
        #: Why the list is short, drawn above it. A refused scan and a scan that
        #: heard only known networks are different answers, and the reader's next
        #: move — turn the radio on, or move closer — depends on which one it is.
        self.note = note

    def heard(self) -> list[str]:
        return [str(n.get("ssid") or "") for n in self.scan if n.get("ssid")]

    def compose(self) -> ComposeResult:
        heard = self.heard()
        with Vertical(classes="prompt-box"):
            yield Static(self.title, classes="prompt-title", markup=False)
            if self.note:
                yield Static(self.note, classes="prompt-hint", markup=False)
            if heard:
                yield Static("in range", classes="prompt-hint", markup=False)
                yield Select(
                    [(s, s) for s in heard],
                    value=heard[0],
                    allow_blank=False,
                    id="wifi-pick",
                )
            yield Static(
                "passphrase (stored as an nvs secret)", classes="prompt-hint",
                markup=False,
            )
            yield Input(password=True, id="wifi-password")
            yield Static(
                "Enter: apply | Ctrl+R: scan again | Esc: cancel",
                classes="prompt-hint",
            )

    def action_submit(self) -> None:
        selects = self.query("#wifi-pick")
        ssid = str(selects.first().value) if selects else ""
        if not ssid:
            # Only reachable with no picker, which means the scan heard nothing to
            # add: Enter cannot answer a network nobody chose, and the hint names
            # Ctrl+R, the gesture that helps. The picker is `allow_blank=False`, so a
            # present one always holds a value and there is nothing to refocus.
            return
        self.dismiss(
            {"ssid": ssid, "password": self.query_one("#wifi-password", Input).value}
        )

    def action_refresh(self) -> None:
        self.dismiss({"refresh": True})


class WifiCustomForm(PromptBox[dict[str, Any] | None]):
    """`05` §7.6: a network the scan cannot see, named and keyed by hand."""

    BINDINGS = [Binding("enter", "submit", "Apply", show=False)]
    CSS = _CSS
    AUTO_FOCUS = "#wifi-ssid"

    def __init__(self, title: str, ssid: str = "") -> None:
        super().__init__()
        self.title = title
        #: A known network's rekey: the ssid is already decided, so asking for it
        #: again would let a typo re-key the wrong entry.
        self.ssid = ssid

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(self.title, classes="prompt-title", markup=False)
            if self.ssid:
                yield Static(f"ssid: {self.ssid}", classes="prompt-hint", markup=False)
            else:
                yield Static("ssid (a hidden network the scan cannot report)",
                             classes="prompt-hint", markup=False)
                yield Input(placeholder="ssid", id="wifi-ssid")
            yield Static(
                "passphrase (stored as an nvs secret)", classes="prompt-hint",
                markup=False,
            )
            yield Input(password=True, id="wifi-password")
            yield Static(
                "Enter: apply | Esc: cancel | a blank passphrase changes nothing",
                classes="prompt-hint",
            )

    def action_submit(self) -> None:
        ssid = self.ssid
        if not ssid:
            ssid = self.query_one("#wifi-ssid", Input).value.strip()
            if not ssid:
                # An ssid-less entry would replace a working list with a network
                # the station cannot join.
                self.query_one("#wifi-ssid", Input).focus()
                return
        # The ssid is stripped and the passphrase is not: a space is part of a
        # passphrase and nobody means one at either end of an ssid.
        self.dismiss(
            {"ssid": ssid, "password": self.query_one("#wifi-password", Input).value}
        )
