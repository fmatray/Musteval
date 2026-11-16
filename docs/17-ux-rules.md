# 17 — UX Rules

Status: frozen by `docs/00-overview.md` §1. This document owns the rules every
screen, modal and keybinding is held to. It decides presentation, never a wire
contract: the frames are `04-protocol.md`, the screens and their content are
`05-bridge.md`, and the class structure is `09-bridge-architecture.md`.
Audience: bridge developers.

Each rule exists because the screens grew one at a time and each solved the same
problem slightly differently: nine screens, three spellings of the same hint line, and
a mnemonic key that meant "delete" on one screen and "download" on another. A rule
that lives only in a reviewer's head does not survive the next screen.

## 1. Keys

| Rule | Why |
|---|---|
| **`Ctrl` + a letter for every action.** A single printable letter is never an action while a table or a text field has focus. | The same letter meant different things per screen, so muscle memory from Files destroyed a task, and no key could be pressed while typing a filename into a prompt. |
| Function keys remain as **aliases** for the seven screens only (`F1`–`F7`). | They are in the muscle memory of the first users and cost nothing, but they are not the documented route: `05-bridge.md` §6.1 lists them second. |
| `Enter` and `Esc` keep their platform meaning. | `Enter` activates the focused row and `Esc` leaves; remapping either to a letter's job makes every widget fight the screen. |
| A destructive action is confirmed by `y`/`n` **only in its own confirmation modal**, never on the screen itself. | A screen-level `y` would fire while the cursor sits on a row, so a stray keystroke deletes. A modal routes the key to the question. |
| Two actions never share a key on one screen. | `W`/`O` were chosen because `U` was taken; that is a collision being discovered after the fact, not a design. |

The `Ctrl` letters are chosen per screen and reused across screens for the same job,
so the set is learnable once rather than per screen:

| Letter | Job, wherever it appears |
|---|---|
| `Ctrl+R` | Refresh / re-fetch the thing this screen shows |
| `Ctrl+N` | New |
| `Ctrl+E` | Edit |
| `Ctrl+D` | Delete |
| `Ctrl+M` | Move or rename |
| `Ctrl+K` | Create a directory |
| `Ctrl+U` | Up one level, or upload |
| `Ctrl+O` | Open, or upload |
| `Ctrl+F` | Find / filter |
| `Ctrl+S` | Save (editing only) |
| `Ctrl+Y` | Toggle (follow mode, enabled state) |
| `Ctrl+X` | Run or execute, or download |
| `Ctrl+G` | Go on without changing what the device stores |

A key that would mean two things on one screen is a design error, not a table entry.
`Ctrl+G` was chosen over `Ctrl+P` because Textual claims the latter for its command
palette, which swallows the binding inside a modal rather than passing it to the
screen.

## 2. Colour

Colour is an **enhancement, never the only carrier of a fact.** Every rule below
keeps the plain text identical when colour is unavailable, so a monochrome terminal
and a screen reader lose no information. This is the same rule as `05-bridge.md` §5's syntax
highlighting, applied to the chrome.

| Slot | Meaning | Fallback |
|---|---|---|
| `$success` | healthy: `linked`, `up`, `Enabled`, a stored value | the word itself |
| `$warning` | degraded but working: `flaky`, `wifi failed`, `SAFE MODE`, `Disabled` | the word itself |
| `$error` | broken: `down`, `refused`, `llm down`, an error code | the word itself |
| `$text-muted` | context that must not compete with the value: units, defaults, bounds, hints | dim text |
| `$accent` | the heading that names what the panel is, and the selected row's marker | bold |

Semantic slots come from the Textual theme the application is running (`app.current_theme`),
not from hard-coded hex in a widget, so a terminal in a light theme gets a light palette
and a contrast that still holds. A widget that draws in CSS writes `$error` and lets the
stylesheet resolve it; a line written into a `RichLog` draws through Rich, which does not
parse CSS variables, so it resolves `app.current_theme.error` itself. The one fixed colour
is the splash gradient, which is the identity mark rather than chrome.

The chat pane's seven kinds and the colour each one takes are `05-bridge.md` §5.1's table; this
section owns only the slots those kinds resolve to.

**Device text is escaped before it enters markup, or rendered without markup.** The
Status grid and the broker list are built as Rich markup because their labels are
styled, so every value an access point, provider or broker chose is `escape()`d at
the point it is spliced in. Unescaped, an SSID containing `[/bold]` raised
`MarkupError` and blanked the screen: the device chose the bytes and the bridge
crashed on them. A `Static` that shows only device or reader text declares
`markup=False` instead: the files path, the log filter echo, and every modal title
that names a path, an id or a question. The chat pane strips control characters from
every kind it writes, because a raw ESC reaches the terminal as a control sequence.

**A line the device refused is `$error`, and it carries the device's own words.** An
error line is the only place the reader learns why a run stopped, so it is written
with `ChatScreen.write(..., error=True)` and it repeats the `code` and `message`
verbatim rather than paraphrasing them. `llm`'s message carries the provider's
rejection body (`02-firmware.md` §llm), which is where a 400 names the offending
member — the model, the tool array, the thinking level. A bridge that replaced that
sentence with "network issue" would be discarding the one diagnostic the firmware
took the trouble to keep, and the `✗` would outlive it as decoration.

## 3. Hint lines

Every screen ends in a **hint line**, and every hint line is built by one function,
`hint_bar()` in `bridge/ui/render.py`, from the same `(key, label)` pairs the screen's
`BINDINGS` already declare. Three rules follow from that:

- **The hint cannot drift from the binding.** It is not a hand-written string; if a
  key changes, the hint changes in the same edit. A hint that names a key the screen
  no longer binds is a lie the user acts on.
- **The key is styled, the label is muted.** `Ctrl+D` in `$accent` and `Delete` in
  `$text-muted` reads as a key and a verb at a glance in a line of eight.
- **It wraps rather than truncating.** A screen with more than a terminal's worth of
  keys loses the last one silently if it is clipped; wrapping costs a row and keeps
  every key discoverable.

`Enter` and `Esc`, where the screen acts on them, are written into the same line as
if they were bindings, because from the reader's side they are.

**Sixty columns is the narrowest supported terminal.** Wrapping is what costs rows, so
the cost has to stop somewhere: at 60×20 the Chat hint takes four of the twenty rows
and leaves eight for the log, which is still a usable pane, and at 20×8 the same hint
wants sixteen and the layout has nothing left. Below 60 columns the hint keeps
wrapping and the content panes shrink past the point of use. That is a stated limit
rather than a clamp, because clamping is the truncation the third rule forbids.

**A screen whose content fills the pane still ends in one.** Chat, Status, Logs and
MQTT each end in content — the composer, the grid, the buffer, the traffic log — and
each bound keys with no row naming them, so the key was discoverable only by reading
this document. They carry a hint line like Tasks, Settings and Files already did,
built from their own `BINDINGS`; Chat adds `Enter` as a pair of its own because the
composer acts on it without declaring it.

**The app's own row is a hint line too.** `Ctrl+Q`, `F1`–`F7` and `Esc` are the
`BridgeApp` bindings, so the row under the composer is `hint_bar` over those pairs,
not Textual's `Footer`. The `Footer` clipped mid-word — `Ctrl+R Reconnec` where the
terminal was wide enough for the whole label, and `F6`/`F7` gone at 50 columns — which
is the truncation the third rule above forbids. Textual's `Footer` shows
`Ctrl+P palette` as well, a command palette the app never bound, so its row named a
key the reader could press and nothing happened.

## 4. Modals

- **Width follows the content, bounded by the terminal.** A one-line prompt is not
  widened to 90 columns to match the editor, and the editor is not pinned to 90 on a
  60-column terminal. Width is `min(content, terminal - margin)`.
- **A modal that edits a document is wide.** The file editor takes at least 80% of
  the terminal width, because a document wrapped at 60 columns is unreadable and the
  wrap point is not the file's.
- **Vertical is content-sized** unless the content is a document, which takes a
  fraction of the screen and scrolls inside itself.
- **State is set in `__init__`, never in `compose()`.** `compose` may run more than
  once and is a pure description of the tree; a constructor assignment inside it
  resets the screen on a re-mount.
- **`Esc` always leaves**, per `05-bridge.md` §5.10 and §6.1.

## 5. Search and filtering

A list longer than a screen needs a way to narrow it without leaving the screen.

- `Ctrl+F` opens the screen's filter, seeded with what is currently applied, so
  refining beats retyping.
- The filter is a **case-insensitive substring** over the columns a reader would
  search by name, not a regex: a terminal user typing `run_7` should not have to
  escape a `_`.
- Filtering never re-orders what it keeps. The rows are the device's order, narrowed.
- The applied filter is **shown**, in the screen's heading, so a reader who forgot it
  is not left wondering where an entry went.
- Clearing the filter is one key (`Esc` in the filter bar, or an emptied field), and
  restoring the full list is instant because the rows are all still held.

## 6. Onboarding

`/onboard` is **three sequential steps**, and each is written and tested before the
next is asked for:

| Step | Asks | Writes | Test |
|---|---|---|---|
| 1. Network and time | SSID, passphrase, region | `wifi.networks`, `ntp.timezone`, `ntp.servers`, `nvs:wifi_psk` | `reload`, then `status`: `wifi.state` and `time.tz` |

| 2. Provider | provider, base URL, API key | — (the list is the proof) | the provider's model list, fetched with this key |
| 3. Model | model, thinking level | `llm.providers`, `nvs:llm_api_key` | the chosen name is in the list step 2 fetched |

The order is the dependency order: `reload` decides whether the station joins, the
station decides whether a provider is reachable, and the provider's published list
decides which model names are real. Writing all three and testing at the end reports
after the fact — which is how a configured device sat with a blank provider and a
model list nobody could load.

The provider entry is written in step **3**, not step 2, because `03-config.md` makes
`model` required: there is no half-configured provider to write, and a device given one
refuses the document.

**Step 3 seeds its thinking level from the stored entry**, not from `none`. The
dropdown opens on the level `llm.providers[0].extra_body` already names, read back
through the catalog's mapping (`reasoning_effort: high` → `high`), so re-running the
wizard is not a silent downgrade for a reader who set a level once.

**A stored value the row does not offer is shown as itself.** Ollama names its levels
per model, so `reasoning_effort: max` is a real setting that the `ollama-cloud` row
does not list. The step adds it as its own row, because the answer is what gets
written back: falling back to the first row and then rebuilding `extra_body` from the
picker replaced `max` with `low` on a re-run that touched nothing. Only a value the
provider's surface cannot express at all — a `"none"` on a surface with no off switch
— is absent, and there the picker shows its first row and the write is what the
reader chose rather than a value the step invented.

Network and time are one step because they are **one test**. A single `reload` reruns
the boot sequence that applies both `wifi.*` and `ntp.timezone`, and one `status` reply
answers both facts. Two steps would mean two reloads, each blocking up to one
`connect_timeout` per configured network, to learn what one reply already says.

**A step's test is a consequence, not the write's `ok`.** `config.set` answering ok
means the document changed; it says nothing about whether the station associated or
whether `setenv("TZ")` took. Each step therefore reads back the fact it depends on and
says so in one chat line: `network: joined HomeLab`, or `network: the station is still
not associated`, or `time: zone still UTC0; the write did not apply`.

**The form reads the config in the shape the app holds it.** `app.py` keeps the config
flattened to dotted paths (`settings.flatten`), so a pre-fill that reads nested keys
finds nothing and the whole form opens empty — including the provider, which is what
left the model list unloaded.

**The form fetches the config itself.** `_config` is filled by a `config.get` reply, and
the only other request came from opening the settings screen. A process that opened the
wizard first therefore pre-filled from an empty document, which looked identical to a
device with nothing configured. `/onboard` requests the document before it shows the
first step, and restores the screen the reader was on afterwards, because a
whole-document reply also navigates to the settings grid.

**A choice is a dropdown where the values are known and a text field where they are
not.** The SSID, the provider, the model and the region are `Select`s, because each has
a finite list the bridge can offer: the scan result, the catalog, the provider's model
list, the region table. Every dropdown keeps a manual route — an SSID the scan cannot
see (a hidden network, a scan that answered `E_WIFI`), a model the provider does not
publish, and a region the table lacks — so the wizard never traps a reader behind a
list that did not load.

**A loaded list is also a selected one.** A list in a `Select` with no value chosen
reads the same as a field the reader has not reached yet, so each step selects the
configured value when the list still offers it and the first entry otherwise. The model
list is fetched in step 2, before the model is asked for, and again when an API key is
typed: a provider that answers `401` without one returns nothing the first time.

**A stored id the catalog does not name is a row, not a value.** `id` is free text in
`03-config.md`, so a device configured over raw frames holds whatever was written — the
board holds `p1`. A `Select` whose value is absent from its own options raises
`InvalidSelectValueError` on mount, so passing that id as the value killed the wizard
before it drew, and only on a device that already had a provider. Every dropdown
therefore offers the stored value as a row next to the known ones.

**A stored TZ string is translated back to its region, never shown raw.**
`ntp.timezone` holds a POSIX string; the dropdown holds place names. Comparing the two
directly always missed, which is how `CET-1CEST,M3.5.0,M10.5.0/3` was shown back to a
reader who expected a place. `providers.region_for_timezone` does the reverse lookup,
and a zone the table does not list keeps its own `custom: <tz>` row — with no
`ntp.servers` written, because guessing a pool could replace a working server.

**Each step can be passed over with `Ctrl+G`, configured or not.** The key always
continues: a wizard that silently ignores a key the screen advertises is
indistinguishable from a broken wizard, and the reader has no way to tell which step
refused them or why. A passed step sends no frame, reloads nothing and prints no
verdict — a test that did not run must not report, or the reader learns to distrust
the line a real test produced. What the key keeps is the device's stored value; what
it costs is named in the step's own line before it is pressed:

|Stored for this step|Line|
|---|---|
|all of it|`network and zone stored; Ctrl+G keeps both`|
|part of it|`no stored zone; Ctrl+G leaves it unconfigured`|
|none of it|`nothing stored; Ctrl+G leaves this step unconfigured`|

Naming the missing part matters on the device that prompted the key: a configured
network with no zone must not be reported as a missing network. The consequence is
stated rather than enforced, because the wizard's job is to make an unconfigured
domain visible, not to refuse to continue past one.

**A step's test runs behind a progress screen.** Each proof is a round trip the device
answers on its own schedule — a `reload` waits on `net_connect`, a model fetch waits on
an HTTPS call — so a wizard that is working looks exactly like a wizard that has hung.
`Working` is pushed for the duration of the writes, the `reload` and the fetch, names
the action in flight, and dismisses itself when the reply lands. It is not cancellable:
the frame is already sent, and a cancel would leave its caller awaiting a screen the
reader had dismissed.

**Cancelling a step writes nothing and stops the wizard.** Each step is its own modal,
so `Esc` at step 2 leaves the step-1 writes in place — they were already tested and
reported — and sends no further frame.

## 7. Empty and loading states

A screen with no data says **which command would fill it**, not only that it is
empty. `no config yet: Ctrl+R to fetch` tells the reader the next action; `no config`
leaves them to guess whether the device is silent, the screen is broken, or they are
on the wrong screen. No screen may present a spinner or a blank panel where a word
and a key would do.

A **wait** is the one case with no key to offer, and there the word does all the work.
A round trip the device answers on its own schedule — `reload` inside its handler,
`wifi.scan`, a model fetch — is shown by `Working`, which names the action in flight
and the command behind it (`reload: re-running the boot sequence`), and dismisses
itself when the reply lands. The spinner beside that line is decoration; the sentence
is the contract. `Working` binds no `Esc` and offers no cancel, because the frame is
already sent: a cancel would dismiss the screen while its caller still awaited the
device, and the two would disagree about which screen the reader is on.
