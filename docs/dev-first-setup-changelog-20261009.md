# Development changelog: first-setup background startup

## Changes

- Start required EPG refresh, scheduled-event and UDI refresh workers after successful live Dispatcharr initialization following connection setup.
- Use the same startup hook when the setup wizard retries initialization after a failed connection.
- Serialize each worker's alive check and thread creation so concurrent setup completion, connection saves and direct start requests cannot create duplicate workers.
- Preserve optional automation settings and existing process-start behavior.
- Keep startup orchestration in a background module and inject the completion hook into the existing thin API handlers.

## Validation

- 55 targeted backend tests passed, including nine new regression cases for successful/failed initialization, retry, callback wiring and actual concurrent worker thread creation.
- Backend stable suite, backend integration contracts, frontend build/tests and all three CodeQL checks passed for the implementation PR.
- Both AMD64 and ARM64 test images built successfully; live validation used the real AMD64 Unraid host.
- Installed the test image through Unraid's official DockerMan template/update path with empty, isolated Appdata and a separate WebUI host port. Actual DockerMan parsing confirmed editable fields and WebUI port substitution.
- The real setup wizard rejected an unavailable connection without starting the three workers. After a valid connection was saved and initial cache loading completed, the dashboard and full readiness passed without restarting or replacing the original container process.
- Logs confirmed one EPG refresh, scheduled-event and UDI refresh worker start in that first process. Six concurrent identical connection saves completed successfully without adding workers or changing stored settings.
- A normal DockerMan container restart preserved the settings signature and connection, restored full readiness and started each required worker once in the new process.
- Verified the non-root `99:100` runtime, persistent database path, SQLite integrity and a CPU FFmpeg probe. Optional automatic checks remained disabled; the production container identity, deployment options and user preferences stayed unchanged.
- Browser validation reached setup completion and the dashboard without JavaScript page errors. Private screenshots contain cropped setup cards rather than provider or connection data.
- Removed the isolated test container through DockerMan's DockerClient and removed its test template, Appdata and local credential file.

## Release

- Ship the stable fix in 2.7.1 and backport the same runtime changes to `dev`.
- Update the Unraid installation guide to describe automatic first-setup worker startup from 2.7.1 onward.
