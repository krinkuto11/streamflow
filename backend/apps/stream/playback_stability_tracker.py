"""Pure tracker for consecutive, confirmed viewer snapshots (no IPTV requests)."""

from dataclasses import dataclass, field
import uuid


@dataclass
class PlaybackLeg:
    channel_id: int
    stream_id: int
    source_fingerprint: str
    session: str
    clients: frozenset
    last_seen: float
    total_bytes: int
    started: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    observed_seconds: float = 0
    stalled_seconds: float = 0
    samples: int = 0
    stalls: int = 0
    failovers: int = 0
    stagnant_seconds: float = 0
    in_stall: bool = False
    segment_started: float = 0

    def checkpoint(self):
        keys = ('id', 'channel_id', 'stream_id', 'source_fingerprint', 'last_seen',
                'observed_seconds', 'stalled_seconds', 'samples', 'stalls', 'failovers')
        return {key: getattr(self, key) for key in keys}


class PlaybackTracker:
    def __init__(self):
        self.legs = {}

    def reset(self):
        # Completed checkpoints remain valid; an unobserved gap is never a failure.
        self.legs.clear()

    def consume(self, snapshots, now, interval):
        changed = []
        current = {}
        for item in snapshots:
            key = item['channel_uuid']
            previous = self.legs.get(key)
            continuous = previous and 0 < now - previous.last_seen <= interval * 2.5
            same_viewer = previous and bool(previous.clients & item['clients'])
            same_session = previous and previous.session == item['session'] and same_viewer
            same_source = previous and (previous.stream_id, previous.source_fingerprint) == (
                item['stream_id'], item['source_fingerprint'])
            same_channel = previous and previous.channel_id == item['channel_id']
            if continuous and same_session and same_channel and not same_source:
                # A source change with a continuously attached viewer is a failover.
                # Ignore initial tuning churn and changes to a URL behind the same ID.
                if previous.stream_id != item['stream_id'] and now - previous.started >= 30:
                    previous.failovers += 1
                    changed.append(previous.checkpoint())
            if not (continuous and same_session and same_source and same_channel) or item['total_bytes'] < previous.total_bytes:
                current[key] = PlaybackLeg(
                    channel_id=item['channel_id'], stream_id=item['stream_id'],
                    source_fingerprint=item['source_fingerprint'], session=item['session'],
                    clients=item['clients'], last_seen=now, total_bytes=item['total_bytes'], started=now,
                    segment_started=now)
                continue
            leg = previous
            delta = now - leg.last_seen
            leg.observed_seconds += delta
            leg.samples += 1
            if item['total_bytes'] == leg.total_bytes and now - leg.started > 30:
                leg.stagnant_seconds += delta
                if leg.stagnant_seconds >= 30:
                    if not leg.in_stall:
                        leg.stalls += 1
                        leg.stalled_seconds += leg.stagnant_seconds
                        leg.in_stall = True
                    else:
                        leg.stalled_seconds += delta
            else:
                leg.stagnant_seconds = 0
                leg.in_stall = False
            leg.last_seen = now
            leg.total_bytes = item['total_bytes']
            leg.clients = item['clients']
            current[key] = leg
            changed.append(leg.checkpoint())
            # Keep the rolling window bounded even for a channel running for days.
            # Persist at most ten minutes per row; continuity stays in memory.
            if now - leg.segment_started >= 600:
                leg.id = uuid.uuid4().hex
                leg.segment_started = now
                leg.observed_seconds = 0
                leg.stalled_seconds = 0
                leg.samples = 0
                leg.stalls = 0
                leg.failovers = 0
        self.legs = current
        return changed
