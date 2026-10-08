# StreamFlow 2.7.1

This patch release fixes the required background-service startup after first-time setup, tracked in [issue #495](https://github.com/krinkuto11/streamflow/issues/495).

## Fixed

- **First setup without a restart.** After a successful live Dispatcharr initialization, StreamFlow starts the required EPG refresh, scheduled-event and UDI refresh workers. The application can become fully ready during the same container process.
- **Initialization retry.** The setup wizard uses the same completion hook when it retries initialization after a failed connection. Workers remain stopped while initialization fails and can start when live data loading succeeds.
- **One worker per service.** Serialized alive checks and thread creation prevent duplicate workers when connection saves, setup completion and direct start requests happen concurrently.

## Compatibility

No database migration, new setting or change to existing user preferences is required. Optional automation keeps its configured state. Existing installations continue using their normal saved-configuration startup path.

The stable image is `ghcr.io/krinkuto11/streamflow:latest`; the versioned image is `ghcr.io/krinkuto11/streamflow:2.7.1`. Keep the existing persistent `/app/data` mapping when updating through DockerMan or Docker Compose.

See [the development changelog](dev-first-setup-changelog-20261009.md) for the regression and live validation record.
