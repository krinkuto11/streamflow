# StreamFlow

[![Docker Pulls](https://ghcr-badge.elias.eu.org/shield/krinkuto11/streamflow/streamflow)](https://github.com/krinkuto11/streamflow/pkgs/container/streamflow)

Automated IPTV stream management for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr). Runs a configurable pipeline — playlist refresh → stream matching → stream quality checking — on a schedule, per channel.

## Installation

```bash
git clone https://github.com/krinkuto11/streamflow.git
cd streamflow
docker compose up -d
```

UI available at **http://localhost:5000** (default port).


## Features

- **Automation Profiles** — per-channel pipeline config with three independently toggleable steps: M3U update, stream matching, stream checking. See [docs/automation.md](docs/automation.md).
- **Automation Periods** — interval or cron-based schedules assigned per-channel with a profile. See [docs/automation.md](docs/automation.md).
- **Stream Matching** — regex and/or TVG-ID based stream→channel assignment with per-pattern M3U account filtering. See [docs/stream-matching.md](docs/stream-matching.md).
- **Stream Checking** — ffmpeg-based quality analysis (bitrate, resolution, FPS, codec, HDR) with weighted scoring and automatic reordering. See [docs/stream-checking.md](docs/stream-checking.md).
- **Optional Hardware Acceleration** — CPU-safe stream probing by default, with optional GPU/ffmpeg acceleration, CPU fallback, and container setup notes. See [docs/operations-guide.md#hardware-acceleration](docs/operations-guide.md#hardware-acceleration).
- **Stream Monitoring** — event-based session tracking, reliability scoring, screenshot capture, and interactive timeline. See [docs/stream-monitoring.md](docs/stream-monitoring.md).
- **Dead Stream Tracking** — persistent dead stream detection with optional auto-removal and quarantine/revival. See [docs/stream-checking.md](docs/stream-checking.md).
- **Concurrent Stream Limits** — per-M3U-account throttling during parallel checks. See [docs/stream-checking.md](docs/stream-checking.md).
- **Mass Regex Assignment** — apply patterns to multiple channels at once; supports `CHANNEL_NAME` variable substitution. See [docs/stream-matching.md](docs/stream-matching.md).
- **EPG Scheduling** — schedule channel checks from live EPG program data. See [docs/automation.md](docs/automation.md).
- **Global Action** — manually trigger a full update cycle across all channels.
- **Changelog** — timestamped activity log with per-channel and per-stream detail.
- **Operations Guide** — practical setup, profile, monitoring, and troubleshooting guidance. See [docs/operations-guide.md](docs/operations-guide.md).
- **REST API** — full API for all operations. See [docs/API.md](docs/API.md).

## Documentation

| Doc                                                    | Contents                                                    |
| ------------------------------------------------------ | ----------------------------------------------------------- |
| [docs/automation.md](docs/automation.md)               | Profiles, periods, scheduling, EPG                          |
| [docs/stream-matching.md](docs/stream-matching.md)     | Regex patterns, TVG-ID, mass assignment, validation         |
| [docs/stream-checking.md](docs/stream-checking.md)     | Quality checking, scoring, dead streams, concurrency limits |
| [docs/stream-monitoring.md](docs/stream-monitoring.md) | Live monitoring, sessions, timeline, screenshots            |
| [docs/operations-guide.md](docs/operations-guide.md)   | Setup, profiles, GPU/container notes, monitoring, troubleshooting |
| [docs/API.md](docs/API.md)                             | REST API reference                                          |
| [DEVELOPMENT.md](DEVELOPMENT.md)                       | Local dev setup                                             |

## License

See [LICENSE](LICENSE).
