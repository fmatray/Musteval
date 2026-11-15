# Geo tools

One tool, `geo.locate`, returning the device's position from WiFi and IP signals.
There is **no GPS receiver** on the board: a fix is produced by scanning nearby BSSIDs
and asking a positioning provider, with an IP lookup as fallback (`../02-firmware.md`
§geo, `../00-overview.md` §4.1). The tool is gated: `geo.enabled` must be `true`,
otherwise `E_CONFIG` (`../06-security.md` §6).

| Tool | Gate | Errors |
|---|---|---|
| `geo.locate` | `geo.enabled: true` | `E_CONFIG`, `E_GEO`, `E_TIMEOUT`, `E_TOOL` (schema validation) |

## Provider chain

`geo.providers[]` is an **ordered fallback chain** of 1..4 entries, configured
per-device and never chosen by the model (`../03-config.md` §geo). The first entry
that returns a fix wins; the order is the user's privacy control, because whichever
entry answers learns where the device is.

| `kind` | Verb | Requires | Typical accuracy | Failure handling |
|---|---|---|---|---|
| `wifi_scan` | `POST` | `api_key_ref` (`nvs:` ref) | 20-200 m | try the next `wifi_scan` entry in order, then the first `ip` entry |
| `ip` | `GET` | no `api_key_ref` (forbidden) | city-level | chain exhausted, return `E_GEO`, cache nothing |

Both `url` hosts must be reachable over TLS. `wifi_scan` is a `POST` carrying the
scanned `[{bssid, rssi}]` list; `ip` is a `GET` with no entity, letting the provider
read the caller's address off the connection. The verb is part of the kind, not a
choice: an `ip` provider answers a `POST` with a refusal of its own (ipwho.is returns
405, ipapi.co closes the connection), which surfaces as a chain-wide `E_GEO` naming
neither the provider nor the verb (`geo_kind_posts`, board half `try_provider`). A
`wifi_scan` provider without `api_key_ref`, or an `ip` provider that has one, is
rejected by `geo_init()` at boot as `E_CONFIG`.

The `wifi_scan` credential is sent as `Authorization: Bearer <key>`; that header form
is marked `[ASSUMPTION]` in `../03-config.md` §geo and is not confirmed against a
provider yet.

## `geo.locate`

Returns a position fix: the cached one if it is still fresh, otherwise a new lookup.
No arguments; the provider chain and all bounds come from config, so the model cannot
widen a scan or pick a provider.

Flow when the cache is cold: when some `geo.providers[]` entry is a `wifi_scan`, scan
with `esp_wifi_scan_start`, sort by RSSI and keep the `geo.max_bssids` strongest; an
`ip`-only chain skips the scan entirely, because it has no use for the result and the
scan disturbs the link the request needs (`geo_chain_needs_scan`,
`../02-firmware.md` §geo). Then walk the chain, cache and return the winner. The call is blocking and runs on the agent task, so a cold
lookup holds the agent loop for the duration of the chain (`../02-firmware.md` §3.3).

**Time bounds.** `geo.timeout_ms` (default 5000) applies to **each provider attempt**,
not to the whole chain. Worst case is therefore roughly `geo.timeout_ms` multiplied by
the number of configured providers - 20 s with the default budget and four providers -
and it does not depend on `geo.max_bssids`: the scan bound shrinks the payload, not
the number of attempts.

 **Cache.** A fix is held in RAM for `geo.cache_ttl_s` (default 900) and reused while
 fresh, so repeated calls do not mean repeated disclosure to the provider. The
 cache is bounded by `geo.cache_ttl_s` alone: a change in RSSI does not invalidate it.
 The cache is RAM-only, so a reboot costs one scan. A failed lookup is not cached:
 the next call retries the full chain.

**Accuracy.** `accuracy_m` is exactly what the provider reported; firmware does not
compute or refine it. Treat it as a hint, not a guarantee - a BSSID-derived fix
locates the access point, which is not necessarily where the device is
(`../06-security.md` §6.4). A caller that needs precision has no better value to fall
back on in this firmware.

**Result trust.** The provider response is untrusted input, and the result narrows it
to typed fields before it reaches the prompt: `lat` and `lon` are parsed numbers,
`accuracy_m` and `source_ts` are integers, and `provider` is the configured entry's
`id`, never a string the provider chose (`../03-config.md` §geo). A body that
does not carry both coordinates is refused rather than defaulted (`geo_parse_fix`), so
a provider cannot put prose, a coordinate it did not report, or a redirect into a
`geo.locate` result.

**Arguments**

| Name | Type | Required | Bounds |
|---|---|---|---|
| - | - | - | The schema declares no properties. |

**Result**

```json
{"lat": 52.3676, "lon": 4.9041, "accuracy_m": 65, "source_ts": 1730563520, "provider": "wifi_beacon", "city": "Amsterdam", "region": "North Holland", "region_code": "NH", "country": "Netherlands", "country_code": "NL", "country_code_iso3": "NLD", "country_capital": "Amsterdam", "country_tld": ".nl", "continent_code": "EU", "in_eu": true, "postal": "1012", "country_calling_code": "+31", "timezone": "Europe/Amsterdam", "utc_offset": "+01:00", "currency": "EUR", "currency_name": "Euro", "languages": "nl-NL,fy-NL"}
```

`provider` is the `geo.providers[].id` that produced the fix. `source_ts` is the Unix
second the fix was obtained — the local receive time, stamped when the response parsed,
because a provider is not trusted to report a usable clock — and it is the reference
point the cache age is measured against.

The place and locale members (`city` through `languages`) are the same set
`04-protocol.md` §4.13 documents for the command surface, and the same rule applies:
each is present only when the provider reported it. The `ip` provider in the seed config
sends neither currency nor currency name nor languages, so a result from it carries the
geography and omits those three. They are informational: a model reading them learns
where the device is, which is the point of the tool, and `06-security.md` §6.4 already
states that a provider sees the request.

**Schema sent to the model**

```json
{"type": "object", "properties": {}}
```

The bridge-side `geo.locate` command is a separate surface with a different result
shape (`{lat, lon, accuracy_m, provider, age_s}`, `../04-protocol.md` §4.13); the tool
documented here is the one the model calls.

## Errors

- `E_CONFIG` - `geo.enabled` is `false`; refused before any scan, so nothing leaves
  the device (`../06-security.md` §6).
- `E_CONFIG` - a provider fails `geo_init()` validation: missing `api_key_ref` on
  `wifi_scan`, an `api_key_ref` on `ip`, a non-`https` URL, or an unresolved `nvs:`
  ref (`../03-config.md` §geo).
- `E_GEO` - every provider in the chain failed; no fix is cached and the previous
  cached fix, if any, is not returned. The message names the last provider and what it
  did (`geo_last_error()`, `"<id>: <err> (http <status>)"`, `../02-firmware.md` §geo),
  because `E_GEO` alone cannot distinguish a DNS miss from a refused handshake from a
  provider that answered `500`, and those are three different repairs. `E_GEO` stays
  the code so the wire contract does not move.
- `E_TIMEOUT` - the **last** provider attempt in the chain exceeded `geo.timeout_ms`
  (`../02-firmware.md` §geo), and no later provider produced a fix.
  **Failure handling.** A provider that exceeds its attempt budget or fails to respond
  within `geo.timeout_ms` is considered failed; the chain continues to the next
  configured entry. `E_TIMEOUT` is only returned if the failure was a timeout AND no
  later provider produced a fix — which is to say the chain reports its final outcome,
  not its worst one. A chain whose first provider timed out and whose last refused is
  `E_GEO`: telling the reader to retry would send them into the same refusal.
- `E_TOOL` - the arguments failed schema validation. The canonical schema has no
  properties, so this only fires on a malformed call (`../02-firmware.md` §7.1).

## See also

- `../03-config.md` §geo - the full key table, defaults, bounds and the example chain.
- `../02-firmware.md` §geo - `geo_init()` and the blocking `geo_locate()` contract.
- `../00-overview.md` §4.1 - why there is no GPS and what each provider kind sends.
- `../06-security.md` §6.4 - positioning responses treated as untrusted prompt input.
- `../00-overview.md` §7 - the frozen list of built-in tool names.
- `../04-protocol.md` §3.12 / §3.13 - the `agent.tool` and `agent.tool_result` frames
  this result travels in.
