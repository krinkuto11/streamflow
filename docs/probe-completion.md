# Probe completion

**Where:** Settings -> Profiles tab -> Edit profile -> Stream Checking ->
Minimum Probe Completion (%).

The advanced profile setting accepts 0 to 100, with 80 as the default for new
and existing profiles. It controls the minimum accepted portion of the requested
FFmpeg analysis duration. For example, an 80% setting with a 30-second probe
requires at least 24 seconds of evidence. Lower values accept shorter samples.
Zero disables this duration criterion. FFmpeg errors, missing video, other
quality thresholds and enabled blank/freeze checks still apply.

Set the requested analysis duration separately under Stream Checker -> Stream
Checker Configuration -> Edit -> Stream Analysis -> FFmpeg Duration (seconds).
Changing the profile percentage changes the completion rule, not that duration.
One-off stream checks without a channel profile retain the 80% default.

A successful probe can cover enough video while finishing faster than real
time. StreamFlow accepts processed media time only with a successful FFmpeg exit,
usable resolution/FPS/bitrate measurements and plausible continuous progress.
Isolated timestamps, large initial offsets, jumps, backwards time and implausible
overruns cannot establish media coverage. They fall back to wall-clock timing.
Probe retries and the final unstable verdict use the same completion policy.

The resolved profile controls parallel/sequential channel checks and their
bitrate rechecks, including explicitly selected profiles. Existing preflight
reuse is invalidated when the profile policy changes. Invalid saved percentages
fall back to 80%.

Stored stream statistics include the effective percentage, wall-clock/media
seconds, FFmpeg exit/error indicators and completion basis where available.
These are diagnostic fields; existing cached measurements without timing keep
their legacy handling. No credentials or raw FFmpeg output are stored in them.
