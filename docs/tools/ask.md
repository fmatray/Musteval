# Ask tool

Put one question to the person at the bridge and wait for the answer. `ask` is the
only tool whose result comes from a human rather than from the device, so it is the
only one that blocks the agent task and the only one that needs a reply frame.

| Tool | Gate | Errors |
|---|---|---|
| `ask` | none | `E_TIMEOUT`, `E_CANCELLED`, `E_TOOL` |

Gate matrix: `../06-security.md` §6.

## Why it is not a local call

Every other built-in answers from state the device already holds. `ask` cannot: the
question is for the reader, and the reader is on the other end of the USB link. The
device therefore sends `agent.ask` (`../04-protocol.md` §3.22), blocks the agent task,
and finishes the call with the ordinary `agent.tool_result` once `ask.reply` (§3.23)
arrives. The model sees one normal tool call; the round trip is the firmware's
problem, not the prompt's.

The wait has three endings and all of them produce a result, so a run can never hang
on a question nobody answers:

| Ending | Result |
|---|---|
| The human answers | `ok:true`, result carries `answer` |
| The human dismisses the question | `ok:false`, `E_CANCELLED` |
| `agent.ask_timeout_s` elapses | `ok:false`, `E_TIMEOUT` |

`cancel` (`../04-protocol.md` §3.5) cuts the wait short: the call still finishes with
`ok:false` and `E_CANCELLED`, and the run then ends with its own `agent.error`.

A bridge that predates this frame never replies, so the model reads `E_TIMEOUT` and
carries on. That is the intended degradation: an older bridge is less useful, not
broken.

## `ask`

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `question` | string | yes | 1..512 bytes |
| `options` | array of string | no | 0..8 entries, each 1..64 bytes |

`options` is a convenience, not a constraint: the bridge always offers a free-text
answer beside the listed ones, because the question that needed a fifth option is
exactly the case this tool exists for. When `options` is absent the bridge asks for
typed text directly.

A question is a sentence, not a paragraph. The 512-byte bound is what keeps the frame
inside `LINK_TX_MAX` after JSON escaping, and it is also the length a person can read
on a 20-row terminal without scrolling.

**Result**

```json
{"question": "Which branch should the reboot land on?", "answer": "release"}
```

`answer` is the chosen option verbatim, or the typed text trimmed of surrounding
whitespace. A typed answer that trims to nothing is treated as a dismissal, because
the bridge's text prompt returns `None` for an emptied field rather than an empty
string; `04` §3.23 still carries `cancelled` as its own field, because the device
must not have to infer a dismissal from an answer it chose itself.

**Schema sent to the model**

```json
{
  "type": "object",
  "properties": {
    "question": {"type": "string"},
    "options": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["question"]
}
```

## When the model should use it

The prompt states the rule and this file states the reason. A model that guesses at a
detail it could have asked about produces work the reader then has to unwind; a model
that asks before every step produces a conversation instead of an answer. The line is
**irreversibility and ambiguity**: ask when acting on a wrong guess cannot be undone
(deleting, flashing, rebooting, publishing) or when the request names no single thing
to do. Otherwise act and say what was assumed.

## Errors

- `E_TIMEOUT` — no reply before `agent.ask_timeout_s`. The message names the bound, so
  the model knows a human simply was not there rather than that its question was
  rejected.
- `E_CANCELLED` — the human dismissed the question, or the run was cancelled while
  waiting. The model MUST NOT re-ask the same question in the same run; the answer is
  that the reader declined.
- `E_TOOL` — the arguments failed verification: an empty question, more than eight
  options, or an option longer than its bound. The message names the rule. Also
  raised when no `ask` port is installed in this build, and when the question could
  not be put on the link at all: that call fails at once rather than waiting out a
  timeout for a frame the bridge never saw.

## Limits

- One question per call. A form with several fields is not this tool; the bridge
  asks them one after another if the model asks them one after another.
- The timeout is `agent.ask_timeout_s`, clamped to 10..600 seconds. It is a config key
  rather than a constant so a reader can wait longer on a slow decision without a new
  build.
- `ask` is exempt from no other limit: it still counts as one tool call against
  `llm.max_tool_rounds` and one of `AGENT_MAX_TOOL_CALLS` in its round.

## See also

- `../04-protocol.md` §3.22, §3.23 — `agent.ask` and `ask.reply`
- `../05-bridge.md` §10 — the chat surface the question is drawn on
- `../03-config.md` — `agent.ask_timeout_s`
- `../06-security.md` §6 — the gate matrix
