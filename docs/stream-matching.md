# Stream Matching

## Overview

Stream matching assigns streams from M3U playlists to channels in Dispatcharr. Each channel can be matched using **regex patterns**, **TVG-ID**, or both. Matching runs as the second step of the automation pipeline.

---

## Regex Patterns

Each channel can have multiple regex patterns. Patterns are stored in `channel_regex_config.json`.

### Pattern format

```json
{
  "patterns": {
    "42": {
      "name": "CNN",
      "enabled": true,
      "match_by_tvg_id": false,
      "regex_patterns": [
        { "pattern": ".*CNN.*", "m3u_accounts": null },
        { "pattern": ".*CNN HD.*", "m3u_accounts": [1, 3] }
      ]
    }
  }
}
```

| Field             | Description                                                            |
| ----------------- | ---------------------------------------------------------------------- |
| `enabled`         | Whether this channel participates in regex matching                    |
| `match_by_tvg_id` | Enable TVG-ID matching for this channel                                |
| `regex_patterns`  | List of pattern objects                                                |
| `pattern`         | Python regex string                                                    |
| `m3u_accounts`    | List of M3U account IDs this pattern applies to. `null` = all accounts |

### Pattern rules

- Matching is case-sensitive by default (global setting in `channel_regex_config.json`)
- Literal spaces in patterns are converted to `\s+` automatically — flexible whitespace matching
- If `match_by_tvg_id` is enabled, **catch-all patterns** (`.*`, `^.*$`, `.+`, `^.+$`) are ignored to prevent unintended mass-matching

Monitoring sessions combine a channel's patterns into an OR filter. Leading
Python flags such as `(?i)` or `(?m)` apply only to their own alternative when
combined, so one pattern cannot change another pattern's flag settings. Stored
patterns and the regular provider-aware matching rules are not rewritten.

### `CHANNEL_NAME` variable

Use `CHANNEL_NAME` in a pattern to substitute the channel's actual name at match time:

```
.*CHANNEL_NAME.*
```

Useful for mass-assigning a template pattern to many channels.

---

## TVG-ID Matching

When `match_by_tvg_id: true` is set on a channel, StreamFlow also checks whether a stream's TVG-ID matches the channel's TVG-ID in Dispatcharr. TVG matching is evaluated before regex by default.

Priority order is configurable per-channel (TVG first or regex first).

---

## Mass Regex Assignment

Assign a pattern to multiple channels at once from the UI (**Channel Configuration → Mass Regex**). Supports per-pattern M3U account filtering and the `CHANNEL_NAME` variable.

---

## Stream Validation

When `validate_existing_streams: true` is set on an Automation Profile, streams currently assigned to a channel are checked against the channel's patterns after each match step. Streams that no longer match any pattern are removed.

This setting is per-profile and only applies when the matching step is enabled for that profile.
