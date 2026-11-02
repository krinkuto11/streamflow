# Dispatcharr integration

StreamFlow needs a Dispatcharr administrator account (user level 10 or higher),
including when authenticating with an API key. Dispatcharr 0.33.0 omits provider
stream URLs for non-admins. The connection test and quality preflight verify
`GET /api/accounts/users/me/`; a readable channel list alone is insufficient.
An account permission failure stops the quality operation before probing or
changing stream state.

Shadow Monitor follows an active viewer's MPEG-TS, fMP4, or native HLS format.
For HLS it opens a separate session with the Watcher API Key, passes the opaque
playlist to FFmpeg, and deletes only its own session on completion, cancellation,
timeout, or persistent watcher shutdown. The same key owns creation and deletion.
Failed transient cleanup is retried once; a remaining failure is logged without
the key or session token, and Dispatcharr's ghost cleanup remains the fallback.
If the viewer format is unknown, probes explicitly request MPEG-TS so a server
or user HLS default cannot create an untracked session.

Auto-Create uses `/api/epg/programs/search/` with `end_after` and `start_before`
to include currently airing and upcoming programmes without scanning retained
history. The normal horizon is 24 hours, extended for callers requesting more
and by the cache lifetime. Supplemental guide and current-programme entries are
restricted to the same window. These are backend query limits, not new settings.

The snapshot follows all pages up to a 500-page safety limit. An interrupted,
invalid, cyclic or truncated result does not replace or renew a complete cache.
The previous complete snapshot remains available when one exists; otherwise
Auto-Create stops and preview requests return `epg_snapshot_incomplete` (503).
`GET /api/scheduling/epg-refresh/status` exposes `snapshot.complete`, `reason`,
`pages`, the query window and `using_previous_snapshot` when applicable. This
diagnostic status is available through the API; it is not a user-editable setting.
