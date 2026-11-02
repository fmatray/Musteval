# Skills tools

Load the body of one user-authored skill from `/skills/`, so the model can follow
instructions the user wrote rather than ones the firmware compiled in. `skill.read`
has no gate and is always available; the skill tree lives on `userfs`, which no tool
can write. Gate matrix: `../06-security.md` §6.

| Tool | Gate | Errors |
|---|---|---|
| `skill.read` | none, skills are userfs read-only | `E_TOOL` |

## `skill.read`

Reads `/skills/<name>.md` and returns the Markdown body: everything after the
leading `---` frontmatter block. Frontmatter is parsed when the tree is scanned at
boot and on `reload` (`../03-config.md` §skills); it is never part of the result.

`name` is the file stem and must match `[a-z0-9_-]{1,32}`. A name that does not
resolve to a file is refused rather than clamped to the nearest match, so the tool
cannot be used to enumerate the directory through error text.

Three properties of skills bound what this tool can do:

 The skill index is assembled at boot and on `reload` from every `/skills/*.md`
 frontmatter. One line per skill of the form `- <name>: <description>` is
 appended to the system prompt before history. This index counts against
 `agent.max_input_bytes` like any other prompt text; if the system prompt plus
 index would exceed the cap, skills are dropped from the end of the index in
 name order until it fits, and the omission is logged (`../02-firmware.md` §2,
 `skills`). The body is still loaded only by `skill.read`.
1. A skill cannot grant tools. Frontmatter `tools` only names tools the body
   expects; the allowlists in `config.json` still decide whether a call is
   permitted (`../03-config.md` §skills, body rule 3).
2. Skills are read-only to the agent. `skill.read` is the intended path; `fs.read`
   can also reach them, `fs.write` cannot (`../06-security.md` §6.1).
 3. The tree is capped at 16 skills and 8 KiB per file `[ASSUMPTION]`
   (`../03-config.md` §Storage limits). `skill.read` refuses a body larger than
   `max_body_bytes` (default 4096) with `E_TOOL` rather than truncating, so the
   model never receives a silently shortened skill.

The mapped path is `/fs/skills/<name>.md`: any agent path outside `/workspace/...`
resolves to `userfs` (`../03-config.md` §1).

**Arguments**

| name | type | required | bounds |
|---|---|---|---|
| `name` | string | yes | file stem of a skill, `[a-z0-9_-]{1,32}`, no extension, no `/` |

**Result**

```json
{
  "body": "# LED control\n\nThe board LED is on GPIO21 and is active-low.\n"
}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}
```

## Errors

 - `E_TOOL` — `name` missing or outside `[a-z0-9_-]{1,32}`, no `/skills/<name>.md`,
   or the read itself failed. This includes bodies exceeding `max_body_bytes`.
   Skills carry no separate error domain: any refusal is `E_TOOL`
   (`../06-security.md` §6).

Load-time frontmatter problems are not tool errors. A file whose frontmatter `name`
does not equal its stem is dropped with a logged warning during validation, so
`skill.read` afterwards sees no such skill and refuses with `E_TOOL`
(`../03-config.md` §Validation rules, rule 11).
 No single byte cap applies to every tool result (as stated in `README.md`);
 instead, each tool defines its own bounds.

 ## See also
 - [Skills: frontmatter schema and body rules](../03-config.md) §skills
 - [Storage map and agent-visible paths](../03-config.md#1-storage-map)
 - [Storage limits](../03-config.md) §Storage limits
 - [Validation rules](../03-config.md) §Validation rules
 - [Authorization model](../06-security.md#6-authorization-model)
 - [Workspace confinement](../06-security.md#61-workspace-confinement)
 - [Module map](../02-firmware.md#1-module-map)
 - [Tool surface index](README.md)
 - [Skills API](../02-firmware.md#skills)
## See also

- [Skills: frontmatter schema and body rules](../03-config.md) §skills
- [Storage map and agent-visible paths](../03-config.md#1-storage-map)
- [Storage limits](../03-config.md) §Storage limits
- [Validation rules](../03-config.md) §Validation rules
- [Authorization model](../06-security.md#6-authorization-model)
- [Workspace confinement](../06-security.md#61-workspace-confinement)
- [Module map](../02-firmware.md#1-module-map)
- [Tool surface index](README.md)
