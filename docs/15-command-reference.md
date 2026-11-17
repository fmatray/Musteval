# Command Reference

Every slash command and every accepted form, in alphabetical order. One row per
form, so a reader who knows the word can find the line without scanning a table
grouped by screen.

`05-bridge.md` §7 owns the contract: the mapping, the notes and the reasoning live
there, and this file restates only the projection a reader looks up — the form, its
arguments, the frame it sends and whether it sends one at all.
`bridge/tests/test_docs.py` locks this table to the command catalogue, so a command
added to the code and left out of here fails a test rather than going undiscovered.

Column meanings:

- **Arguments** — what the form accepts after the command word. `none` means a
  surplus word is refused with a `usage:` line, not ignored.
- **Sends** — the frame, or `(Local)` for a command that renders from state the
  bridge already holds and puts nothing on the wire.

## The table

| Form | Arguments | Sends | Note |
|---|---|---|---|
| `/cancel` | none | `cancel` | `reason: "user"`; stops the run in flight. Answers in chat either way, the device text printed verbatim: `run cancelled` or `nothing to cancel` |
| `/clear` | none | `history.clear` | Forgets the conversation on the device and in the pane |
| `/config` | none | (Local) | Opens the Settings screen; the screen sends `config.get` and `config.set` |
| `/config edit` | none | (Local) | Same screen as the bare `/config` |
| `/config get <path>` | `path` | `config.get` | Dumps one value |
| `/config set <path> <value>` | `path`, then the value as the rest of the line | `config.set` | Writes one value |
| `/context` | none | `context.budget` | Opens one box: every block of the next request with its byte count, then `used`/`free` |
| `/export` | none | (Local) | Names the `bridge-export-<ts>.md` file it wrote |
| `/files [<path>]` | optional sandbox `path`, `/workspace/` by default | (Local) | Opens the Files screen; the screen then sends `sandbox.list` |
| `/fs put <path>` | `path` inside the import allowlist | `fs.put` | Prompts for a local file, uploads it in 2 KiB chunks |
| `/geo` | none | `geo.locate` | Forces a fresh fix |
| `/help` | none | (Local) | Opens the Help screen |
| `/logs` | none | (Local) | Opens the Logs screen |
| `/mcp` | none | (Local) | Lists servers from the last `status.mcp[]` |
| `/model` | none | `config.get`, `config.set` | Opens the picker, pre-filled with the stored model |
| `/model <name>` | `name` | `config.get`, `config.set` | Writes the whole `llm.providers` array |
| `/mqtt` | none | `mqtt.status` | Lists brokers and state |
| `/mqtt publish <server> <topic> <payload>` | `server`, `topic`, then the payload as the rest of the line | `mqtt.publish` | Publishes once |
| `/onboard` | none | `wifi.scan`, `status`, `reload`, `secret.set`, `config.get`, `config.set` | Three-step wizard; writes each step before asking the next |
| `/ping device` | `device` | `ping` | Link latency, measured by the bridge itself |
| `/ping llm` | `llm` | `net.ping` | The device ICMPs its active provider host |
| `/ping ntp` | `ntp` | `net.ping` | The device ICMPs its first NTP server |
| `/ping router` | `router` | `net.ping` | The device ICMPs its default gateway |
| `/providers` | none | `config.get`, `config.set`, `secret.set`, `secret.delete` | Opens the provider editor: add, log in to, or disconnect an `llm.providers` entry (`05-bridge.md` §7.7) |
| `/quit` | none | (Local) | Exits the bridge |
| `/retry` | none | `prompt` | Resends the last prompt verbatim; refused when there is none |
| `/run <id>` | task `id` | `tasks.run` | Runs a task now and names the `run_id` |
| `/sandbox` | none | `sandbox.list` | Lists the sandbox root, `/workspace/` |
| `/sandbox delete [-r\|--recursive] <path>` | optional recursive flag, `path` | `sandbox.delete` | The flag may sit before or after the path |
| `/sandbox list [<path>]` | optional `path` | `sandbox.list` | Directory listing |
| `/sandbox mkdir <path>` | `path` | `sandbox.mkdir` | Creates one directory |
| `/sandbox move <from> <to>` | `from`, `to` | `sandbox.move` | Renames or relocates |
| `/sandbox read <path>` | `path` | `sandbox.read` | Prints one file; a file is written from the Files screen, never from chat |
| `/secret delete <key>` | `key` | `secret.delete` | Erases one key; refused while a config `_ref` still names it |
| `/secret set <key>` | `key` | `secret.set` | Value is prompted, masked, and never echoed |
| `/sys diag` | `diag` | `status` | Opens the info modal on the diagnostic half of a fresh `status`, headed `Memory`, `Boot`, `Tools`, `LLM budget` |
| `/sys reboot` | `reboot` | `reboot` | Restarts the device; reports the grace period in chat |
| `/sys reload` | `reload` | `reload` | Re-reads the flash configs; reports each reloaded scope and each warning in chat |
| `/sys reset-counter` | `reset-counter` | `sys.reset_counter` | Clears the boot-failure counter |
| `/sys status` | `status` | `status` | Opens the info modal on the general half of a fresh `status` |
| `/tasks` | none | `tasks.list` | Lists tasks and switches to the Tasks screen |
| `/tasks add` | none taken from the line | (none) | Refused with `a task body must be supplied (add opens a modal)`; a new task is authored with `Ctrl+N` on the Tasks screen |
| `/tasks delete <id>` | task `id` | `tasks.delete` | Confirms first; the one irreversible command |
| `/tasks disable <id>` | task `id` | `tasks.set` | Resends the cached task with `enabled: false` |
| `/tasks enable <id>` | task `id` | `tasks.set` | Resends the cached task with `enabled: true` |
| `/tasks set <id>` | task `id` | `tasks.set` | Resends the cached task unchanged |
| `/template <name>` | template `name` | `template.get` | Dumps one template's text |
| `/templates` | none | `templates.list` | Lists prompt templates |
| `/thinking` | none | `config.get`, `config.set` | Reports the stored level and the levels this provider accepts |
| `/thinking <level>` | a level the provider accepts | `config.get`, `config.set` | Writes the level into `llm.providers[0].extra_body` |
| `/todo` | none | `todo.apply` | Same as `/todo view` |
| `/todo append <phase> <item>...` | `phase`, then at least one `item` | `todo.apply` | Adds items to a phase, creating it |
| `/todo block <target>` | `target` (task or phase) | `todo.apply` | Marks actionable targets blocked |
| `/todo done <target>` | `target` (task or phase) | `todo.apply` | Marks completed |
| `/todo drop <target>` | `target` (task or phase) | `todo.apply` | Marks abandoned |
| `/todo edit` | none | `todo.apply` | Opens the tree editor, which sends one `init` per rewrite |
| `/todo init` | none | `todo.apply` | Clears the list |
| `/todo init <phase> <item>...` | `phase`, then at least one `item` | `todo.apply` | Replaces the list with one phase |
| `/todo rm <target>` | `target` (task or phase) | `todo.apply` | Removes a task |
| `/todo start <target>` | `target` (task or phase) | `todo.apply` | Marks in progress |
| `/todo unblock <target>` | `target` (task or phase) | `todo.apply` | Clears a blocker |
| `/todo view` | none | `todo.apply` | Renders phases and tasks; a target is refused |
| `/tool disable` | `disable <name>` | `config.set tools.disabled` | Removes a name from the model's tool list |
| `/tool enable` | `enable <name>` | `config.set tools.disabled` | Puts a disabled name back |
| `/tool list` | `list` | `tools.list` | The tool table: name, enabled or disabled, description, under a header row. Same output as `/tools` |
| `/tools` | none | `tools.list` | The tool table: name, enabled or disabled, description, under a header row. Same output as `/tool list` |
| `/usage` | none | (Local) | Token totals for this link session |
| `/wifi connect` | `connect` | `wifi.connect` | Joins the best configured network |
| `/wifi disconnect` | `disconnect` | `wifi.disconnect` | Leaves the network and stays down |
| `/wifi edit` | none | `config.get`, `wifi.scan`, `secret.set`, `config.set` | Opens the network editor: pick a known network to change its passphrase or forget it, or add one from the scan (`Ctrl+R` rescans) or by hand (`05-bridge.md` §7.6) |
| `/wifi list` | none | `wifi.status`, `wifi.scan` | One row per configured network with its priority, whether it is joined, and its RSSI when audible; `unknown` when the scan was refused |
| `/wifi reconnect` | `reconnect` | `wifi.reconnect` | Leaves and rejoins, moving to the best AP |
| `/wifi scan` | `scan` | `wifi.scan` | Lists audible access points, strongest first |
| `/wifi status` | `status` | `wifi.status` | Station state, SSID, RSSI and IP; busy-safe |

## Commands grouped by kind

| Kind | Commands |
|---|---|
| Device, one frame | `config` (`get`, `set`), `fs`, `geo`, `mqtt`, `ping` (three of four), `reboot`, `reload`, `run`, `sandbox`, `secret`, `status`, `sys`, `tasks` (all but `add`), `template`, `templates`, `todo`, `wifi` |
| Device, read-modify-write | `model`, `thinking`, `tool` |
| Device, a wizard of several frames | `onboard` |
| Host-only frame | `cancel`, `ping device`, `retry` |
| Local, no frame | `config` (bare and `edit`), `context`, `export`, `files`, `help`, `logs`, `mcp`, `usage`, `quit` |

## Refusals a reader will meet

Every refusal is a chat line, costs no round trip, and is raised before any frame is
built. The wording is the code's, quoted so a reader can search for it.

| Refusal | Raised by |
|---|---|
| `usage: <form>` | A missing or surplus argument on a fixed-arity form |
| `unknown command: /<head>` | A head no dispatch site knows |
| `unknown subcommand: /tasks <word>` | `/tasks` with a word that is not one of its five verbs |
| `unknown task id: <id>` | `/tasks set\|enable\|disable` on an id not in the cached list |
| `path is outside /config.json, /tasks.json, /skills/, /prompts/, /tools/` | `/fs put` outside the import allowlist |
| `path must be absolute: <path>` | `/fs put` with a relative path |
| `outside /workspace/: <path>` | A `/sandbox` or `/files` path outside the sandbox |
| `no secret value supplied` | `/secret set` whose modal was cancelled |
| `no prompt to retry` | `/retry` before any prompt was sent |
| `view takes no target: /todo [view]` | `/todo view` with a trailing word |
