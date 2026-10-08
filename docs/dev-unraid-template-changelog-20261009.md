# Development changelog: Unraid Community Applications template

## Changes

- Add a public DockerMan v2 template for the stable image, with a development branch option.
- Expose host port, persistent Appdata path and user/group IDs through standard template fields.
- Use bridge networking and CPU probing without privileged mode or embedded connection credentials.
- Add repository profile metadata, existing project icons, example-data screenshots and GitHub support links.
- Document manual DockerMan installation, normal updates and the Community Applications submission workflow.
- Document optional NVIDIA and DRI/VAAPI setup through DockerMan, including device permissions, the exact Stream Checker controls, CPU fallback and return to CPU mode.
- Add a cropped 40 KB screenshot of the real hardware settings, without connection or provider data.
- Document automatic required-worker startup after first setup in StreamFlow 2.7.1.
- Keep Community Applications availability marked as pending until the listing is published.

## Validation

- XML validation passed for both metadata files: DockerMan v2 fields, stable/development image tags, bridge networking, non-privileged defaults, persistent mount and credential-free template values.
- Existing public icon and example screenshot URLs were reachable. The template and guide canonical URLs refer to `main` and will become available there after merge.
- Installed the stable image with empty Appdata on the real Unraid host using its official DockerMan `update_container` script, an isolated user template, a separate data directory and an alternate host port.
- Verified that DockerMan discovers the user template, parses the editable fields and resolves the WebUI to the configured host port using its actual PHP implementation. An interactive authenticated Unraid GUI session was unavailable; application browser tests and DockerMan parser checks were performed separately.
- Completed the real StreamFlow 2.7.0 setup wizard with an existing Dispatcharr connection, reached the dashboard and observed no browser JavaScript errors. Initial cache loading completed; this first test exposed the required-worker startup gap tracked in #495.
- The separate fix in #496 was verified on a fresh DockerMan installation: failed initialization did not start workers, successful setup reached readiness in the original process, and concurrent saves retained one worker per service. The guide describes the corrected 2.7.1 flow.
- After that restart, readiness passed, the Dispatcharr connection remained valid and stored settings matched their pre-restart signature.
- A normal DockerMan update recreated the container while preserving the image selection, environment, port and data mapping; readiness and stored settings passed again afterward.
- Verified the non-root runtime (`99:100`), SQLite integrity, persistent database location and a CPU FFmpeg probe.
- Applied the documented optional NVIDIA runtime/variables through the test user template and DockerMan. CUDA decoding of a generated local H.264 clip passed as `99:100`, without a provider stream connection.
- Enabled CUDA through the documented Stream Checker UI, confirmed NVIDIA detection and CPU fallback, saved the configuration, then returned the test instance to CPU mode. Intel/AMD decode was not exercised on this validation host.
- Automatic stream checks stayed disabled in the isolated instance. The existing production container identity, deployment options and user preferences remained unchanged before and after testing.
- Removed the isolated container through DockerMan's DockerClient, then removed its test template, Appdata and local credential file.
- PR CI passed: backend stable suite, backend integration contracts, frontend build/tests and all three CodeQL checks.

## Submission status

- No runtime code or existing user defaults change in this PR.
- Repository submission remains pending. After merge, an Unraid account must run the portal's Validate/Scan steps and submit for review; catalog scanning and approval were not claimed as completed by these checks.
