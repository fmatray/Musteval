"""Escapable modal prompts: secrets, confirmation, file editing and help."""

from __future__ import annotations

from typing import Any, TypeVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static, TabbedContent, TabPane, TextArea
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


class OnboardPrompt(PromptBox["onboard.Answers | None"]):
    """`05` §7 and §13.6: the whole first-run form, grouped by domain.

    One form for all three domains, not one dialog per value: a reader sees everything
    `wifi.*` will become before committing any of it, can revise an earlier answer
    after reading a later one, and knows how much is left. `Ctrl+S` writes the whole
    form; `Esc` writes nothing.

    A dropdown where the values are known, a text field where they are not. Every
    dropdown keeps its manual route open, so a scan that answered `E_WIFI`, a model the
    provider does not publish, and a region the table lacks are all still reachable.
    """

    BINDINGS = [Binding("ctrl+s", "submit", "Write", show=False)]
    CSS = (
        _CSS
        + """
.prompt-box.onboard-box {
    width: 92;
    max-width: 100%;
    max-height: 92%;
}
.onboard-section {
    text-style: bold;
    color: $accent;
    margin-top: 1;
}
.onboard-label {
    color: $text-muted;
}
.onboard-note {
    color: $text-muted;
}
"""
    )
    AUTO_FOCUS = "#onboard-ssid"

    def __init__(
        self,
        current: dict[str, Any],
        scan: list[dict[str, Any]] | None = None,
        providers: list[Any] | None = None,
        models: list[str] | None = None,
        fetch_models: Any = None,
    ) -> None:
        super().__init__()
        self.current = current
        #: `fetch_models(provider, api_key) -> Awaitable[list[str]]`, supplied by
        #: `app.py`, which owns the thread. Never opened here: this module has widgets
        #: in it, and a blocking call in a widget freezes the form.
        self._fetch_models = fetch_models
        #: The `wifi.scan` result, strongest first. Empty when the scan refused, which
        #: leaves the manual SSID field as the only route rather than blocking.
        self.scan = scan or []
        #: The catalog (`05` §7.1). Built-ins alone when the custom file is broken.
        self.providers = providers or []
        #: The chosen provider's model names, or empty for a typed model.
        self.models = models or []
        #: The `(provider, key)` whose list is already in the dropdown.
        self._loaded: tuple[str, str] | None = None

    @staticmethod
    def _select(
        options: list[tuple[str, str]], value: str, widget_id: str, **kwargs: Any
    ) -> "Select":
        """A `Select` whose current value is real or absent.

        `Select.BLANK` is `False`, and the widget refuses a `False` value, so the
        value is passed only when there is one; `allow_blank` decides the rest.
        """
        if value:
            return Select(options, value=value, id=widget_id, **kwargs)
        return Select(options, id=widget_id, **kwargs)

    @staticmethod
    def _options(values: list[str], current: str) -> list[tuple[str, str]]:
        """`Select` rows, with the current value present even if the list lacks it."""
        rows = [(value, value) for value in values]
        if current and current not in values:
            rows.insert(0, (current, current))
        return rows

    def _provider_rows(self, current: str) -> list[tuple[str, str]]:
        """The catalog with its labels, plus the configured id when it is not one.

        A device set up over raw frames holds whatever id was written — `p1` on the
        board — and a `Select` whose value is absent from its options raises on mount,
        which is what made the wizard unusable on a device that already had a
        provider. The stored id becomes a row showing its base url, so the reader can
        see the provider they have and keep it without retyping, and the catalog rows
        keep their labels rather than their ids.
        """
        rows = [(entry.label, entry.id) for entry in self.providers]
        if current and current not in {entry.id for entry in self.providers}:
            rows.insert(0, (current, current))
        return rows or [(current or "", current or "")]

    def compose(self) -> ComposeResult:
        """The fields, pre-filled from whatever the device already reports.

        Pre-filling is what makes the form revisable: the previous answer is already
        there, so the reader edits instead of retyping blind.
        """
        networks = self._existing("wifi.networks") or []
        first = networks[0] if networks else {}
        provider_rows = self._existing("llm.providers") or []
        provider = provider_rows[0] if provider_rows else {}
        current_tz = str(self._existing("ntp.timezone") or "")

        ssids = [str(n.get("ssid") or "") for n in self.scan if n.get("ssid")]
        current_ssid = str(first.get("ssid") or "")
        chosen_provider = str(provider.get("id") or "")
        chosen_model = str(provider.get("model") or "")

        with Vertical(classes="prompt-box onboard-box"):
            yield Static("First run", classes="prompt-title")
            with VerticalScroll(id="onboard-body"):
                yield Static("Wi-Fi  →  wifi.networks[0]", classes="onboard-section")
                yield Static(
                    "ssid (from the scan; type one the scan cannot see)",
                    classes="onboard-label",
                )
                yield self._select(
                    self._options(ssids, current_ssid),
                    current_ssid or (ssids[0] if ssids else ""),
                    "onboard-ssid-select",
                    allow_blank=True,
                )
                yield Input(current_ssid, placeholder="or type an ssid", id="onboard-ssid")
                yield Static(
                    "password (the nvs secret wifi_psk; blank leaves it alone)",
                    classes="onboard-label",
                )
                yield Input(password=True, id="onboard-password")

                yield Static("LLM  →  llm.providers[0]", classes="onboard-section")
                yield Static("provider (fills the base url)", classes="onboard-label")
                yield self._select(
                    self._provider_rows(chosen_provider),
                    chosen_provider,
                    "onboard-provider",
                    allow_blank=True,
                )
                yield Static("base url", classes="onboard-label")
                yield Input(
                    str(provider.get("base_url") or ""),
                    placeholder="https://api.openai.com/v1",
                    id="onboard-base-url",
                )
                yield Static(
                    "api key (the nvs secret llm_api_key; blank leaves it alone)",
                    classes="onboard-label",
                )
                yield Input(password=True, id="onboard-api-key")
                yield Static("model", classes="onboard-label")
                yield self._select(
                    self._options(self.models, chosen_model),
                    chosen_model,
                    "onboard-model-select",
                    allow_blank=True,
                )
                yield Input(chosen_model, placeholder="or type a model", id="onboard-model")
                yield Static(
                    "thinking (only where the provider accepts the member)",
                    classes="onboard-label",
                )
                # Mounted unconditionally and shown or hidden: a widget that exists
                # only for some providers cannot be shown when the provider changes,
                # and `_show_thinking` would have nothing to toggle.
                yield Select(
                    [(level, level) for level in providers.THINKING_LEVELS],
                    value="none",
                    allow_blank=False,
                    id="onboard-thinking",
                )

                yield Static("Time  →  ntp.timezone, ntp.servers", classes="onboard-section")
                yield Static("region (fills the TZ string and the pool server)",
                             classes="onboard-label")
                yield self._select(
                    self._options(providers.region_names(), current_tz),
                    current_tz,
                    "onboard-region",
                    allow_blank=True,
                )
                yield Input(
                    current_tz,
                    placeholder="or type a POSIX TZ string, e.g. CET-1CEST,M3.5.0,M10.5.0/3",
                    id="onboard-timezone",
                )
            yield Static(
                "Ctrl+S: write | Esc: cancel | blank fields are left alone",
                classes="prompt-hint",
            )

    def on_mount(self) -> None:
        """Seed the picker's visibility, and the model list, from the config.

        A form opened on a provider that publishes a list must show that list: the
        reader may accept every pre-filled value and press `Ctrl+S`, so an empty model
        dropdown is not a form waiting to be filled, it is a write that will be
        skipped.
        """
        chosen = self.query_one("#onboard-provider", Select).value
        provider_id = chosen if isinstance(chosen, str) else ""
        self._show_thinking(provider_id)
        if provider_id:
            self.run_worker(self._load_models(provider_id))

    def _existing(self, path: str) -> object | None:
        """One config value by dotted path, from either shape `app.py` may hold.

        `app.py` keeps the config flattened (`settings.flatten`), so a nested read
        found nothing and every field opened empty — including the provider, which is
        what left the model list unloaded. Both shapes are accepted because this form
        is constructed directly in tests with a nested document.
        """
        current = self.current or {}
        if path in current:
            return current[path]
        value: object = current
        for segment in path.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(segment)
        return value

    def _chosen(self, select_id: str, input_id: str) -> str:
        """The dropdown's value, or the text field's when one is typed.

        One rule for all four dropdowns: the typed field wins when it has anything in
        it, and the choice is the default. That is what keeps a manual route open — a
        hidden SSID, an unpublished model, a TZ string no region names — without the
        reader having to clear a dropdown first.
        """
        typed = self.query_one(input_id, Input).value.strip()
        if typed:
            return typed
        chosen = self.query_one(select_id, Select).value
        return chosen if isinstance(chosen, str) else ""

    def answers(self) -> onboard.Answers:
        chosen_id = self.query_one("#onboard-provider", Select).value
        provider_id = chosen_id if isinstance(chosen_id, str) else ""
        entry = next((p for p in self.providers if p.id == provider_id), None)

        level = self.query_one("#onboard-thinking", Select).value
        # A hidden picker keeps whatever it last held, so a provider that takes no
        # level must contribute nothing rather than the previous provider's choice.
        extra = (
            entry.extra_body(level if isinstance(level, str) else "none")
            if entry is not None and entry.thinking_levels
            else {}
        )

        base_url = self.query_one("#onboard-base-url", Input).value.strip()
        if not base_url and entry is not None:
            base_url = entry.base_url
        provider_name = onboard.provider_id(base_url, entry)

        region = self._chosen("#onboard-region", "#onboard-timezone")
        timezone, server = providers.timezone_for(region)

        return onboard.Answers(
            ssid=self._chosen("#onboard-ssid-select", "#onboard-ssid"),
            password=self.query_one("#onboard-password", Input).value.strip(),
            base_url=base_url,
            api_key=self.query_one("#onboard-api-key", Input).value.strip(),
            model=self._chosen("#onboard-model-select", "#onboard-model"),
            auth=self._auth_for(self._chosen("#onboard-ssid-select", "#onboard-ssid")),
            provider=provider_name,
            token_param=providers.TOKEN_PARAM.get(provider_name, ""),
            extra_body=extra,
            timezone=timezone,
            ntp_server=server,
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-offer the model list once a key is typed.

        Most providers answer `401` without one, so the fetch made when the form
        opened came back empty and the dropdown stayed blank. The reader types the
        key, and the list they were promised arrives without them having to reselect
        the provider.
        """
        if event.input.id == "onboard-api-key" and event.value.strip():
            provider_id = self.query_one("#onboard-provider", Select).value
            if isinstance(provider_id, str) and provider_id:
                self.run_worker(self._load_models(provider_id))

    async def on_select_changed(self, event: Select.Changed) -> None:
        """A provider choice fills the base url and fetches its model list.

        `05` §7.1: the model list arrives when `models_url` is set, on a thread rather
        than the UI thread. A provider with no list, or a failed fetch, leaves the
        typed model field, which is the route that always works.
        """
        if event.select.id != "onboard-provider":
            return
        provider_id = event.value if isinstance(event.value, str) else ""
        entry = next((p for p in self.providers if p.id == provider_id), None)
        if entry is None:
            return
        if entry.base_url:
            self.query_one("#onboard-base-url", Input).value = entry.base_url

        # A thinking level is only meaningful for the provider it was chosen for.
        self._show_thinking(provider_id)

        await self._load_models(provider_id)

    async def _load_models(self, provider_id: str) -> None:
        """Fetch `provider_id`'s model names into the dropdown and select one.

        `set_options` resets the selection, so the value is set again afterwards.
        Without that the box renders blank and `answers()` reads no model at all,
        which drops the whole `llm.providers` write — the form submits, the reader
        sees a model chosen, and nothing is written.

        The configured model is kept when the provider still offers it; otherwise the
        first name is selected, because a filled dropdown with nothing chosen is the
        same empty answer as an unfilled one.
        """
        entry = next((p for p in self.providers if p.id == provider_id), None)
        if entry is None:
            return
        select = self.query_one("#onboard-model-select", Select)
        if self._fetch_models is None or not entry.models_url:
            return
        key = self.query_one("#onboard-api-key", Input).value.strip()
        # Mount and a `Changed` for the pre-filled provider both fire, so the same
        # list is not requested twice for one provider and key.
        if self._loaded == (provider_id, key):
            return
        self._loaded = (provider_id, key)
        models = await self._fetch_models(entry, key)
        rows = self._existing("llm.providers")
        configured = str((rows or [{}])[0].get("model") or "") if isinstance(rows, list) else ""
        keep = (
            self.query_one("#onboard-model", Input).value.strip()
            or select.value
            or configured
        )
        select.set_options(self._options(models, ""))
        if models:
            select.value = keep if keep in models else models[0]
        else:
            self.query_one("#onboard-model", Input).placeholder = (
                "no list from this provider; type the model"
            )

    def _show_thinking(self, provider_id: str) -> None:
        """Show the level picker only where the provider's `extra_body` takes one.

        `05` §7.1: a level written where the member is rejected turns every call into
        a 400, so the picker is hidden rather than left offering a value that would
        break the provider. Hidden, not disabled — a disabled row still reads as an
        option the reader is being denied.
        """
        self.query_one("#onboard-thinking", Select).display = bool(
            _thinking_levels(self.providers, provider_id)
        )

    def _auth_for(self, ssid: str) -> str:
        """The scanned security of `ssid`, or empty for a hand-typed name.

        An empty `auth` means the reader asserted a name no scan returned, and the
        planner takes that at face value; a scanned name carries the security the
        radio reported, which is what stops a password being written for a network
        the station will refuse (`04` §4.24).
        """
        for row in self.scan:
            if row.get("ssid") == ssid:
                return str(row.get("auth") or "")
        return ""

    def action_submit(self) -> None:
        self.dismiss(self.answers())


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
