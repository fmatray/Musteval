"""Help screen body and slash-command completion (`05-bridge.md` §5.6, §7).

`COMMANDS` is the only catalogue of slash commands: the help screen, the composer
highlighter and the input suggester all read it, so a command cannot be documented
in one place and offered in another.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.suggester import Suggester


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
        "templates", "template", "mqtt", "geo", "fs", "sandbox", "sys",
    ),
    "Chat": ("retry", "ping", "cancel", "diag", "export"),
    "Info": ("tools", "mcp", "usage", "context", "todo"),
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
    CommandHelp("ping", "/ping", "test link latency", False),
    CommandHelp("cancel", "/cancel", "cancel the current run", False),
    CommandHelp("diag", "/diag", "render the diagnostic half of status", False),
    CommandHelp("tools", "/tools", "list built-in and MCP tools", True),
    CommandHelp("mcp", "/mcp", "list MCP servers from the last status", True),
    CommandHelp("usage", "/usage", "token totals for this link session", True),
    CommandHelp("context", "/context", "input size and token usage", True),
    CommandHelp("todo", "/todo", "re-render todo phases and tasks", True),
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


def options(value: str) -> list[str]:
    """Candidate completions for the token being typed, in catalogue order."""
    if not value.startswith("/"):
        return []
    head, separator, partial = value[1:].partition(" ")
    if not separator:
        return [f"/{name}" for name in _NAMES if name.startswith(head)]
    subcommands = _SUBCOMMANDS.get(head)
    if subcommands is None:
        return []
    return [f"/{head} {subcommand}" for subcommand in subcommands if subcommand.startswith(partial)]


class CommandSuggester(Suggester):
    """Composer completion: `Input` renders the tail of `suggest(value)`."""

    async def get_suggestion(self, value: str) -> str | None:
        return suggest(value)
