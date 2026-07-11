# API Reference

Base URL: `http://<host>:<port>/api`

Versioned aliases are available for selected endpoints under `/api/v1`. All
endpoints return JSON unless the route explicitly serves an image or stream.
Errors return `{ "error": "<message>" }` with an appropriate HTTP status code.

API routes are protected by a lightweight in-memory rate limiter by default:

- `API_RATE_LIMIT_ENABLED` (default: `true`)
- `API_RATE_LIMIT_MAX_REQUESTS` (default: `240`)
- `API_RATE_LIMIT_WINDOW_SECONDS` (default: `60`)
- `API_RATE_LIMIT_MAX_BUCKETS` (default: `4096`)
- `STREAMFLOW_TRUSTED_PROXY_CIDRS` (default: empty)

`X-Forwarded-For` is ignored unless the direct peer belongs to a configured
trusted proxy network. StreamFlow does not currently provide its own UI/API
login. Treat the API as a trusted-LAN-only control plane and do not expose it
directly to the internet. Adding a StreamFlow authentication boundary is a
separate compatibility decision.

Dispatcharr and Shadow watcher secrets can be supplied without saving them in
StreamFlow configuration files:

- `DISPATCHARR_API_KEY_FILE` or `DISPATCHARR_API_KEY`
- `DISPATCHARR_PASS_FILE` or `DISPATCHARR_PASS`
- `SHADOW_WATCHER_API_KEY_FILE` or `SHADOW_WATCHER_API_KEY`

The `*_FILE` source takes precedence and fails closed if it cannot be read. API
responses report only whether a secret is configured and never return its value.

---

## Health

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/health` | Process and HTTP liveness |
| GET | `/api/v1/health` | Versioned liveness alias |
| GET | `/api/readiness` | Database, schema, configuration, UDI, and service readiness |
| GET | `/api/v1/readiness` | Versioned readiness alias |
| GET | `/api/version` | Application version |

---

## Channels

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/channels` | List channels with profile assignments |
| GET | `/api/v1/channels` | Versioned channel-list alias |
| GET | `/api/channels/groups` | List channel groups |
| GET | `/api/channels/<channel_id>/stats` | Stream count, dead streams, resolution, and bitrate |
| GET | `/api/v1/channels/<channel_id>/stats` | Versioned channel-stats alias |
| GET | `/api/channels/logos/<logo_id>` | Logo metadata |
| GET | `/api/channels/logos/<logo_id>/cache` | Cached logo image |
| POST | `/api/channels/<channel_id>/match-settings` | Update channel match settings |

---

## Regex Patterns

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/regex-patterns` | List regex patterns |
| GET | `/api/v1/regex-patterns` | Versioned listing alias |
| POST | `/api/regex-patterns` | Add or update channel patterns |
| POST | `/api/v1/regex-patterns` | Versioned add/update alias |
| DELETE | `/api/regex-patterns/<channel_id>` | Delete channel patterns |
| DELETE | `/api/v1/regex-patterns/<channel_id>` | Versioned delete alias |
| POST | `/api/regex-patterns/bulk` | Add patterns to multiple channels |
| POST | `/api/v1/regex-patterns/bulk` | Versioned bulk alias |
| GET | `/api/regex-patterns/global-settings` | Read global matching settings |
| PUT | `/api/regex-patterns/global-settings` | Update global matching settings |

---

## Automation

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/automation/status` | Current automation and scheduler state |
| POST | `/api/automation/start` | Start the automation service |
| POST | `/api/automation/stop` | Stop the automation service |
| POST | `/api/automation/abort-run` | Abort the active run cleanly |
| POST | `/api/automation/trigger` | Trigger a configured automation run |
| GET | `/api/automation/config` | Read global automation configuration |
| PUT | `/api/automation/config` | Update global automation configuration |
| GET | `/api/automation/profiles` | List profiles |
| POST | `/api/automation/profiles` | Create a profile |
| GET | `/api/automation/profiles/<profile_id>` | Read a profile |
| PUT | `/api/automation/profiles/<profile_id>` | Update a profile |
| DELETE | `/api/automation/profiles/<profile_id>` | Delete a profile |
| GET | `/api/automation/periods` | List periods |
| POST | `/api/automation/periods` | Create a period |
| GET | `/api/automation/periods/<period_id>` | Read a period |
| PUT | `/api/automation/periods/<period_id>` | Update a period |
| DELETE | `/api/automation/periods/<period_id>` | Delete a period |
| POST | `/api/automation/periods/<period_id>/assign-channels` | Assign channels to a period |
| POST | `/api/automation/periods/<period_id>/remove-channels` | Remove channels from a period |
| GET | `/api/automation/periods/<period_id>/channels` | List period channels |
| POST | `/api/automation/assign/channels` | Assign a profile to channels |
| POST | `/api/automation/assign/groups` | Assign a profile to groups |

---

## Stream Checker

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/stream-checker/status` | Current checker state and statistics |
| POST | `/api/stream-checker/start` | Start queued checks |
| POST | `/api/stream-checker/stop` | Stop active checks |
| GET | `/api/stream-checker/queue` | Read the check queue |
| POST | `/api/stream-checker/queue/clear` | Clear queued work |
| GET | `/api/stream-checker/config` | Read Stream Checker configuration |
| PUT | `/api/stream-checker/config` | Update Stream Checker configuration |
| GET | `/api/stream-checker/hardware-status` | Effective FFmpeg and hardware status |
| POST | `/api/stream-checker/check-channel` | Check one channel |
| POST | `/api/stream-checker/check-stream` | Check one Dispatcharr stream by ID or reference |
| POST | `/api/stream-checker/streams/<int:stream_id>/check` | Check one stream by path ID |
| GET | `/api/stream-checker/streams/<int:stream_id>/last-quality-stats` | Read the latest persisted quality result |

Single-stream checks accept `stream_id`, `id`, `stream_reference`, or
`stream_ref`. They can measure an unassigned Dispatcharr stream while regex
rules are being prepared. A check still consumes provider and analysis capacity.

---

## Shadow Monitor

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/shadow-blank-monitor/config` | Read Continuous Monitoring configuration |
| PUT | `/api/shadow-blank-monitor/config` | Update Continuous Monitoring configuration |
| GET | `/api/shadow-blank-monitor/status` | Viewer, probe, switch, cooldown, and decision state |
| POST | `/api/shadow-blank-monitor/start` | Start Continuous Monitoring |
| POST | `/api/shadow-blank-monitor/stop` | Stop monitoring and release probes |
| POST | `/api/shadow-blank-monitor/run-once` | Run one discovery cycle |
| POST | `/api/shadow-blank-monitor/offline-image/learn` | Learn an offline-image reference |

---

## Teamarr Preflight

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/teamarr-preflight/config` | Read Preflight configuration |
| PUT | `/api/teamarr-preflight/config` | Update Preflight configuration |
| GET | `/api/teamarr-preflight/status` | Candidate, queue, scan, and error state |
| POST | `/api/teamarr-preflight/start` | Start the background service |
| POST | `/api/teamarr-preflight/stop` | Stop and cancel pending work |
| POST | `/api/teamarr-preflight/run-once` | Scan candidates and queue currently due checks |
| POST | `/api/teamarr-preflight/events/force-check` | Force one selected event or team check |

---

## Stream Monitoring

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/stream-sessions` | List sessions |
| POST | `/api/stream-sessions` | Create a session |
| GET | `/api/stream-sessions/<session_id>` | Read a session |
| POST | `/api/stream-sessions/<session_id>/start` | Start a session |
| POST | `/api/stream-sessions/<session_id>/stop` | Stop a session |
| DELETE | `/api/stream-sessions/<session_id>` | Delete an inactive session |
| GET | `/api/stream-sessions/<session_id>/alive-screenshots` | List current alive screenshots |

---

## Scheduling

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/scheduling/config` | Read scheduling configuration |
| PUT | `/api/scheduling/config` | Update scheduling configuration |
| GET | `/api/scheduling/epg/grid` | Read the EPG grid |
| GET | `/api/scheduling/events` | List scheduled events |
| POST | `/api/scheduling/events` | Create an event |
| DELETE | `/api/scheduling/events/<event_id>` | Delete an event |
| GET | `/api/scheduling/auto-create-rules` | List auto-create rules |
| POST | `/api/scheduling/auto-create-rules` | Create an auto-create rule |
| PUT | `/api/scheduling/auto-create-rules/<rule_id>` | Update an auto-create rule |
| DELETE | `/api/scheduling/auto-create-rules/<rule_id>` | Delete an auto-create rule |

---

## Dispatcharr Cache / UDI

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/dispatcharr/initialization-status` | Cache initialization progress |
| POST | `/api/dispatcharr/initialize-udi` | Initialize or rebuild the cache |
| GET | `/api/scheduling/udi-refresh/status` | Scheduled UDI worker status |
| POST | `/api/scheduling/udi-refresh/trigger` | Trigger the configured UDI refresh path |

---

## Changelog and Dead Streams

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/changelog` | Recent retained changelog entries |
| GET | `/api/changelog/<int:run_id>/export` | Export one run as JSON |
| GET | `/api/dead-streams` | List tracked dead streams |
| GET | `/api/dead-streams/export` | Export tracked dead streams |
| POST | `/api/dead-streams/revive` | Mark selected streams for revival |
| POST | `/api/dead-streams/clear` | Clear tracked dead streams |

---

## Telemetry

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/telemetry/global` | Runs retained during the last seven days |
| GET | `/api/telemetry/providers` | Provider quality and availability summary |
| GET | `/api/telemetry/channels/list` | Channels with retained telemetry |
| GET | `/api/telemetry/channels/<int:channel_id>` | Retained history for one channel |

Provider telemetry uses `availability_percentage`. The misspelled V7 field
`availability_pecentage` remains temporarily available with the same value for
compatibility.
