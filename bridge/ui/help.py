"""Help screen body and slash-command completion (`05-bridge.md` §5.6, §7).

`COMMANDS` is the only catalogue of slash commands: the help screen, the composer
highlighter and the input suggester all read it, so a command cannot be documented
in one place and offered in another.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.suggester import Suggester

from ..commands import TODO_OPS


@dataclass(frozen=True)
class CommandHelp:
    """One command as the help screen lists it."""

    name: str
    """Command head without the leading slash."""

    usage: str
    """Every accepted form, joined by ` | `."""

    summary: str
    """One line, lowercase start, no trailing period."""

    local: bool
    """True when no frame leaves the host (`05-bridge.md` §7)."""


#: Tab groupings for the help modal (`05-bridge.md` §5.6). A dict rather than a
#: field on every entry so the catalogue stays one table and the grouping stays one
#: statement; a name in no group, or in two, fails `test_ui_help.py` rather than
#: vanishing from the modal.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Device": (
        "status", "reload", "reboot", "tasks", "run", "config", "secret",
        "templates", "template", "mqtt", "geo", "wifi", "todo", "fs", "sandbox", "sys",
    ),
    "Chat": ("retry", "ping", "cancel", "diag", "thinking", "model", "export"),
    "Info": ("tools", "mcp", "usage", "context"),
    "Screens": ("settings", "onboard", "files", "logs", "help", "quit"),
}

#: `05-bridge.md` §7, device commands first, then local ones.
COMMANDS: tuple[CommandHelp, ...] = (
    CommandHelp("status", "/status", "force a status refresh", False),
    CommandHelp("reload", "/reload", "re-read the flash configs", False),
    CommandHelp("reboot", "/reboot", "restart the device", False),
    CommandHelp(
        "tasks",
        "/tasks | /tasks add | /tasks set <id> | /tasks enable <id>"
        " | /tasks disable <id> | /tasks delete <id>",
        "list tasks, or add, set, enable, disable or delete one",
        False,
    ),
    CommandHelp("run", "/run <id>", "run a task now", False),
    CommandHelp(
        "config",
        "/config get <path> | /config set <path> <value>",
        "read a config value, or update one",
        False,
    ),
    CommandHelp(
        "secret", "/secret set <key>", "prompt for a secret value, masked", False
    ),
    CommandHelp("templates", "/templates", "list prompt templates", False),
    CommandHelp("template", "/template <name>", "dump one prompt template", False),
    CommandHelp(
        "mqtt",
        "/mqtt | /mqtt publish <srv> <topic> <payload>",
        "list brokers and state, or publish one message",
        False,
    ),
    CommandHelp("geo", "/geo", "force a fresh location fix", False),
    CommandHelp(
        "fs",
        "/fs put <path>",
        "upload a local file to userfs in 2 KiB chunks",
        False,
    ),
    CommandHelp(
        "sandbox",
        "/sandbox list|read|write <path> | mkdir|move | delete [-r] <path>",
        "browse and edit the agent sandbox from chat",
        False,
    ),
    CommandHelp(
        "sys",
        "/sys reset-counter",
        "clear the boot-failure counter",
        False,
    ),
    CommandHelp("retry", "/retry", "resend the last prompt", False),
    CommandHelp(
        "ping",
        "/ping device|router|llm|ntp",
        "link latency, or reach the router, llm, ntp",
        False,
    ),
    CommandHelp("cancel", "/cancel", "cancel the current run", False),
    CommandHelp(
        "thinking",
        "/thinking | /thinking <level>",
        "read or set the provider's thinking level",
        False,
    ),
    CommandHelp(
        "model",
        "/model | /model <name>",
        "pick the provider's model",
        False,
    ),
    CommandHelp("wifi", "/wifi scan|status|connect|disconnect|reconnect",
                "scan the radio, or read and steer the station", False),
    CommandHelp("diag", "/diag", "render the diagnostic half of status", False),
    CommandHelp("tools", "/tools", "list built-in and MCP tools", True),
    CommandHelp("mcp", "/mcp", "list MCP servers from the last status", True),
    CommandHelp("usage", "/usage", "token totals for this link session", True),
    CommandHelp("context", "/context", "input size and token usage", True),
    CommandHelp("todo", "/todo [op] [target]", "read and steer the run's todo list", False),
    CommandHelp("export", "/export", "write the chat log to a markdown file", True),
    CommandHelp("settings", "/settings", "browse and edit device config", True),
    CommandHelp("onboard", "/onboard", "set wifi, provider key and model", True),
    CommandHelp("files", "/files [path]", "browse and edit the agent sandbox", True),
    CommandHelp("logs", "/logs", "switch to the Logs screen", True),
    CommandHelp("help", "/help", "show the command reference", True),
    CommandHelp("quit", "/quit", "exit the bridge", True),
)

#: Accepted sub-forms of the heads that take one (`05-bridge.md` §7).
_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "tasks": ("add", "set", "enable", "disable", "delete"),
    "config": ("get", "set"),
    "mqtt": ("publish",),
    "fs": ("put",),
    "sandbox": ("list", "read", "write", "mkdir", "move", "delete"),
    "sys": ("reset-counter",),
    "secret": ("set",),
    "ping": ("device", "router", "llm", "ntp"),
    "wifi": ("scan", "status", "connect", "disconnect", "reconnect"),
    # `edit` is a UI verb, not a device op: it opens a modal, so it is offered in
    # the list without being part of `TODO_OPS`, which is `tools/todo.md`'s op set.
    "todo": (*TODO_OPS, "edit"),
}

#: What each sub-command does, keyed by head then verb. The head's own `summary`
#: says what the command is for; this says which form does what, which is the
#: question a reader typing `/tasks ` actually has. A verb missing here falls back
#: to the head's summary rather than a blank column.
SUBCOMMAND_HELP: dict[str, dict[str, str]] = {
    "tasks": {
        "add": "add a task from a JSON description",
        "set": "update one field of a task by id",
        "enable": "let a task fire again",
        "disable": "keep a task but stop it firing",
        "delete": "remove a task by id",
    },
    "config": {"get": "read one value by path", "set": "write one value by path"},
    "mqtt": {"publish": "publish one message to one broker"},
    "fs": {"put": "upload a local file into userfs"},
    "sandbox": {
        "list": "list a directory",
        "read": "print a file",
        "write": "write a file",
        "mkdir": "create a directory",
        "move": "move or rename a path",
        "delete": "remove a path",
    },
    "sys": {"reset-counter": "clear the boot-failure counter"},
    "secret": {"set": "store a secret, prompted and masked"},
    "ping": {
        "device": "ping the link itself",
        "router": "ping the gateway",
        "llm": "ping the model provider",
        "ntp": "check the clock source",
    },
    "wifi": {
        "scan": "list visible networks",
        "status": "show the current link",
        "connect": "join a network",
        "disconnect": "drop the link, and stay down",
        "reconnect": "join the configured network again",
    },
    "todo": {
        "view": "show the phases and their tasks",
        "init": "replace the list, one phase of items",
        "start": "mark a task or phase in progress",
        "done": "mark a task or phase completed",
        "drop": "abandon a task or phase",
        "block": "mark a task blocked, with a reason",
        "unblock": "clear a blocker",
        "rm": "remove a task",
        "append": "add items to a phase, creating it",
        "edit": "open a modal to add, modify or delete",
    },
}

_NAMES: tuple[str, ...] = tuple(command.name for command in COMMANDS)


#: Beyond this the aligned column would wrap in a normal terminal, so the
#: summary moves to its own line instead of padding the usage out to 100 columns.
TABLE_WIDTH = 62


def _table(group: tuple[CommandHelp, ...]) -> list[str]:
    """One line per command, summary on a second line when the usage is wide."""
    width = max(len(command.usage) for command in group)
    if width > TABLE_WIDTH:
        lines: list[str] = []
        for command in group:
            forms = command.usage.split(" | ")
            lines.append(f"  {forms[0]}")
            lines.extend(f"  {form}" for form in forms[1:])
            lines.append(f"      {command.summary}")
        return lines
    return [f"  {command.usage.ljust(width)}  {command.summary}" for command in group]


#: Above the tables in every tab: the keys that are not a slash command at all.
KEYS_TEXT = (
    "F1 Help | F2 Chat | F3 Status | F4 Tasks | F5 Logs | F6 MQTT | F7 Files\n"
    "Ctrl+R Reconnect | Ctrl+Q Quit\n"
    "Esc Closes a modal and cancels a run"
)


def render_help(category: str) -> str:
    """One tab's body: the commands in that group, in catalogue order."""
    names = CATEGORIES[category]
    group = tuple(command for command in COMMANDS if command.name in names)
    return "\n".join(_table(group)) + "\n"


def _render_help() -> str:
    lines = [KEYS_TEXT]
    for category in CATEGORIES:
        lines.extend(["", category, *render_help(category).splitlines()])
    return "\n".join(lines) + "\n"


HELP_TEXT: str = _render_help()


def help_tabs() -> dict[str, str]:
    """The modal's tab bodies, in `CATEGORIES` order (`05` §5.6)."""
    return {category: render_help(category) for category in CATEGORIES}


def _longest_common_prefix(values: list[str]) -> str:
    first, last = min(values), max(values)
    index = 0
    while index < len(first) and first[index] == last[index]:
        index += 1
    return first[:index]


def _complete(token: str, candidates: tuple[str, ...], prefix: str) -> str | None:
    matches = [candidate for candidate in candidates if candidate.startswith(token)]
    if not matches:
        return None
    completion = matches[0] if len(matches) == 1 else _longest_common_prefix(matches)
    # Never offer a value the user has already typed in full.
    if len(completion) <= len(token):
        return None
    return prefix + completion


def suggest(value: str) -> str | None:
    """The whole replacement value for `value`, or `None` when nothing completes."""
    if not value.startswith("/") or value.endswith(" "):
        return None
    head, separator, partial = value[1:].partition(" ")
    if not separator:
        return _complete(head, _NAMES, "/")
    subcommands = _SUBCOMMANDS.get(head)
    if subcommands is None:
        return None
    return _complete(partial, subcommands, f"/{head} ")


def _summary(name: str) -> str:
    for command in COMMANDS:
        if command.name == name:
            return command.summary
    return ""


def options(value: str) -> list[Completion]:
    """Candidate completions for the token being typed, in catalogue order.

    A sub-command gets its own line from `SUBCOMMAND_HELP`, falling back to the
    head's summary when the table has none: `/tasks ` answers "which form does
    what", while `/tasks` alone answers "what is this command".
    """
    if not value.startswith("/"):
        return []
    head, separator, partial = value[1:].partition(" ")
    if not separator:
        return [
            Completion(f"/{name}", _summary(name))
            for name in _NAMES
            if name.startswith(head)
        ]
    subcommands = _SUBCOMMANDS.get(head)
    if subcommands is None:
        return []
    summary = _summary(head)
    help_by_verb = SUBCOMMAND_HELP.get(head, {})
    return [
        Completion(f"/{head} {subcommand}", help_by_verb.get(subcommand, summary))
        for subcommand in subcommands
        if subcommand.startswith(partial)
    ]


@dataclass(frozen=True)
class Completion:
    """One row of the candidate list: what Tab would insert, and what it does."""

    text: str
    """The completion itself, e.g. `/tasks add`."""

    help: str
    """One line from the same `COMMANDS` catalogue the help screen prints, so a
    command cannot be documented in one place and described differently here."""


class CommandSuggester(Suggester):
    """Composer completion: `Input` renders the tail of `suggest(value)`."""

    async def get_suggestion(self, value: str) -> str | None:
        return suggest(value)
