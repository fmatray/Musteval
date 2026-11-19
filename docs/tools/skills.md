# Skills tools

Load the body of one user-authored skill from `/skills/`, so the model can follow
instructions the user wrote rather than ones the firmware compiled in. `skill.read`
has no gate and is always available; the skill tree lives on `userfs`, which no tool
can write. Gate matrix: `../06-security.md` §6.

| Tool | Gate | Errors |
|---|---|---|
| `skill.read` | none, skills are userfs read-only | `E_TOOL` |

## `skill.read`

Reads `/skills/<name>/SKILL.md` and returns the Markdown body: everything after the
leading `---` frontmatter block. The layout is
[agentskills.io](https://agentskills.io/specification): one directory per skill, and
the instructions live in the `SKILL.md` inside it. Frontmatter is parsed when the
tree is scanned at boot and on `reload` (`../03-config.md` §skills); it is never part
of the result.

`name` is the directory name and must match the spec's rule: `[a-z0-9-]{1,64}`, no
leading or trailing hyphen, no consecutive hyphens. A name that does not resolve to
a directory is refused rather than clamped to the nearest match, so the tool cannot
be used to enumerate the tree through error text.

What bounds a skill:

- The skill index is assembled at boot and on `reload` from every
  `/skills/<name>/SKILL.md` frontmatter. One line per skill of the form
  `- <name>: <description>` is appended to the system prompt before history. This
  index counts against `agent.max_input_bytes` like any other prompt text; if the
  system prompt plus index would exceed the cap, skills are dropped from the end of
  the index in name order until it fits, and the omission is logged
  (`../02-firmware.md` §2, `skills`). The body is still loaded only by
  `skill.read`.
- A skill cannot grant tools. `allowed-tools` names tools the body expects; the
  allowlists in `config.json` still decide whether a call is permitted
  (`../03-config.md` §skills, body rule 3).
- Skills are read-only to the agent. `skill.read` is the intended path; `fs.read`
  can also reach them, `fs.write` cannot (`../06-security.md` §6.1).
- The tree is capped at 16 skills and 8 KiB per file `[ASSUMPTION]`
  (`../03-config.md` §Storage limits). `skill.read` refuses a body its result
  buffer cannot carry with `E_TOOL` rather than truncating, so the model never
  receives a silently shortened skill.

The mapped path is `/fs/skills/<name>/SKILL.md`: any agent path outside
`/workspace/...` resolves to `userfs` (`../03-config.md` §1).

**Arguments**

| name | type | required | bounds |
|---|---|---|---|
| `name` | string | yes | skill directory name, `[a-z0-9-]{1,64}`, no `/` |

**Result**

```json
{
  "name": "led-control",
  "body": "# LED control\n\nThe board LED is on GPIO21 and is active-low.\n",
  "bytes": 61
}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}
```

## Errors

- `E_TOOL` — `name` missing or outside the spec's name rule, no
  `/skills/<name>/SKILL.md`, or the read itself failed. This includes a body the
  result buffer cannot carry. Skills carry no separate error domain: any refusal is
  `E_TOOL` (`../06-security.md` §6).

Load-time frontmatter problems are not tool errors. A directory without a readable
`SKILL.md`, or one whose frontmatter `name` does not equal the directory name, is
dropped with a logged warning during validation, so `skill.read` afterwards sees no
such skill and refuses with `E_TOOL` (`../03-config.md` §skills).

## See also

- [Skills: frontmatter schema and body rules](../03-config.md) §skills
- [Storage map and agent-visible paths](../03-config.md#1-storage-map)
