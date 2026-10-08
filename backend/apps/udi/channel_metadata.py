"""Fresh channel metadata reads and atomic publication into UDI indexes."""

import copy
import json


def source_signature(stream):
    fields = ('name', 'url', 'tvg_id', 'channel_group', 'channel_group_id',
              'm3u_account', 'm3u_account_id', 'stream_profile_id', 'is_custom')
    return json.dumps({key: stream.get(key) for key in fields}, sort_keys=True, default=str)


class ChannelMetadataMixin:
    def _fetch_channel_read(self, channel_id):
        return self._channel_reads.run(
            (self.fetcher.base_url, int(channel_id)),
            lambda: self.fetcher.fetch_channel_by_id(int(channel_id)),
        )

    def _publish_channel(self, channel):
        channel_id = int(channel['id'])
        existing = self._channels_by_id.get(channel_id)
        previous_group = existing.get('channel_group_id') if existing else None
        incoming = dict(channel)
        if existing is None:
            existing = incoming
            self._channels_cache.append(existing)
        else:
            existing.clear()
            existing.update(incoming)
        self._channels_by_id[channel_id] = existing
        current_group = existing.get('channel_group_id')
        if previous_group != current_group:
            previous = self._channels_by_group_id.get(previous_group, [])
            self._channels_by_group_id[previous_group] = [item for item in previous if item.get('id') != channel_id]
        group = self._channels_by_group_id.setdefault(current_group, [])
        if not any(item.get('id') == channel_id for item in group):
            group.append(existing)

    def refresh_channel_metadata(self, channel_id):
        """Coalesce only in-flight reads; every later call reads fresh metadata."""
        report = self._metadata_reads.run(
            (self.fetcher.base_url, int(channel_id)),
            lambda: self._refresh_channel_metadata(int(channel_id)),
        )
        return copy.deepcopy(report)

    def _refresh_channel_metadata(self, channel_id):
        from apps.core.api_utils import _stream_stats_update_lock

        scope = self.fetcher.base_url
        with self._lock:
            baseline_stats = {int(sid): copy.deepcopy(self._streams_by_id.get(int(sid), {}).get('stream_stats'))
                              for sid in (self._channels_by_id.get(channel_id, {}).get('streams') or [])}
        channel = self._fetch_channel_read(channel_id)
        if not channel or int(channel.get('id', 0)) != channel_id:
            return {'success': False, 'reason': 'channel_metadata_unavailable'}
        stream_ids = {int(sid) for sid in channel.get('streams') or []}
        # Newly assigned streams were outside the old channel's snapshot. Record
        # their current stats before the stream read so old cached values do not
        # override fresh server values, while writes during that read still win.
        with self._lock:
            for sid in stream_ids - baseline_stats.keys():
                baseline_stats[sid] = copy.deepcopy(self._streams_by_id.get(sid, {}).get('stream_stats'))
        streams = self.fetcher.fetch_streams_by_ids(sorted(stream_ids)) if stream_ids else []
        if {int(item['id']) for item in streams} != stream_ids:
            return {'success': False, 'reason': 'stream_metadata_incomplete'}
        # Match the statistics writer's lock order. Network reads stay outside.
        with _stream_stats_update_lock, self._lock:
            if scope != self.fetcher.base_url:
                return {'success': False, 'reason': 'metadata_scope_changed'}
            old_channel = self._channels_by_id.get(channel_id) or {}
            channel_changed = any(old_channel.get(key) != channel.get(key)
                                  for key in ('name', 'uuid', 'channel_group_id', 'streams'))
            changed = set()
            for incoming in streams:
                incoming = copy.deepcopy(incoming)
                sid = int(incoming['id'])
                existing = self._streams_by_id.get(sid)
                if existing is None or source_signature(existing) != source_signature(incoming):
                    changed.add(sid)
                # An acknowledged local write during the read wins over its older server snapshot.
                if existing and existing.get('stream_stats') != baseline_stats.get(sid):
                    incoming['stream_stats'] = copy.deepcopy(existing.get('stream_stats'))
                self.update_stream(sid, incoming)
            if channel_changed:
                changed.update(stream_ids)
            self._publish_channel(channel)
            return {'success': True, 'changed_stream_ids': sorted(changed),
                    'channel_changed': channel_changed}
