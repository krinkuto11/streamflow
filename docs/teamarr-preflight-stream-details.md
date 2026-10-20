# Teamarr preflight stream details

Open **Teamarr Preflight → Preflight Items → Stream details** on an item with a completed check. The section starts collapsed and reads the saved result when opened. Opening it does not run a check, refresh a playlist, or change channel assignments.

Each row identifies the stream and provider and shows the status, a brief reason, measured resolution, frame rate, bitrate, video/audio codecs, quality score, and channel rank saved by that check. Removed dead streams and candidates excluded by the profile's stream limit remain visible. Skipped and incomplete checks are identified separately; previous measurements are labelled and do not establish that the stream was playable during this check.

The order summary explains how the profile combines playlist priority, resolution, and score. A higher score does not necessarily mean a better channel rank. **First in channel** describes the saved channel order, not an active viewer's playback. If monitoring prevented the order from being applied, the result says **Order not applied**.

Details are retained for up to 120 checks while the preflight service is running. Each result is limited to 500 streams and 256 KiB; a size-limited result explicitly shows the number displayed and total candidates. Older checks and checks made before this feature may have no detail data. Restarting the service clears these diagnostic snapshots. The checked time and profile describe the original result, even if configuration changes later.

The read-only `GET /api/teamarr-preflight/inspections/<inspection_id>` endpoint returns a versioned, bounded projection without stream URLs, credentials, or raw analyzer payloads. The normal status response contains only the inspection reference. An expired reference returns `404 inspection_not_found`.
