"""Bounded, credential-free snapshots of one preflight's stream decisions."""

import json
import math
from datetime import datetime, timezone

from apps.core.log_sanitizer import scrub_secrets
from apps.core.stream_stats_utils import extract_stream_stats, format_stream_stats_for_display

MAX_INSPECTION_STREAMS = 500
MAX_INSPECTION_BYTES = 256 * 1024
TEXT_FIELDS = {
    'stream_name', 'provider_name', 'status', 'reason', 'resolution', 'fps',
    'bitrate', 'video_codec', 'audio_codec', 'action', 'measurement_source',
}
NUMBER_FIELDS = {'stream_id', 'provider_id', 'score', 'rank', 'candidate_rank', 'provider_rank'}


def safe_text(value):
    return scrub_secrets(value)[:300] if value is not None else None


def public_inspection(value):
    """Project fields rather than exposing arbitrary analyzer/exception payloads."""
    if not isinstance(value, dict):
        return None
    result = {'schema_version': 1}
    for key in ('checked_at', 'profile_id', 'profile_name', 'priority_mode', 'ordering_status'):
        result[key] = safe_text(value.get(key))
    rows = value.get('streams')
    if not isinstance(rows, list):
        return None
    total = value.get('total_streams')
    result['total_streams'] = max(len(rows), total if isinstance(total, int) else 0)
    result['streams'] = []
    for raw in rows[:MAX_INSPECTION_STREAMS]:
        if not isinstance(raw, dict):
            continue
        row = {key: safe_text(raw[key]) for key in TEXT_FIELDS if key in raw}
        row.update({key: raw[key] for key in NUMBER_FIELDS
                    if isinstance(raw.get(key), (int, float)) and not isinstance(raw[key], bool)
                    and math.isfinite(raw[key])})
        result['streams'].append(row)
    result['truncated'] = bool(value.get('truncated')) or len(rows) > len(result['streams'])
    while len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_INSPECTION_BYTES:
        result['streams'].pop()
        result['truncated'] = True
    return result


def build_inspection(*, streams, analyzed, skipped, checked_ids, dead_ids,
                     ordered_ids, profile, priority_mode, priority_ids, provider_names,
                     decision_reasons=None):
    """Capture probe facts before removed/limited streams disappear from the channel."""
    analysis = {item.get('stream_id'): item for item in analyzed}
    skipped_by_id = {item.get('id', item.get('stream_id')): item for item in skipped}
    assigned = {sid: rank for rank, sid in enumerate(ordered_ids or [], 1)}
    candidate_ranks = {item.get('stream_id'): rank for rank, item in enumerate(analyzed, 1)}
    rows = []
    for source in streams:
        sid = source.get('id')
        probe = analysis.get(sid)
        cached = bool(probe and (probe.get('status') == 'cached' or probe.get('cached')))
        deferred = bool(probe and probe.get('provider_limit_skipped'))
        fresh = sid in checked_ids and probe is not None and not cached and not deferred
        skipped_item = skipped_by_id.get(sid)
        reason = (probe or {}).get('dead_reason') or (probe or {}).get('quality_reason')
        if reason == 'none':
            reason = None
        if not reason and (probe or {}).get('blank_detected'):
            reason = 'blank'
        if not reason and (probe or {}).get('freeze_detected'):
            reason = 'freeze'
        detail = ((probe or {}).get('dead_reason_detail')
                  or (probe or {}).get('quality_reason_detail') or (probe or {}).get('reason_detail'))
        if detail == 'none':
            detail = None
        if detail == reason:
            detail = None
        if skipped_item or not fresh:
            status = 'skipped'
            reason = (skipped_item or {}).get('reason')
            if not reason:
                reason = ((probe or {}).get('reason_detail') or 'provider_capacity') if deferred else (
                    'cached_measurement' if cached else 'not_checked')
        elif (probe or {}).get('reason_detail') == 'viewer_preempted':
            status, reason = 'skipped', 'viewer_preempted'
        elif sid in dead_ids:
            status = 'dead'
            reason = (decision_reasons or {}).get(sid) or reason or 'marked_dead'
        elif (probe or {}).get('measurement_incomplete'):
            status, reason = 'incomplete', (probe or {}).get('measurement_incomplete_reason') or 'incomplete_measurement'
        elif reason in ('blank', 'freeze', 'low_quality'):
            status = 'warning'
        elif (probe or {}).get('error') or reason in ('error', 'failed', 'timeout', 'offline'):
            status, reason = 'dead', reason or 'analysis_failed'
        else:
            status = 'alive'
        facts = probe or source
        metrics = format_stream_stats_for_display(extract_stream_stats(facts))
        provider_id = source.get('m3u_account') or source.get('m3u_account_id')
        if isinstance(provider_id, dict):
            provider_id = provider_id.get('id')
        row = {
            'stream_id': sid, 'stream_name': source.get('name') or (probe or {}).get('stream_name'),
            'provider_id': provider_id, 'provider_name': provider_names.get(sid),
            **metrics, 'status': status,
            'reason': ': '.join(str(part) for part in (reason, detail if fresh else None) if part),
            'measurement_source': 'current_check' if fresh else 'previous_measurement' if cached or source.get('stream_stats') else 'not_measured',
            'rank': assigned.get(sid), 'candidate_rank': candidate_ranks.get(sid),
            'provider_rank': priority_ids.index(provider_id) + 1 if provider_id in priority_ids else None,
            'action': ('retained' if sid in assigned else 'not_retained') if ordered_ids is not None else 'order_not_applied',
        }
        if probe is not None and isinstance(probe.get('score'), (int, float)):
            row['score'] = probe['score']
        rows.append(row)
    rows.sort(key=lambda row: (row.get('rank') is None, row.get('rank') or row.get('candidate_rank') or 999999))
    profile = profile or {}
    return public_inspection({
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'profile_id': profile.get('id'), 'profile_name': profile.get('name'),
        'priority_mode': priority_mode,
        'ordering_status': 'applied' if ordered_ids is not None else 'not_applied',
        'streams': rows,
    })
