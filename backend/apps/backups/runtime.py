"""Restore lifecycle adapters; the API supplies its existing runtime instances."""

import os
import sys
import time
from pathlib import Path

from apps.backups.history import capture_monitoring_history
from apps.core.atomic_json import atomic_write_json


def configure_runtime(service, *, automation_provider, stop_processors):
    from apps.stream import stream_checker_service as checker_module
    from apps.stream import shadow_blank_monitor_service as shadow_module
    from apps.stream import teamarr_preflight_service as preflight_module
    from apps.stream import stream_monitoring_service as monitoring_module
    from apps.stream.stream_session_manager import get_session_manager

    def busy_reasons():
        reasons = []
        checker = checker_module._service_instance
        if checker is not None:
            with checker.lock:
                active = any(getattr(checker, name, False) for name in (
                    '_single_stream_check_active', '_single_channel_check_active',
                    '_sync_batch_execution_active', '_automation_cycle_active'))
            queue = checker.check_queue.get_status()
            if active or queue.get('queued') or queue.get('in_progress') or queue.get('queue_size'):
                reasons.append('stream checks')
        automation = automation_provider()
        if automation is not None and automation.get_run_status().get('active'):
            reasons.append('automation')
        preflight = preflight_module._teamarr_preflight_instance
        if preflight is not None:
            status = preflight.get_status()
            if status.get('active_checks') or status.get('queued_checks_count') or status.get('queue_active_checks_count'):
                reasons.append('event preflights')
        shadow = shadow_module._shadow_monitor_instance
        if shadow is not None:
            with shadow._lock:
                if shadow._active_probes:
                    reasons.append('shadow probes')
        if get_session_manager().get_active_sessions():
            reasons.append('monitoring sessions')
        return reasons

    def stop_runtime():
        service.stop()
        for stop in stop_processors:
            stop()
        automation = automation_provider()
        if automation is not None:
            automation.stop_automation()
        for instance in (shadow_module._shadow_monitor_instance, preflight_module._teamarr_preflight_instance):
            if instance is not None:
                instance.stop(persist=False)
        if checker_module._service_instance is not None:
            checker_module._service_instance.stop()
        if monitoring_module._monitoring_instance is not None:
            monitoring_module._monitoring_instance.stop()
        from apps.stream.playback_stability_service import get_playback_stability_service
        get_playback_stability_service().stop()
        # The safety archive is created offline after exec. Preserve its optional
        # in-memory timeline before replacing this process.
        atomic_write_json(service.config_dir / 'monitoring_history.json', capture_monitoring_history(), backup=False)
        if not service.review_pending():
            inventory = service.capture_inventory()
            if inventory is not None:
                atomic_write_json(service.config_dir / 'restore_inventory.json', inventory, backup=False)

    def restart():
        time.sleep(1)  # Allow the accepted response to reach the browser.
        entrypoint = Path('/app/entrypoint.sh')
        if entrypoint.is_file() and os.access(entrypoint, os.X_OK):
            os.execv(str(entrypoint), [str(entrypoint)])
        else:
            os.execv(sys.executable, [sys.executable, *sys.argv])

    service.busy_reasons = busy_reasons
    service.stop_runtime = stop_runtime
    service.restart = restart
