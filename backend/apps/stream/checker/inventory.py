"""Inventory responsibilities for the shared StreamCheckerService instance."""

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerInventoryMixin:
    @staticmethod
    def _coerce_stream_id_list(raw_stream_ids: Any) -> List[int]:
        """Return stream IDs as ints, accepting Dispatcharr int or object lists."""
        if not isinstance(raw_stream_ids, (list, tuple)):
            return []

        coerced: List[int] = []
        seen = set()
        for raw_stream_id in raw_stream_ids:
            if isinstance(raw_stream_id, dict):
                raw_stream_id = raw_stream_id.get('id')
            try:
                stream_id = int(raw_stream_id)
            except (TypeError, ValueError):
                continue
            if stream_id in seen:
                continue
            seen.add(stream_id)
            coerced.append(stream_id)
        return coerced


    def _get_channel_assignment_stream_ids(
        self,
        channel_id: int,
        channel_data: Optional[Dict[str, Any]],
        udi: Any,
        fallback_stream_ids: Optional[List[int]] = None,
        refresh_from_dispatcharr: bool = False,
    ) -> List[int]:
        """Return the best available full Dispatcharr channel assignment list.

        When dead-stream removal is disabled, the checker still rewrites the
        channel for ordering. A stale UDI stream cache must not make that write
        shrink the user's existing channel assignment list.
        """
        if refresh_from_dispatcharr:
            try:
                fetcher = getattr(udi, 'fetcher', None)
                fetch_channel_by_id = getattr(fetcher, 'fetch_channel_by_id', None)
                if callable(fetch_channel_by_id):
                    fresh_channel = fetch_channel_by_id(channel_id)
                    if isinstance(fresh_channel, dict) and 'streams' in fresh_channel:
                        try:
                            update_channel = getattr(udi, 'update_channel', None)
                            if callable(update_channel):
                                update_channel(channel_id, fresh_channel)
                        except Exception as cache_err:
                            logger.debug(
                                "Could not refresh cached channel assignment for %s: %s",
                                channel_id,
                                cache_err,
                            )
                        return self._coerce_stream_id_list(fresh_channel.get('streams'))
            except Exception as exc:
                logger.warning(
                    "Could not fetch fresh channel assignment for %s before write-back: %s",
                    channel_id,
                    exc,
                )

        assignment_ids: List[int] = []
        if isinstance(channel_data, dict):
            assignment_ids.extend(self._coerce_stream_id_list(channel_data.get('streams')))
        assignment_ids.extend(self._coerce_stream_id_list(fallback_stream_ids or []))
        return self._coerce_stream_id_list(assignment_ids)


    @staticmethod
    def _build_write_back_valid_stream_ids(
        udi: Any,
        dead_stream_removal_enabled: bool,
    ) -> Optional[set]:
        """Return cached IDs; the writer verifies cache misses with Dispatcharr."""
        if dead_stream_removal_enabled:
            return None

        try:
            get_valid_stream_ids = getattr(udi, 'get_valid_stream_ids', None)
            if callable(get_valid_stream_ids):
                return set(get_valid_stream_ids() or set())
        except Exception as exc:
            logger.warning("Could not read UDI valid stream IDs before write-back: %s", exc)
        return set()


    @staticmethod
    def _get_uncached_channel_stream_ids(
        raw_channel_stream_ids: List[int],
        cached_stream_id_set: set,
        dead_stream_removal_enabled: bool,
        dead_stream_ids: set,
    ) -> List[int]:
        """Return stream IDs present in the channel's raw Dispatcharr assignment list
        but absent from the UDI stream cache (i.e. not in cached_stream_id_set).

        These IDs need to be preserved in the write-back to Dispatcharr to avoid
        accidentally dropping streams that are validly assigned but not yet indexed
        in the UDI cache (stale cache scenario).

        When dead_stream_removal_enabled is True, IDs that are known dead (in
        dead_stream_ids) are excluded so they are still removed as intended.
        """
        return [
            sid for sid in raw_channel_stream_ids
            if sid not in cached_stream_id_set
            and (not dead_stream_removal_enabled or sid not in dead_stream_ids)
        ]


    @staticmethod
    def _limit_write_back_stream_ids(
        stream_ids: List[int],
        stream_limit: int,
        protected_stream_ids: set,
    ) -> List[int]:
        """Apply the channel limit to the final assignment, keeping active viewers."""
        ordered_ids = list(dict.fromkeys(stream_ids))
        if stream_limit <= 0 or len(ordered_ids) <= stream_limit:
            return ordered_ids

        protected = set(ordered_ids).intersection(protected_stream_ids or set())
        capacity = max(stream_limit, len(protected))
        retained = set(protected)
        for stream_id in ordered_ids:
            if len(retained) >= capacity:
                break
            retained.add(stream_id)
        return [stream_id for stream_id in ordered_ids if stream_id in retained]


    @staticmethod
    def _get_stream_m3u_account_id(stream: Dict) -> Optional[Any]:
        """Return a stream's M3U account id across legacy and SQL payloads."""
        if not isinstance(stream, dict):
            return None
        account_id = stream.get('m3u_account_id')
        if account_id in (None, ''):
            account_id = stream.get('m3u_account')
        try:
            return int(account_id) if account_id not in (None, '') else None
        except (TypeError, ValueError):
            return account_id


    @staticmethod
    def _get_priority_account_rank(account_id: Any, priority_m3u_ids: Optional[List[Any]]) -> Optional[int]:
        """Return account priority rank using type-stable id comparison."""
        if account_id in (None, '') or not priority_m3u_ids:
            return None
        account_key = str(account_id)
        for index, priority_id in enumerate(priority_m3u_ids):
            if str(priority_id) == account_key:
                return index
        return None


    def _get_m3u_account_name(self, stream_id: int, udi=None) -> Optional[str]:
        """Get the M3U account name for a stream.

        Args:
            stream_id: The stream ID to look up
            udi: Optional UDI manager instance (will fetch if not provided)

        Returns:
            M3U account name or None if not found
        """
        try:
            if udi is None:
                udi = self._checker_get_udi_manager()

            stream_data = udi.get_stream_by_id(stream_id)
            if not stream_data:
                return None

            m3u_account_id = self._get_stream_m3u_account_id(stream_data)
            if not m3u_account_id:
                return None

            m3u_account = udi.get_m3u_account_by_id(m3u_account_id)
            if not m3u_account:
                return None

            return m3u_account.get('name', 'Unknown')
        except Exception as e:
            logger.debug(f"Could not fetch M3U account for stream {stream_id}: {e}")
            return None


    @staticmethod
    def _initialize_provider_probe_account_inventory(
        *,
        udi: Any,
        limiter: Any,
        initialize_account_limits: Callable[[List[Dict[str, Any]]], Any],
        operation_label: str,
    ) -> bool:
        """Publish fresh provider authority before any quality probe starts.

        Inventory invalidation deliberately precedes the UDI fetch. A missing,
        failed, or malformed fetch therefore cannot reuse limits from an older
        successful run. An empty list is a valid authoritative snapshot and is
        still published. It permits explicitly custom streams to use the shared
        scheduler while the limiter rejects every provider-account stream.
        """
        invalidate_inventory = getattr(
            limiter,
            'invalidate_account_inventory',
            None,
        )
        if not callable(invalidate_inventory):
            logger.error(
                "%s aborted because the account limiter cannot invalidate "
                "stale provider inventory",
                operation_label,
            )
            return False
        try:
            invalidate_inventory()
        except Exception as exc:
            logger.error(
                "%s aborted because stale provider inventory invalidation "
                "failed: %s",
                operation_label,
                exc,
            )
            return False

        try:
            limiter.udi_manager = udi
        except Exception as exc:
            logger.error(
                "%s aborted because the account limiter could not bind the "
                "current UDI snapshot: %s",
                operation_label,
                exc,
            )
            return False

        get_accounts = getattr(udi, 'get_m3u_accounts', None)
        if not callable(get_accounts):
            logger.error(
                "%s aborted because the UDI account inventory getter is "
                "unavailable",
                operation_label,
            )
            return False
        try:
            accounts = get_accounts()
        except Exception as exc:
            logger.error(
                "%s aborted because the UDI account inventory fetch failed: %s",
                operation_label,
                exc,
            )
            return False

        if not isinstance(accounts, list) or any(
            not isinstance(account, dict)
            for account in accounts
        ):
            logger.error(
                "%s aborted because the UDI account inventory is malformed",
                operation_label,
            )
            return False

        try:
            # Empty is authoritative too: publishing it clears any prior
            # account routes instead of leaving stale capacity available.
            publication_result = initialize_account_limits(accounts)
        except Exception as exc:
            logger.error(
                "%s aborted because provider account inventory publication "
                "failed: %s",
                operation_label,
                exc,
            )
            return False

        # The limiter performs the authoritative deep validation (IDs, limits,
        # profiles, duplicates) while publishing atomically.  Preserve support
        # for legacy initializers that returned None, but an explicit rejection
        # must never open the scheduler for a non-empty malformed snapshot.
        if publication_result is False:
            logger.error(
                "%s aborted because the provider account inventory was "
                "rejected during publication",
                operation_label,
            )
            return False

        if not accounts:
            logger.info(
                "%s continuing with an authoritative empty provider account "
                "inventory; only explicit custom streams remain eligible",
                operation_label,
            )
        return True


    def _run_capacity_limited_stream_probes(
        self,
        streams: List[Dict[str, Any]],
        *,
        udi: Any,
        **analysis_params: Any,
    ) -> List[Dict[str, Any]]:
        """Run probes through the shared provider/profile-aware scheduler.

        One-off checks use this path too, so they cannot bypass account,
        profile, global-worker, viewer-preemption, URL-transformation, or abort
        behavior that applies to channel quality checks.
        """
        from apps.stream.concurrent_stream_limiter import (
            get_account_limiter,
            get_smart_scheduler,
            initialize_account_limits,
        )

        limiter = get_account_limiter()
        if not self._initialize_provider_probe_account_inventory(
            udi=udi,
            limiter=limiter,
            initialize_account_limits=initialize_account_limits,
            operation_label='One-off stream probe',
        ):
            return []

        concurrent_enabled = bool(self.config.get("concurrent_streams.enabled", True))
        configured_limit = self.config.get("concurrent_streams.global_limit", 10)
        try:
            global_limit = max(1, int(configured_limit)) if concurrent_enabled else 1
        except (TypeError, ValueError):
            global_limit = 10 if concurrent_enabled else 1

        scheduler = get_smart_scheduler(global_limit=global_limit)
        return scheduler.check_streams_with_limits(
            streams=streams,
            check_function=self._checker_analyze_stream,
            stagger_delay=0,
            abort_event=self.abort_current_check,
            provider_wait_timeout=self.config.get(
                "concurrent_streams.provider_wait_timeout",
                300,
            ),
            **analysis_params,
        )


    def _check_channel_limits(
        self,
        channel_id: int,
        channel_name: str,
        streams: List[Dict],
        provider_limit_override: bool = False,
    ) -> Optional[Dict]:
        """Check if a channel can be checked based on viewer and playlist limits.

        This method now uses profile-aware checking. Instead of just checking account-level
        max_streams, it verifies that at least one stream has an available profile slot.

        Args:
            channel_id: ID of the channel
            channel_name: Name of the channel
            streams: List of streams for the channel
            provider_limit_override: If True, bypass provider/profile capacity
                skips. Active viewer streams are protected by the channel
                checker so other streams can still be analyzed.

        Returns:
            None if check can proceed, or a result dict if check should be skipped
        """
        udi = self._checker_get_udi_manager()

        if provider_limit_override:
            logger.info(
                "Provider/profile slot guard override enabled for channel %s; "
                "active-viewer stream protection is handled before analysis",
                channel_name,
            )
            return None

        # Check if at least one stream can run (has an available profile)
        # This replaces the old account-level checking with profile-aware logic
        has_available_slot = False
        blocked_reasons = []

        for stream in streams:
            m3u_account = self._get_stream_m3u_account_id(stream)
            if not m3u_account:
                # Custom stream without M3U account - can always check
                has_available_slot = True
                break

            # Check if this stream can run using profile-aware checking.
            # If the helper is unavailable in a mocked environment, default to allowing checks.
            check_stream_can_run = getattr(udi, 'check_stream_can_run', None)
            if not callable(check_stream_can_run):
                has_available_slot = True
                break

            can_run_result = check_stream_can_run(stream)
            if not (isinstance(can_run_result, tuple) and len(can_run_result) == 2):
                has_available_slot = True
                break

            can_run, reason = can_run_result
            if can_run:
                has_available_slot = True
                break
            else:
                if reason and reason not in blocked_reasons:
                    blocked_reasons.append(reason)

        # If no stream has an available slot, skip the check
        if not has_available_slot:
            reason_str = "; ".join(blocked_reasons) if blocked_reasons else "All M3U account profiles are at capacity"
            logger.warning(f"Cannot check channel {channel_name}: {reason_str}")
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0,
                'skipped': True,
                'skip_reason': 'max_streams_reached',
                'reason_detail': reason_str
            }

        # At least one stream has an available slot, check can proceed
        return None


    def _get_active_viewer_protected_stream_ids(
        self,
        channel_id: int,
        streams: List[Dict[str, Any]],
        udi: Any,
    ) -> set:
        """Resolve watched stream IDs for this channel and intersect with assigned streams."""
        assigned_ids = set()
        for stream in streams:
            if not isinstance(stream, dict) or stream.get('id') is None:
                continue
            try:
                assigned_ids.add(int(stream.get('id')))
            except (TypeError, ValueError):
                continue
        if not assigned_ids:
            return set()

        protected_ids = set()
        try:
            get_active = getattr(udi, 'get_active_stream_ids_for_channel', None)
            if callable(get_active):
                protected_ids.update(get_active(channel_id) or set())
        except Exception as exc:
            logger.debug("Could not resolve active stream IDs for channel %s: %s", channel_id, exc)

        if not protected_ids:
            try:
                active_status = getattr(udi, 'is_channel_active', lambda *_args: False)(channel_id)
                get_playing = getattr(udi, 'get_playing_stream_ids', None)
                if active_status is True and callable(get_playing):
                    protected_ids.update(get_playing() or set())
            except Exception as exc:
                logger.debug("Could not fall back to playing stream IDs for channel %s: %s", channel_id, exc)

        coerced = set()
        for stream_id in protected_ids:
            try:
                coerced.add(int(stream_id))
            except (TypeError, ValueError):
                continue
        return coerced.intersection(assigned_ids)


    @staticmethod
    def _merge_protected_stream_order(
        original_stream_ids: List[int],
        reordered_ids: List[int],
        protected_stream_ids: set,
    ) -> List[int]:
        """Keep protected stream IDs at their original indexes while reordering the rest."""
        protected_stream_ids = set(protected_stream_ids or set())
        if not protected_stream_ids:
            return reordered_ids

        remaining = [
            stream_id
            for stream_id in reordered_ids
            if stream_id not in protected_stream_ids
        ]
        result: List[int] = []
        used = set()

        for original_id in original_stream_ids:
            if original_id in protected_stream_ids:
                if original_id not in used:
                    result.append(original_id)
                    used.add(original_id)
                continue
            while remaining and remaining[0] in used:
                remaining.pop(0)
            if remaining:
                next_id = remaining.pop(0)
                result.append(next_id)
                used.add(next_id)

        for stream_id in remaining:
            if stream_id not in used:
                result.append(stream_id)
                used.add(stream_id)

        for original_id in original_stream_ids:
            if original_id in protected_stream_ids and original_id not in used:
                result.append(original_id)
                used.add(original_id)

        return result


    def _active_viewer_skipped_streams(
        self,
        streams: List[Dict[str, Any]],
        protected_stream_ids: set,
    ) -> List[Dict[str, Any]]:
        skipped = []
        for stream in streams:
            stream_id = stream.get('id') if isinstance(stream, dict) else None
            try:
                protected_id = int(stream_id)
            except (TypeError, ValueError):
                continue
            if protected_id not in protected_stream_ids:
                continue
            skipped.append({
                'id': protected_id,
                'stream_id': protected_id,
                'name': stream.get('name', f"Stream {protected_id}"),
                'stream_name': stream.get('name', f"Stream {protected_id}"),
                'skip_reason': 'active_viewer_protected',
                'reason_detail': 'active_viewer_protected',
                'status': 'active_viewer_protected',
            })
        return skipped

