# API error privacy and bounded mass regex edits

Mass find/replace in the Channels view retains Python `re` syntax, capture-group
replacements and literal replacements. Preview and Apply use one configuration
snapshot and prepare the complete replacement batch before any channel write.
An isolated, disposable Python process handles the batch with a two-second wall
clock deadline. It is killed and reaped on timeout, even when a regex holds the
GIL. On Linux/Unraid its address space is also capped at 256 MiB.

Find/replacement expressions are limited to 16,384 characters each, the selected
channel list to 10,000 IDs, the worker input to 2 MiB and the combined replacement
result to 1,048,576 characters. These are backend safety limits, not UI settings.
A rejected batch returns an actionable HTTP 400 error and does not save any
channel. Operators can simplify the expression or select fewer channels.
No new regex-engine dependency or CodeQL exclusion is needed.

Bulk regex operations, UDI initialization, Playback Stability validation,
monitoring-session creation and scheduling-rule import log internal exceptions
on the server and return public error messages. Channel IDs, failed counts,
validation error codes and missing-TVG-ID guidance remain available. Successful
single-channel checks expose their public result fields and measurement data
without provider URLs, credentials or nested exception diagnostics.

The regression tests exercise real worker termination, standard-library regex
and literal substitution parity, replacement backreferences and amplification,
preview/apply consistency, provider scoping and priority preservation, and
secret-bearing failure responses. CodeQL must be evaluated through its actual
results as well as its workflow status; a successful workflow can report alerts.

Incomplete EPG snapshots in channel-program lookup and Auto-Create rule tests
return fixed retry guidance with HTTP 503 and `epg_snapshot_incomplete`. The
response is independent of exception text, including diagnostics added by future
service changes. Missing-TVG-ID guidance and successful responses retain their
existing contracts. This is automatic API behavior, not a user setting.
