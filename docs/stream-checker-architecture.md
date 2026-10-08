# Stream Checker module boundaries

`apps.stream.stream_checker_service.StreamCheckerService` remains the public
entry point and singleton owner. It initializes configuration, queue, progress,
trackers, synchronization primitives, lifecycle threads, and operation state.
The worker/scheduler and dispatch methods remain in this facade.

Stateless behavior mixins in `apps.stream.checker` operate on that same service
instance. They do not construct additional queues, locks, progress stores,
provider limiters, or trackers, and do not start independent lifecycle threads.
Calls between responsibilities use the existing service methods and retain
queue-entry tokens, batch/progress generations, and lock ordering.

| Module | Responsibility |
|---|---|
| `queue.py` | Admission, specialized gates, draining, clear and abort |
| `ownership.py` | Operation reservations and authorized terminal/side-effect transitions |
| `inventory.py` | Fresh assignments, provider inventory, capacity and viewer protection |
| `classification.py` | Thresholds, quality classification, scoring and sort keys |
| `bitrate.py` | Current-measurement evidence and deferred bitrate rechecks |
| `connectivity.py` | Fail-closed connectivity checks and bounded recovery |
| `changelog.py` | Generation-owned batch result aggregation |
| `status.py` | Status snapshots, stale diagnostics and queue estimates |
| `snapshots.py` | Run evidence and visibility result reporting |
| `batch.py` | Synchronous multi-channel execution and result counts |
| `single_channel.py` | Direct channel refresh/matching/check orchestration |
| `single_stream.py` | Direct capacity-limited stream checks |
| `concurrent_channel.py` | Parallel channel orchestration |
| `concurrent_progress.py` | Profile reservations, atomic live-row snapshots and initial probe callbacks |
| `bitrate_progress.py` | Serial bitrate recheck callbacks and terminal live-row updates |
| `heartbeat.py` | Periodic publication of current run snapshots |
| `sequential_channel.py` | Sequential channel orchestration |
| `loop_probes.py` | Capacity-limited loop analysis |
| `refresh_scope.py` | Playlist scope selection and refresh observation |

The existing `queue_execution.py` and `stream_stats_writer.py` behaviors remain
part of the composition. Probe result fields remain in `quality_report_fields.py`.

Callback factories receive the existing run values, mutable counters, status
mapping and synchronization objects explicitly. They return callable bundles;
they do not copy the run state or create locks. Heartbeat thread creation,
shutdown and joining remain in the channel orchestrator. The initial callbacks,
heartbeat and bitrate callbacks share the same revision/publication boundaries.

Connector/media bindings stay at the facade boundary. Properties return the
original callable rather than wrapping it, preserving its identity, attributes,
and replaceable integration/test bindings. Version-file discovery also stays
in the facade so its relative paths remain unchanged.

Validation includes source-AST comparison of the relocated methods (normalizing
only dependency access), followed by runtime regression and integration checks.
The source-location regression reads the actual concurrent/sequential methods
rather than assuming all execution code lives in the facade file.
