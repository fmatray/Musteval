# Web tools

Outbound HTTP from the device: the built-in `http.get`, declarative tools compiled
from `/tools/*.json` at load, and the two declarative tools that ship in the default
`userfs` image (`weather_now`, `web_search`). Every one of them is bounded by the
`tools.http_allow_hosts` hostname allowlist and by certificate-verified TLS
(`../06-security.md` §5). There is no config key to disable verification.

TLS enforcement and scheme enforcement are both frozen: certificate verification
cannot be disabled (`../06-security.md` §5), and every HTTP tool accepts `https`
only, refusing an `http://` URL with `E_TOOL` even when its host is allowlisted
(`../03-config.md` §tools). The LLM provider path is the one place `http://` remains
legal, because `../03-config.md` §llm permits it for a local Ollama; that path is
not an HTTP tool and does not pass through this allowlist.

Gate: URL host must exactly match an entry in `tools.http_allow_hosts`; the authoritative
matrix is `../06-security.md` §6.

| Tool | Gate | Errors |
|---|---|---|
| `http.get` | host exactly matches entry in `tools.http_allow_hosts` | `E_TOOL` on a host miss, no request made |
| `tools.<name>` (declarative, `/tools/*.json`) | same allowlist; `nvs:` header refs must resolve at load | `E_TOOL` on a host miss or bad call, `E_CONFIG` at load |
| `weather_now` | `api.open-meteo.com` in allowlist; keyless | `E_TOOL` as any declarative tool |
| `web_search` | `api.search.brave.com` in allowlist and `brave_key` in NVS `secrets` | `E_CONFIG` at load when the key is absent, `E_TOOL` at call |

## `http.get`

Fetches one URL and returns the status line, the response headers and the body. It is
the escape hatch for a host the user has already allowlisted; it is not a general
browser. The host of `url` MUST match an entry in `tools.http_allow_hosts` exactly --
no wildcard, no subdomain match, no redirect following to a second host `[ASSUMPTION]`
-- and a miss is refused with `E_TOOL` before any socket is opened
(`../06-security.md` §6). Scheme is `https`; `http://` targets are refused
(`../06-security.md` §4, T8, marked `[ASSUMPTION]` there).

The body is returned as received; there is no projection. A caller that wants one
field of a large JSON document should use a declarative tool with `http.response_path`
instead of pulling the whole body through `http.get` and parsing it in the model.
The 4 KiB result cap is stated explicitly for declarative tools
(`../03-config.md` §tools, binding step 4); `[ASSUMPTION]` the same model-facing cap
applies here.

Header values supplied in `headers` are sent as given. Do not pass a credential that
way: `http.get` arguments are emitted verbatim in `agent.tool`
(`../04-protocol.md` §3.12) and an argument is visible to the model. Only a
declarative tool's `nvs:` header ref is resolved out of the model's sight
(`../06-security.md` §7).

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `url` | string | yes | absolute `https` URL whose host is in `tools.http_allow_hosts`; `http://` is refused with `E_TOOL` |
| `headers` | object | no | string to string; request headers added to the outbound request |

**Result**

```json
{"status":200,"headers":{"content-type":"application/json"},"body":"{\"current\":{\"temperature_2m\":4.1}}"}
```

`status` is the HTTP status code as an integer, `headers` is the response header map,
and `body` is the raw response text `[ASSUMPTION]`. A non-2xx status is a successful
tool call, not an error: the model sees the code and decides what to do with it.

**Schema sent to the model**

```json
{"type":"object","properties":{"url":{"type":"string"},"headers":{"type":"object"}},"required":["url"]}
```

## Declarative tools

Every `/tools/<name>.json` becomes a tool whose name is the file stem; the optional
`name` key must equal that stem. Declarative tools are HTTP-only in v1. The built-in
names are always present and cannot be redefined -- a file whose stem matches a
built-in fails validation and is dropped, so the name keeps resolving to the built-in
(`../03-config.md` §tools).

The authoring contract, summarised. The authoritative key table, defaults and limits
live in `../03-config.md` §tools:

| Key | Required | Effect on the call |
|---|---|---|
| `description` | yes | 1..300 chars, the only text the model sees about the tool |
| `parameters` | yes | JSON Schema subset handed to the provider |
| `http.method` | no | `GET` (default) or `POST` |
| `http.url` | yes | fixed base URL; host must be in `tools.http_allow_hosts` |
| `http.headers` | no | request headers; values may be `{{arg}}` placeholders or an `nvs:<key>` ref |
| `http.query` | no | appended as a query string; values may be `{{arg}}` placeholders |
| `http.body` | no | JSON body for `POST`; values may be `{{arg}}` placeholders |
| `http.timeout_ms` | no | per-request deadline, default 10000 |
| `http.response_path` | no | dotted path into the response JSON; the tool result is that subtree |

Parameter binding, in the order it matters to whoever calls the tool:

- `{{arg}}` placeholders in `url`, `query`, `headers` and `body` are replaced with the
  model-supplied argument of that name. Only names declared in
  `parameters.properties` may be bound; an unknown argument is rejected with `E_TOOL`
  before any request is made.
- With `http.response_path` set, the response is parsed as JSON and the dotted path
  is selected (for example `web.results`). A path that does not resolve is `E_TOOL`
  and no partial body is returned, so a changed upstream shape fails loudly instead
  of feeding the model a fragment. Without `response_path` the raw body is the
  result.
- The result is capped at 4 KiB before it reaches the model; the rest is dropped and
  a `truncated: true` note is appended. Truncating a **projected array** cuts it
  mid-element, which yields invalid JSON, so a projection that can exceed 4 KiB must
  be bounded upstream through a request parameter -- `web_search` sends `count: 5`
  for exactly this reason.
- A header value of the exact form `nvs:<key>` is resolved from NVS namespace
  `secrets` by the HTTP layer *after* header assembly. The tool-argument object only
  ever holds the literal ref, so `agent.tool` and `agent.tool_result` show the model
  `nvs:brave_key`, not the key, and the resolved value appears in the outbound header
  only (`../06-security.md` §7, T18). An unresolvable ref is `E_CONFIG` at validation,
  not a call-time error: the tool never loads.

The schema subset is deliberately narrow: `type: "object"` at the root, then
`properties`, `required`, and `enum` for a closed value set. No `oneOf`, no `$ref`,
no top-level scalars. Built-ins obey the same rule; the two that nest are `task.set`
and `todo` (`../02-firmware.md` §7.1).

## `weather_now`

Calls Open-Meteo on `api.open-meteo.com` and is keyless: no setup, no NVS entry. It
takes `latitude` and `longitude` and returns the current temperature subtree.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `latitude` | number | yes | decimal degrees |
| `longitude` | number | yes | decimal degrees |

**Result**

```json
{"current":{"temperature_2m":18.4}}
```

**Schema sent to the model**

```json
{"type":"object","properties":{"latitude":{"type":"number","description":"Latitude in decimal degrees."},"longitude":{"type":"number","description":"Longitude in decimal degrees."}},"required":["latitude","longitude"]}
```

## `web_search`

Calls a Brave-compatible search endpoint on `api.search.brave.com` and needs
`brave_key` in NVS namespace `secrets`, set with `secret.set` (`../03-config.md` §tools).
The key is referenced as `nvs:brave_key` in `http.headers`, never stored in the JSON
file. Without it the tool fails to **load**, because an unresolvable `nvs:` ref is
`E_CONFIG` at validation -- the run continues with one fewer tool, and no call is
attempted. `response_path` selects `web.results` so the model receives the results
array rather than the provider envelope.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| `query` | string | yes | 1..200 chars |

**Result**

```json
[{"title":"ESP32-S3 datasheet","url":"https://example.com/esp32s3","description":"Espressif documentation for the ESP32-S3."}]
```

**Schema sent to the model**

```json
{"type":"object","properties":{"query":{"type":"string","description":"Search query, 1..200 chars."}},"required":["query"]}
```

## Shipped tools

`weather_now` and `web_search` are ordinary `/tools/*.json` files in the default
`userfs` image -- not built-ins, nothing special about them except that they are
already there. Both are subject to the same allowlist and the same validation as any
file the user writes, and both can be deleted (`../00-overview.md` §7).

Editing or replacing either file means importing it with the host-only `fs.put`
command (`../04-protocol.md` §4.16) or reflashing a `userfs` image; `reload` only
re-reads what is already on flash. A path that ships in the image refuses the import
with `E_CONFIG`, so a built-in declarative tool is replaceable only by reflash. The
image is produced by `bridge/tools/build_userfs.py`, which builds and flashes it from
a source directory; the script is an M4 deliverable and does not exist yet
(`../03-config.md` §Open questions, item 8).

## Errors

- `E_TOOL` -- the URL host is absent from `tools.http_allow_hosts`. Refused before
  any request; the model gets `ok:false` and the run continues
  (`../04-protocol.md` §3.13).
- `E_TOOL` -- an argument not declared in `parameters.properties` was supplied, or
  the arguments failed schema validation; nothing is executed
  (`../02-firmware.md` §7.1).
- `E_TOOL` -- `http.response_path` did not resolve in the response body; the result
  carries no partial body.
- `E_CONFIG` -- a `tools.<name>.json` file has an unresolvable `nvs:` header ref.
  Detected at load: the tool is dropped, not failed at call time
  (`../03-config.md` §Validation rules, check 10).
- `E_TIMEOUT` -- the request exceeded `http.timeout_ms`. `[ASSUMPTION]` the tool
  result carries the code rather than the run aborting.
- `E_WIFI` -- the request was attempted while Wi-Fi was down. `[ASSUMPTION]` this
  surfaces as the tool result when the network has not recovered
  (`../02-firmware.md` §4.2).

## See also

- `../03-config.md` §tools -- the declarative key table, binding rules and shipped
  file examples.
- `../02-firmware.md` §7.1 -- the built-in tool table with gates and result shapes.
- `../02-firmware.md` §Tool definitions sent to the model -- how built-ins,
  declarative tools and MCP tools are assembled into `tools_json`.
- `../06-security.md` §5 -- TLS policy: verification always on, no insecure path.
- `../06-security.md` §6 -- authorization model; §7 -- redaction rules and T18.
- `../04-protocol.md` §3.12, §3.13 -- the `agent.tool` and `agent.tool_result`
  frames.
- `../00-overview.md` §7 -- the frozen set of built-in tool names.
