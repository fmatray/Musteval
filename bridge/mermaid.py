"""Draw a Mermaid flowchart as an indented tree, for the chat pane (`05` 5.1).

A model answers with a fenced `mermaid` block and a terminal has no renderer for
it, so the alternative to this module is the diagram's source with its arrows
spelt out. The pane draws the graph instead: a tree of nodes, one edge per line,
with the arrow a glyph and the edge label after it.

The shapes the docs rely on are `flowchart` and `graph`, in either direction, and
`stateDiagram-v2`, whose transition list is the same edges with a colon label.
Everything else — `sequenceDiagram`, `classDiagram`, a kind added after this
was written — returns `None` so the caller keeps printing the fence as code. A
guessed rendering of a sequence diagram would be worse than its source.

Pure: it takes text and a width and returns lines. No Rich, no widget, so the
parse and the layout are host-tested without a terminal.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

#: Edge spellings, longest first so `<-->` is not read as `<--` then `>`.
_ARROWS = (
    "<-.->",
    "<==>",
    "<-->",
    "-->>",
    "-.->",
    "==>",
    "-->",
    "---",
    "--x",
    "--o",
    "x--x",
    "o--o",
    ".->",
)

#: The glyph a reader sees for each edge. `---` is mermaid's undirected link and
#: `-.->` its dotted one; both keep their meaning as a mark on the arrow.
_GLYPH = {
    "-->": "▸",
    "---": "─",
    "-.->": "┈▸",
    ".->": "┈▸",
    "==>": "━▸",
    "<-->": "↔",
    "<-.->": "↔",
    "<==>": "↔",
    "--x": "✗",
    "--o": "○",
    "-->>": "▸▸",
}

#: A node body in its bracket spellings: `[[sub]]`, `[(db)]`, `((round))`,
#: `{choice}`, `[plain]`. Alternation order matters: `[[` before `[`, or the
#: inner `]` ends the match early and the label keeps a bracket.
_SHAPE = r"\[\[[^\]]*\]\]|\[\([^)]*\)\]|\[[^\]]*\]|\(\([^)]*\)\)|\([^)]*\)|\{[^}]*\}"
_NODE = rf"[\w.:\-]+(?:{_SHAPE})?"
_ARROW = "|".join(sorted((re.escape(a) for a in _ARROWS), key=len, reverse=True))

_TOKEN = re.compile(rf"(?P<arrow>{_ARROW})\s*(?:\|(?P<label>[^|]*)\|)?|(?P<node>{_NODE})")
#: `A -- text --> B` is the same edge as `A -->|text| B`; normalised, so the
#: tokeniser sees one spelling.
_NAMED = re.compile(r'\s(--|-\.|==)\s*(?P<t>"[^"]*"|[^|\->=.][^>]*?)\s*(-->|\.->|==>)\s')
_HEAD = re.compile(r"^(?P<kind>flowchart|graph)\s+(?P<dir>LR|RL|TD|TB|BT)\s*$")
_STATE = re.compile(r"^stateDiagram(?:-v2)?\s*$")
_SUB = re.compile(r"^subgraph\s+(?P<id>[\w.:\-]+)\s*(?:\[(?P<label>[^\]]*)\])?$")
#: Directives that carry no graph: styling, click handlers, direction overrides.
_SKIP = re.compile(r"^(%%|classDef\b|class\b|linkStyle\b|style\b|click\b|direction\b)")
_EVENT = re.compile(r"^\s*[*\[]?[\w.\-]*[*\]]?\s*(\S.*)$")


def _clean(text: str) -> str:
    """The label as a terminal reads it: unquoted, `<br/>` as a separator."""
    text = text.strip().strip("\"'")
    return text.replace("<br/>", " · ").replace("<br>", " · ")


def _split(token: str) -> tuple[str, str]:
    """`Q["Asyncio Queue"]` -> (`Q`, `Asyncio Queue`)."""
    match = re.match(r"^(?P<id>[\w.:\-]+)(?P<shape>.*)$", token)
    assert match is not None, token
    node_id, shape = match.group("id"), match.group("shape")
    # The first and last character are the bracket pair, whatever its width.
    text = shape[1:-1] if len(shape) > 1 else ""
    return node_id, _clean(text)


def _state_to_flow(lines: list[str]) -> list[str]:
    """`stateDiagram-v2`'s transition list as `flowchart` edges.

    A state transition is `A --> B: label`, which is `A -->|label| B`; the two
    diagram kinds then take one path through the tokeniser.
    """
    out = []
    for line in lines:
        body = re.sub(r"\s*;\s*$", "", line.strip())
        head, _, label = body.partition(":")
        if label.strip():
            head = re.sub(r"(-->|-.->)", lambda m: f"{m.group(1)}|{label.strip()}|", head, count=1)
        # Mermaid's `[*]` is the start pseudo-state on the left of an edge and
        # the end one on the right; both need a name to be drawn at all.
        head = re.sub(r"^\s*\[\*\]", "start", head)
        head = re.sub(r"\[\*\]\s*$", "end", head)
        out.append(head)
    return out


@dataclass(frozen=True)
class Diagram:
    """One parsed diagram. `labels` holds nodes only; a subgraph is a `containers`
    entry that names the group its members sit in, never a node of its own."""

    header: str
    labels: dict[str, str]
    containers: dict[str, str]
    groups: dict[str, str]
    edges: list[tuple[str, str, str, str]]
    order: list[str]


def parse(source: str) -> Diagram | None:
    """Read a diagram, or `None` for a kind this does not draw.

    `None` is a fact the caller acts on by leaving the fence alone. A line it
    cannot read is skipped, so one odd directive costs a node rather than the
    whole diagram.
    """
    lines = [line.rstrip() for line in source.split("\n")]
    header = ""
    for line in lines:
        stripped = line.strip()
        if match := _HEAD.match(stripped):
            header = f"{match.group('kind')} {match.group('dir')}"
            break
        if _STATE.match(stripped):
            header = "stateDiagram"
            lines = _state_to_flow(lines[1:])
            break
        if stripped and not stripped.startswith("%%"):
            return None
    if not header:
        return None

    labels: dict[str, str] = {}
    containers: dict[str, str] = {}
    groups: dict[str, str] = {}
    order: list[str] = []
    edges: list[tuple[str, str, str, str]] = []
    stack: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or _SKIP.match(stripped) or _HEAD.match(stripped):
            continue
        if stripped == "end":
            if stack:
                stack.pop()
            continue
        if match := _SUB.match(stripped):
            group = match.group("id")
            # A second `subgraph G` reopens the same group, so the first label
            # stands; an unlabelled group is named by its id, since it still has
            # to be drawn as the tag on its members.
            containers.setdefault(group, _clean(match.group("label") or "") or group)
            if group not in order:
                order.append(group)
            stack.append(group)
            continue

        body = re.sub(r"\s*;\s*$", "", stripped)
        body = _NAMED.sub(lambda m: f" {m.group(3)}|{m.group(2).strip()}| ", body)
        sequence: list[tuple[str, str, tuple[str, str]]] = []
        pending: tuple[str, str] = ("-->", "")
        for token in _TOKEN.finditer(body):
            if token.group("arrow"):
                pending = (token.group("arrow"), _clean(token.group("label") or ""))
            else:
                node_id, text = _split(token.group("node"))
                sequence.append((node_id, text, pending))
                pending = ("-->", "")
        for node_id, text, _ in sequence:
            if text or node_id not in labels:
                labels[node_id] = text or labels.get(node_id, "")
            if node_id not in order:
                order.append(node_id)
            if stack:
                groups.setdefault(node_id, stack[-1])
        for index in range(len(sequence) - 1):
            left, right = sequence[index][0], sequence[index + 1]
            edges.append((left, right[0], right[2][0], right[2][1]))

    return Diagram(header, labels, containers, groups, edges, order)


def draw(source: str, width: int = 70) -> list[str] | None:
    """The diagram as terminal lines, wrapped to `width`, or `None`.

    The graph is walked from every node nothing points at, which is a root. A
    node already printed is marked `(seen)` rather than expanded again: a graph
    is not a tree, and the roadmap's `M2 -.-> M4` edge would otherwise recurse
    for ever. Nothing points at a cycle, so a diagram made only of one is
    started from its first node.

    shortcut: a `LR` or `RL` diagram is drawn as the same vertical tree, because
    the tree is what a terminal reads; upgrade to columns if a horizontal layout
    is ever needed.
    """
    parsed = parse(source)
    if parsed is None:
        return None
    labels, containers, groups, edges, order = (
        parsed.labels, parsed.containers, parsed.groups, parsed.edges, parsed.order)
    if not labels:
        # A header with nothing under it draws a title and no graph; the source
        # says more than a summary of zero.
        return None

    children: dict[str, list[tuple[str, str, str]]] = {}
    incoming: dict[str, int] = {}
    for left, right, arrow, label in edges:
        children.setdefault(left, []).append((right, arrow, label))
        incoming[right] = incoming.get(right, 0) + 1

    printed: set[str] = set()
    lines: list[str] = []

    def label_of(node_id: str) -> str:
        text = labels.get(node_id) or containers.get(node_id) or node_id
        group = groups.get(node_id)
        return f"{text}  [{containers.get(group) or group}]" if group else text

    def emit(text: str, indent: int) -> None:
        """One node line, wrapped with its continuation under the same indent."""
        if len(text) <= width:
            lines.append(text)
            return
        lines.extend(
            textwrap.wrap(text, width, subsequent_indent=" " * (indent + 2), break_long_words=True)
        )

    def walk(node_id: str, prefix: str, branch: str, arrow: str, label: str) -> None:
        # The indent already says there is an edge; the glyph marks what kind it
        # is, so it is only worth a column when it carries a label beside it.
        tag = f" {_GLYPH.get(arrow, '▸')} {label}" if label else ""
        seen = node_id in printed
        printed.add(node_id)
        emit(prefix + branch + label_of(node_id) + tag + (" (seen)" if seen else ""), len(prefix + branch))
        if seen:
            return
        kids = children.get(node_id, [])
        for index, (kid, kid_arrow, kid_label) in enumerate(kids):
            last = index == len(kids) - 1
            walk(kid, prefix + ("   " if branch.startswith("└") else "│  "),
                 "└─ " if last else "├─ ", kid_arrow, kid_label)

    roots = [node for node in order if not incoming.get(node)]
    for root in roots or order[:1]:
        # No `printed` check here: a root has no incoming edge, and only a child
        # can be printed before this loop, so every root is unprinted by construction.
        printed.add(root)
        emit(label_of(root), 0)
        kids = children.get(root, [])
        for index, (kid, kid_arrow, kid_label) in enumerate(kids):
            walk(kid, "", "└─ " if index == len(kids) - 1 else "├─ ", kid_arrow, kid_label)

    return [f"{parsed.header} · {len(labels)} nodes · {len(edges)} edges", *lines]


#: A fence opener for a language, with its optional trailing text.
_FENCE = re.compile(r"^```\s*(?P<lang>[\w-]*)\s*$")


def rewrite(markup: str, width: int = 70) -> str:
    """Replace each drawable `mermaid` fence in `markup` with a drawn one.

    The result is the same Markdown with the fence's language changed to `text`
    and its body swapped for the tree, so Rich renders it as a code block in the
    pane's own monospace style. A fence this module cannot draw, and a fence left
    open at end of input, are copied through unchanged: the reader sees the
    model's source rather than nothing.
    """
    lines = markup.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        match = _FENCE.match(lines[index].strip())
        if not match or match.group("lang") != "mermaid":
            out.append(lines[index])
            index += 1
            continue
        close = index + 1
        while close < len(lines) and lines[close].strip() != "```":
            close += 1
        if close == len(lines):
            out.append(lines[index])
            index += 1
            continue
        drawn = draw("\n".join(lines[index + 1:close]), width)
        if drawn is None:
            out.extend(lines[index:close + 1])
        else:
            out.append("```text")
            out.extend(drawn)
            out.append("```")
        index = close + 1
    return "\n".join(out)

