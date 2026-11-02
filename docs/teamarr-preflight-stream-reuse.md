# Reuse successful Teamarr preflight measurements

Enable **Teamarr Preflight -> Configuration -> Skip Known Good Streams** to avoid
opening successful sources again at each automatic pre-start/post-start checkpoint.
The option is off by default. Manual **Force Check** still probes all eligible sources.

The first automatic check of each event probes the assigned sources normally.
Later checkpoints retry failed, below-threshold, incomplete, new, changed or stale
sources. A recovered source becomes reusable at subsequent checkpoints. Sources
protected by active viewers and existing provider capacity rules retain that protection.

Evidence belongs to one event (or one static-team live window), channel UUID,
source URL/provider profile, resolved quality profile and analysis configuration.
Changing any of those invalidates the evidence. Newer contradictory statistics or
dead status also require another probe. The original check time is retained;
reading or reusing a result never extends its lifetime. Evidence expires no later
than event start plus Post Start Grace, with a maximum age equal to the configured
preflight span. SQL-backed evidence survives a restart; absent or unreadable state
falls back to ordinary checks. The store is bounded and expires old entries.

Reused sources still take part in ranking and limits alongside newly measured
sources. If all sources are already good, the checkpoint completes without media
probes or provider-slot reservations. Stream details identify the reused result
and its previous successful check time.

A successful technical measurement does not prove that an event is being shown,
and a skipped source can fail after that measurement. This option reduces probes;
it does not add continuous monitoring.

Internal configuration key: `skip_known_good_streams` in the existing Teamarr
preflight configuration API (`/api/teamarr-preflight/config`). Internal evidence
uses the SQL system setting `teamarr_preflight_stream_success_state`; it contains
bounded hashes and timestamps, without stored provider credentials or raw URLs.
