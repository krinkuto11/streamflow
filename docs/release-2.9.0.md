# StreamFlow 2.9.0

This release promotes the tested development changes to stable. Changes are
relative to **2.8.0**. It includes security updates alongside more efficient
Teamarr checks and improved Dispatcharr compatibility.

### Added

- **Reuse successful Teamarr Preflight measurements ([#528](https://github.com/krinkuto11/streamflow/issues/528), [#532](https://github.com/krinkuto11/streamflow/pull/532)).** The new **Teamarr Preflight -> Configuration -> Skip Known Good Streams** option is off by default. Later checkpoints of the same event reuse successful measurements, while failed, below-threshold, incomplete, new, changed and stale sources still receive fresh probes. Original check times, ordering and manual full rechecks are preserved. Suggested by [@Muppetteer](https://github.com/Muppetteer).
- **Soft per-provider stream caps with ranked backfill ([#526](https://github.com/krinkuto11/streamflow/issues/526), [#527](https://github.com/krinkuto11/streamflow/pull/527)).** **Settings -> Profiles -> Edit profile -> Stream Checking -> Provider Stream Cap** prefers sources within each provider's cap, then fills unused channel slots from the remaining eligible ranked sources. This improves backup-provider coverage without leaving slots empty. The default is 0, disabled; active-viewer protection and existing ranking remain intact. Suggested by [@sm0keb0x](https://github.com/sm0keb0x).

### Fixed

- **Dispatcharr 0.33 permissions, HLS monitoring and EPG history ([#521](https://github.com/krinkuto11/streamflow/pull/521)).** Validate administrator capability for API-key and credential connections and before guarded quality work. Shadow Monitor owns and releases its own native HLS sessions without disconnecting an independent viewer. Auto-Create reads complete, time-bounded, paginated EPG snapshots, retaining long-running current programmes and excluding ended/out-of-window history and duplicate guide rows.

### Security

- **Setuptools 84.1.0 ([#521](https://github.com/krinkuto11/streamflow/pull/521), [#529](https://github.com/krinkuto11/streamflow/pull/529)).** Update the production and test hash-locked requirements actually installed by Docker and CI, together with the reviewed input pin, to resolve [GHSA-xmj3-9gf7-h52w](https://github.com/advisories/GHSA-xmj3-9gf7-h52w).
- **Follow-redirects 1.16.2 ([#529](https://github.com/krinkuto11/streamflow/pull/529), [#530](https://github.com/krinkuto11/streamflow/pull/530)).** Remove the vulnerable transitive versions flagged by the frontend audit for [GHSA-mrwp-f3q8-3g9p](https://github.com/advisories/GHSA-mrwp-f3q8-3g9p), without disabling or bypassing the security checks.

### Changed

- **Runtime and tooling updates ([#522](https://github.com/krinkuto11/streamflow/pull/522), [#523](https://github.com/krinkuto11/streamflow/pull/523), [#524](https://github.com/krinkuto11/streamflow/pull/524), [#525](https://github.com/krinkuto11/streamflow/pull/525), [#530](https://github.com/krinkuto11/streamflow/pull/530), [#531](https://github.com/krinkuto11/streamflow/pull/531)).** Include reviewed Axios, Hookform Resolvers, React Hook Form, React Router, Lucide, PostCSS, Vite/Vitest and Node type updates. Frontend test and image-build workflows now use Node 24 to match Vitest 5's supported engines. Automated dependency proposals are credited to [Dependabot](https://github.com/apps/dependabot).
- **Preflight Help and measurement details.** Document the new control at its verified UI location with a small, lazy-loaded crop from the live application. Reused stream rows show the original successful check time and distinguish historical evidence from a new probe.

### Upgrade notes and credits

- Update `ghcr.io/krinkuto11/streamflow:latest`, or pin `ghcr.io/krinkuto11/streamflow:2.9.0`. Preserve the existing `/app/data` mapping and configured environment/device settings; take a host-level backup before upgrading.
- Both Skip Known Good Streams and Provider Stream Cap are opt-in. Skipping a previously successful stream does not provide continuous monitoring: the stream can fail after its recorded measurement. Use a manual Force Check for a full recheck.
- Feature suggestions are credited to [@Muppetteer](https://github.com/Muppetteer) and [@sm0keb0x](https://github.com/sm0keb0x). Implementation, integration, security maintenance and release work are by [@bttfw](https://github.com/bttfw), with automated dependency proposals from Dependabot.
- Report regressions with the version/image tag, reproduction steps and sanitized logs: [open an issue](https://github.com/krinkuto11/streamflow/issues/new/choose).

Full comparison: [2.8.0...2.9.0](https://github.com/krinkuto11/streamflow/compare/2.8.0...2.9.0).
