# API Reference

Base URL: `http://<host>:<port>/api`

Versioned aliases are available for selected endpoints under `/api/v1`.

All endpoints return JSON. Errors return `{ "error": "<message>" }` with an appropriate HTTP status code.

API routes are protected by a lightweight in-memory rate limiter by default.
Configure via environment variables:
- `API_RATE_LIMIT_ENABLED` (default: `true`)
- `API_RATE_LIMIT_MAX_REQUESTS` (default: `240`)
- `API_RATE_LIMIT_WINDOW_SECONDS` (default: `60`)

---

## Health

| Method | Path           | Description         |
| ------ | -------------- | ------------------- |
| GET    | `/api/health`  | Health check        |
| GET    | `/api/v1/health` | Versioned health check |
| GET    | `/api/version` | Application version |

---

## Channels

| Method | Path                                  | Description                                                   |
| ------ | ------------------------------------- | ------------------------------------------------------------- |
| GET    | `/api/channels`                       | List all channels with profile assignments                    |
| GET    | `/api/v1/channels`                    | Versioned alias for channel listing                           |
| GET    | `/api/channels/groups`                | List channel groups                                           |
| GET    | `/api/channels/<id>/stats`            | Stream count, dead streams, resolution, bitrate for a channel |
| GET    | `/api/v1/channels/<id>/stats`         | Versioned alias for channel stats                             |
| GET    | `/api/channels/logos/<logo_id>`       | Logo metadata                                                 |
| GET    | `/api/channels/logos/<logo_id>/cache` | Cached logo image                                             |
| POST   | `/api/channels/<id>/match-settings`   | Update match settings (`match_by_tvg_id`)                     |

---

## Regex Patterns

| Method | Path                               | Description                                |
| ------ | ---------------------------------- | ------------------------------------------ |
| GET    | `/api/regex-patterns`              | All regex patterns                         |
| GET    | `/api/v1/regex-patterns`           | Versioned alias for regex pattern listing  |
| POST   | `/api/regex-patterns`              | Add or update a pattern for a channel      |
| POST   | `/api/v1/regex-patterns`           | Versioned alias for adding/updating pattern |
| DELETE | `/api/regex-patterns/<channel_id>` | Delete patterns for a channel              |
| DELETE | `/api/v1/regex-patterns/<channel_id>` | Versioned alias for deleting channel patterns |
| POST   | `/api/regex-patterns/bulk`         | Add the same patterns to multiple channels |
| POST   | `/api/v1/regex-patterns/bulk`      | Versioned alias for bulk regex assignment  |

---

## Automation Config

| Method | Path                                    | Description                              |
| ------ | --------------------------------------- | ---------------------------------------- |
| GET    | `/api/automation/config`                | Global automation settings               |
| POST   | `/api/automation/config`                | Update global settings                   |
| GET    | `/api/automation/profiles`              | List profiles                            |
| POST   | `/api/automation/profiles`              | Create profile                           |
| GET    | `/api/automation/profiles/<id>`         | Get profile                              |
| PUT    | `/api/automation/profiles/<id>`         | Update profile                           |
| DELETE | `/api/automation/profiles/<id>`         | Delete profile                           |
| GET    | `/api/automation/periods`               | List periods                             |
| POST   | `/api/automation/periods`               | Create period                            |
| PUT    | `/api/automation/periods/<id>`          | Update period                            |
| DELETE | `/api/automation/periods/<id>`          | Delete period                            |
| POST   | `/api/automation/periods/<id>/assign`   | Assign period to channels with a profile |
| POST   | `/api/automation/periods/<id>/unassign` | Remove period from channels              |
| GET    | `/api/automation/channel-assignments`   | Channel → profile assignments            |
| POST   | `/api/automation/channel-assignments`   | Assign profile to channels               |
| GET    | `/api/automation/group-assignments`     | Group → profile assignments              |
| POST   | `/api/automation/group-assignments`     | Assign profile to group                  |

---

## Stream Checker

| Method | Path                                | Description                                      |
| ------ | ----------------------------------- | ------------------------------------------------ |
| GET    | `/api/stream-checker/status`        | Current checker status and stats                 |
| POST   | `/api/stream-checker/run`           | Trigger a global action (update → match → check) |
| POST   | `/api/stream-checker/check-channel` | Check a single channel                           |
| GET    | `/api/stream-checker/config`        | Stream checker config                            |
| POST   | `/api/stream-checker/config`        | Update stream checker config                     |

---

## Stream Monitoring

| Method | Path                                    | Description                   |
| ------ | --------------------------------------- | ----------------------------- |
| GET    | `/api/monitoring/sessions`              | List monitoring sessions      |
| GET    | `/api/monitoring/sessions/<id>`         | Get session detail            |
| GET    | `/api/monitoring/channels/<id>/history` | Session history for a channel |
| GET    | `/api/monitoring/screenshots/<id>`      | Serve a screenshot            |

---

## Scheduling

| Method | Path                                     | Description                     |
| ------ | ---------------------------------------- | ------------------------------- |
| GET    | `/api/scheduling/events`                 | List scheduled EPG events       |
| POST   | `/api/scheduling/events`                 | Create a scheduled event        |
| DELETE | `/api/scheduling/events/<id>`            | Delete a scheduled event        |
| GET    | `/api/scheduling/config`                 | Scheduling configuration        |
| POST   | `/api/scheduling/config`                 | Update scheduling configuration |
| GET    | `/api/scheduling/auto-create-rules`      | List auto-create rules          |
| POST   | `/api/scheduling/auto-create-rules`      | Create auto-create rule         |
| DELETE | `/api/scheduling/auto-create-rules/<id>` | Delete auto-create rule         |
| GET    | `/api/scheduling/epg`                    | Fetch EPG grid                  |

---

## UDI (Universal Data Index)

| Method | Path               | Description                                     |
| ------ | ------------------ | ----------------------------------------------- |
| POST   | `/api/udi/refresh` | Force refresh of the UDI cache from Dispatcharr |
| GET    | `/api/udi/status`  | Cache status and last refresh time              |

---

## Changelog

| Method | Path             | Description                 |
| ------ | ---------------- | --------------------------- |
| GET    | `/api/changelog` | Recent changelog entries    |
| DELETE | `/api/changelog` | Clear all changelog entries |

---

## Dead Streams

| Method | Path                            | Description                          |
| ------ | ------------------------------- | ------------------------------------ |
| GET    | `/api/dead-streams`             | All tracked dead streams (paginated) |
| DELETE | `/api/dead-streams/<stream_id>` | Remove a stream from the dead list   |
| POST   | `/api/dead-streams/clear`       | Clear all dead streams               |
