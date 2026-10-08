# Development changelog: restore assignment review

## Changes

- Capture a bounded Dispatcharr identity inventory in new backups, using hashed addresses and URLs instead of plaintext connection secrets in review metadata.
- Persist a restore-review hold across container restarts. Pause automatic work, inventory initialization and normal API actions until assignments are explicitly confirmed.
- Add target connection editing, read-only inventory comparison, searchable provider/channel/group/channel-profile mappings, explicit skip choices and a measurement-retention preview to Backups.
- Detect renamed objects, moved identities and reused numeric IDs; re-fetch the live inventory before confirmation and reject stale comparisons.
- Remap Dispatcharr configuration references without rewriting regex expressions, their order, or StreamFlow profile/period IDs.
- Remove skipped restricted regex rules, disable empty restricted scopes and reject removal of the entire global provider filter.
- Keep quality/playback history only with verified channel and stream identities. Omit embedded monitoring references when inventory identities or IDs change. Preserve aggregate Analytics totals.
- Support legacy archives through manual assignment review, with unverified history excluded from the restored working database.
- Apply confirmed mappings through the existing offline journal, safety backup and rollback mechanism. A failed approval write cancels its unapplied stage.

## Validation

Validation results will be recorded after the local, CI and Unraid DockerMan checks are complete.
