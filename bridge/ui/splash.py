"""The splash screen: the wordmark, the mascot and one tip (`05` §5.9).

Musteval is a two-foot celestial spy from the Realms: white fur, tufted ears, an
elongated ferret snout and pink furtive eyes. It throws magic missiles at will.

Both blocks are solid half-blocks rather than line art, following the omp/pi
splash: at this size a thin outline reads as scratches, while a filled block keeps
its silhouette. The name is drawn as letters because that is the part that is
legible by construction; the art is a side profile because the long low body and
the tail sweeping up behind it are what identify a ferret, where a head alone
reads as a cat and a head with tall ears reads as a bat. A diagonal
pink → violet → cyan gradient runs across both, the stops the omp lockup uses.
"""

from __future__ import annotations

import random

from rich.text import Text

#: `05-bridge.md` §5.9: stored unpadded, padded to the block width when drawn, so
#: the panel never jitters and the literals carry no meaningless trailing spaces.
MASCOT: tuple[str, ...] = (
    '   ▄▄▄▄▄▄▄',
    '   ▀███████▄                ▄▄ ▄▄',
    '      ▀▀▀▀▀██▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄██▄██',
    '          █████████████████████o▄█▀',
    '           ▀▀██▀▀▀▀██▀▀▀██▀██',
    '             ██    ██   ██ ██',
)

#: The name in half-block letters, the same form the omp lockup uses. At this
#: size a wordmark identifies the thing when a drawing of it cannot.
WORDMARK: tuple[str, ...] = (
    '██▄██ █   █ █▀▀▀▀ ▀▀█▀▀ ████▀ █   █ ▄███▄ █',
    '█   █ █   █ ▀▀▀█    █   █▄▄▄▄ ▀▄ ▄▀ █▀▀▀█ █',
    '▀   ▀ ▀▀▀▀▀ ▀▀▀▀▀   ▀   ▀▀▀▀▀   ▀   ▀   ▀ ▀▀▀▀▀',
)

#: `05-bridge.md` §5.9: long enough to read one tip, short enough not to nag.
SPLASH_SECONDS = 8.0

#: The eye breaks out of the ramp so the face keeps a focal point. Every other
#: glyph takes the body gradient.
_EYE = "o"

#: omp's own lockup stops, bottom-left → top-right. Same palette, same direction.
_STOPS: tuple[tuple[int, int, int], ...] = (
    (248, 79, 204),
    (147, 98, 244),
    (0, 219, 228),
)

#: `05-bridge.md` §5.9. Every `/name` here must be in `help.COMMANDS`; the
#: drift test holds the two together.
TIPS: tuple[str, ...] = (
    "/onboard walks through wifi, the provider key and the model, one modal at a time",
    "/settings edits every scalar in config.json without memorising a dotted path",
    "/secret set api_key prompts masked and stores the value in NVS, never in a file",
    "/tasks add schedules a recurring prompt that runs on the device, not here",
    "F3 shows the last status frame; /status forces a fresh one",
    "Ctrl+R reconnects without restarting the bridge",
    "/files browses the agent sandbox, and /fs put uploads a local file to userfs",
    "/usage totals tokens for this link session, /context shows what is in the window",
    "/export writes the chat log to markdown, redaction included",
    "/ping measures one round trip; a rising number means the link is sick",
    "Esc closes a modal and cancels a run, whichever is in front of you",
    "a tool call is rendered inline, so you see the arguments before the result",
)


def _gradient(t: float) -> tuple[int, int, int]:
    """Piecewise-linear sample of `_STOPS` at `t` in [0, 1]."""
    if t <= 0:
        return _STOPS[0]
    if t >= 1:
        return _STOPS[-1]
    span = t * (len(_STOPS) - 1)
    index = min(int(span), len(_STOPS) - 2)
    part = span - index
    low, high = _STOPS[index], _STOPS[index + 1]
    return tuple(round(a + (b - a) * part) for a, b in zip(low, high))  # type: ignore[return-value]


def _style_for(row: int, col: int, char: str, width: int, rows: int) -> str:
    """One cell's style: the eye breaks out of the gradient, nothing else does.

    `width` and `rows` come from the block being drawn, so the wordmark and the
    art each span the full ramp instead of the art's geometry stretching over
    both.
    """
    if char == _EYE:
        return "hot_pink"
    red, green, blue = _gradient(
        col / max(1, width - 1) * 0.5 + (rows - 1 - row) / max(1, rows - 1) * 0.5
    )
    return f"#{red:02x}{green:02x}{blue:02x}"


def _block(rows: tuple[str, ...]) -> Text:
    """A block of art as one `Text`, coloured per character.

    Not markup: Rich rewrites art as markup, and the gradient has to be per cell
    anyway, so building the `Text` directly keeps one path instead of two.
    """
    width, height = max(len(r) for r in rows), len(rows)
    text = Text()
    for row, line in enumerate(rows):
        line = line.ljust(width)
        for col, char in enumerate(line):
            if char == " ":
                text.append(char)
            else:
                text.append(char, style=_style_for(row, col, char, width, height))
        text.append("\n")
    return text


def pick_tip(rng: random.Random | None = None) -> str:
    """One tip, chosen from the injected generator so a test can pin it.

    A module-level `random` would make the screen untestable without patching,
    and patching a module global is how a test starts depending on import order.
    """
    return (rng or random.Random()).choice(TIPS)


def _wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap. A tip that runs past the budget drops its tail to column 0."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    return lines


def render_splash(tip: str) -> Text:
    """The whole panel: the wordmark, the art, the tip and the footer.

    Both blocks are stacked rather than set side by side, as the omp splash does
    with its mark and wordmark; side by side at this width either starves the tip
    or overruns the 78-column budget of `05` §5.9, and an overrun soft-wraps at
    the terminal edge and drops its tail to column 0.
    """
    body = Text()
    body.append_text(_block(WORDMARK))
    body.append("celestial spy, two feet of white fur\n", style="grey62")
    body.append_text(_block(MASCOT))
    body.append("tip  ", style="bold gold1")
    body.append("\n")
    for line in _wrap(tip, 76):
        body.append(line + "\n", style="bright_white")
    body.append("\npress any key, or wait", style="grey62")
    return body
