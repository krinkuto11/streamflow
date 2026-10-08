# Dispatcharr throttling

StreamFlow respects Dispatcharr's requested delay when retrying a throttled
UDI GET. Supported hints are numeric or HTTP-date `Retry-After` headers and
the Django REST Framework `Expected available in ... seconds` detail. A valid
hint is not shortened to the usual small backoff; absent or malformed hints
fall back to the existing bounded retry cadence.

Credential logins are serialized and share a monotonic cooldown after HTTP 429.
Waiting initial-login callers recheck the stored token and reuse a successful
login. Changing the configured server or credentials starts a new cooldown
identity. API-key authentication does not use the token-login endpoint.

These are backend behaviors, not additional user settings. Request timeouts
and UDI retry-count limits still apply. Unsafe POST operations are not given
new automatic retries by this change.
