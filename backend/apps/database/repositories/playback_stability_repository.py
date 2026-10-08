"""Bounded playback history in the existing persisted SQLite database."""

from sqlalchemy import func, literal
from apps.database.connection import get_session
from apps.database.models import PlaybackObservation

MIN_OBSERVED_SECONDS = 600
MIN_SAMPLES = 60


class PlaybackStabilityRepository:
    def save(self, checkpoints, cutoff):
        with get_session() as session:
            for values in checkpoints:
                session.merge(PlaybackObservation(**values))
            session.query(PlaybackObservation).filter(PlaybackObservation.last_seen < cutoff).delete(synchronize_session=False)
            session.commit()

    def summaries(self, cutoff, stream_ids=None, aggregate_streams=False):
        m = PlaybackObservation
        with get_session() as session:
            query = session.query(
                literal(None) if aggregate_streams else m.channel_id, m.stream_id, m.source_fingerprint,
                func.sum(m.observed_seconds), func.sum(m.stalled_seconds),
                func.sum(m.samples), func.sum(m.stalls), func.sum(m.failovers), func.max(m.last_seen),
            ).filter(m.last_seen >= cutoff)
            if stream_ids is not None:
                query = query.filter(m.stream_id.in_(stream_ids))
            groups = [m.stream_id, m.source_fingerprint]
            if not aggregate_streams:
                groups.insert(0, m.channel_id)
            rows = query.group_by(*groups).all()
        result = []
        for cid, sid, fingerprint, observed, stalled, samples, stalls, failovers, last_seen in rows:
            eligible = observed >= MIN_OBSERVED_SECONDS and samples >= MIN_SAMPLES
            stability = max(0, 1 - stalled / observed) / (1 + failovers * 3600 / observed) if eligible else None
            result.append({'channel_id': cid, 'stream_id': sid, 'source_fingerprint': fingerprint,
                           'observed_seconds': round(observed, 1), 'stalled_seconds': round(stalled, 1),
                           'samples': samples, 'stalls': stalls, 'failovers': failovers,
                           'last_seen': last_seen, 'score': stability, 'eligible': eligible})
        return result
