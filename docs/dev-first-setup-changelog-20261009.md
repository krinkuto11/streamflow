# Development changelog: first-setup background startup

## Changes

- Start required EPG refresh, scheduled-event and UDI refresh workers after successful live Dispatcharr initialization following connection setup.
- Use the same startup hook when the setup wizard retries initialization after a failed connection.
- Serialize each worker's alive check and thread creation so concurrent setup completion, connection saves and direct start requests cannot create duplicate workers.
- Preserve optional automation settings and existing process-start behavior.
- Keep startup orchestration in a background module and inject the completion hook into the existing thin API handlers.

## Validation

- Regression and live DockerMan fresh-install validation are in progress.
