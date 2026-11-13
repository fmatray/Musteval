"""`bridge/mermaid.py`: a fenced diagram drawn instead of printed (`05` 5.1).

The module is pure, so every case here is a string in and lines out. The two
rules that matter are that a drawable fence becomes a tree whose nodes and edge
labels are all present, and that a fence this cannot draw stays exactly as the
model wrote it — a wrong picture is worse than the source.
"""

from __future__ import annotations

from bridge import mermaid

FLOW = """flowchart TD
  B[boot] --> P[parse config.json]
  P -->|syntax or range error in x| E1[E_CONFIG at boot]
  P -->|ok| S[scan prompts, skills, tools, tasks]
  S --> V[validate secret refs against NVS secrets]
  V -->|unresolved nvs:key| E1
  V -->|ok| R[agent ready]
  RL[reload command] --> P
"""


def test_every_node_and_edge_label_survives_the_draw():
    lines = mermaid.draw(FLOW)

    assert lines is not None
    text = "\n".join(lines)
    # Every display label, in the order the source declares them.
    for label in (
        "boot",
        "parse config.json",
        "E_CONFIG at boot",
        "scan prompts, skills, tools, tasks",
        "validate secret refs against NVS secrets",
        "agent ready",
        "reload command",
    ):
        assert label in text, label
    # An edge label is what tells two edges into the same node apart, so losing
    # one would draw a graph that reads differently from the source.
    assert "syntax or range error in x" in text
    assert "unresolved nvs:key" in text
    assert lines[0] == "flowchart TD · 7 nodes · 7 edges"


def test_a_node_reached_twice_is_marked_rather_than_expanded_again():
    lines = mermaid.draw(FLOW)

    assert lines is not None
    text = "\n".join(lines)
    # `E1` is pointed at by two edges; the second visit says so instead of
    # printing the node a second time. Without this a cycle recurses for ever.
    assert text.count("(seen)") == 2
    assert text.count("E_CONFIG at boot") == 2
    # The second visit keeps its own edge label, so the two edges stay distinct.
    assert "▸ unresolved nvs:key (seen)" in text


def test_a_cycle_is_drawn_from_its_first_node():
    cycle = "flowchart LR\n  A[one] --> B[two]\n  B --> A\n"

    lines = mermaid.draw(cycle)

    assert lines is not None
    text = "\n".join(lines)
    assert lines[0] == "flowchart LR · 2 nodes · 2 edges"
    assert "one" in text and "two" in text
    assert "(seen)" in text


def test_a_subgraph_name_labells_its_nodes():
    grouped = """flowchart TD
  subgraph BR["Bridge Process"]
    RXT["Reader"] --> Q["Queue"]
  end
"""

    lines = mermaid.draw(grouped)

    assert lines is not None
    text = "\n".join(lines)
    assert "Reader  [Bridge Process]" in text
    # A node outside every subgraph carries no group tag, so the tag is the
    # membership fact and not decoration on every row.
    assert "Queue  [Bridge Process]" in text


def test_a_dotted_edge_and_a_bidirectional_edge_keep_their_kind():
    kinds = 'flowchart LR\n  D -. "SNTP" .-> N\n  T <-->|"USB CDC"| L\n'

    lines = mermaid.draw(kinds)

    assert lines is not None
    text = "\n".join(lines)
    assert "┈▸ SNTP" in text
    assert "↔ USB CDC" in text


def test_a_state_diagram_draws_its_transitions():
    states = """stateDiagram-v2
  [*] --> LINK_DOWN
  LINK_DOWN --> LINK_WAIT: host opens CDC
  LINK_WAIT --> LINK_UP: hello / hello_ack
"""

    lines = mermaid.draw(states)

    assert lines is not None
    text = "\n".join(lines)
    assert lines[0] == "stateDiagram · 4 nodes · 3 edges"
    assert "▸ host opens CDC" in text
    assert "▸ hello / hello_ack" in text


def test_a_kind_it_cannot_draw_is_refused_rather_than_guessed():
    assert mermaid.draw("sequenceDiagram\n  A->>B: hi\n") is None
    assert mermaid.draw("classDiagram\n  class A\n") is None
    assert mermaid.draw("flowchart TD\n") is None


def test_a_long_label_wraps_instead_of_being_cut():
    wide = "flowchart TD\n  A --> B[the quick brown fox jumps over the lazy dog and keeps running]\n"

    lines = mermaid.draw(wide, width=40)

    assert lines is not None
    # Nothing is dropped: the wrap rejoins to the whole label. The wrap inserts
    # an indent, so the rejoin collapses runs of spaces rather than one pair.
    joined = " ".join(" ".join(lines).split())
    assert "the quick brown fox jumps over the lazy dog and keeps running" in joined
    assert all(len(line) <= 40 for line in lines), [len(line) for line in lines]


def test_rewrite_draws_the_fence_and_leaves_every_other_line_alone():
    markup = "Before.\n\n```mermaid\nflowchart LR\n  A[host] --> B[board]\n```\n\nAfter.\n"

    out = mermaid.rewrite(markup)

    assert out.startswith("Before.\n\n```text\n")
    assert out.endswith("\n```\n\nAfter.\n")
    body = out.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert body.startswith("flowchart LR · 2 nodes · 1 edges")
    assert "host" in body and "board" in body


def test_rewrite_copies_through_a_fence_it_cannot_draw():
    markup = "```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n"

    assert mermaid.rewrite(markup) == markup


def test_rewrite_copies_through_a_fence_with_no_closing_line():
    markup = "```mermaid\nflowchart LR\n  A --> B\n"

    assert mermaid.rewrite(markup) == markup


def test_rewrite_leaves_a_non_mermaid_fence_alone():
    markup = "```python\nprint(1)\n```\n"

    assert mermaid.rewrite(markup) == markup


def test_rewrite_draws_a_second_fence_in_the_same_answer():
    markup = (
        "```mermaid\nflowchart LR\n  A --> B\n```\n"
        "text between\n"
        "```mermaid\nflowchart TD\n  C --> D\n```\n"
    )

    out = mermaid.rewrite(markup)

    assert out.count("```text") == 2
    assert "text between" in out


def test_a_comment_before_the_header_is_not_a_kind():
    commented = "%% the boot path\nflowchart LR\n  A --> B\n"

    lines = mermaid.draw(commented)

    assert lines is not None
    assert lines[0] == "flowchart LR · 2 nodes · 1 edges"


def test_a_body_of_comments_only_is_refused():
    assert mermaid.draw("%% nothing here\n%% still nothing\n") is None


def test_an_unmatched_end_does_not_close_the_wrong_group():
    loose = "flowchart TD\n  end\n  A[one] --> B[two]\n"

    lines = mermaid.draw(loose)

    assert lines is not None
    # `end` with no open `subgraph` is a no-op; if it popped an empty stack it
    # would raise, and if it ended a group nothing opened, the nodes below it
    # would land in a group that does not exist.
    assert "one" in "\n".join(lines)


def test_a_group_named_twice_keeps_its_members_under_it():
    twice = """flowchart TD
  subgraph G["Group"]
    A --> B
  end
  subgraph G
    C --> D
  end
"""

    lines = mermaid.draw(twice)

    assert lines is not None
    text = "\n".join(lines)
    # Four members, two roots, and the label from the first declaration is the
    # one that survives: a second `subgraph G` is the same group reopened.
    assert "Group" in text
    assert lines[0] == "flowchart TD · 4 nodes · 2 edges"
