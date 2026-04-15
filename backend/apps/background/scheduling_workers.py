"""Background worker loops for scheduled-event and EPG refresh processing."""

import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional


def _is_schedule_due(schedule: Optional[dict], last_run: Optional[datetime]) -> bool:
    """Determine whether a schedule is due to fire.

    Mirrors the _is_period_due() logic in automated_stream_manager.py.
    Supports the same {type, value} structure used by automation periods.

    Args:
        schedule: Dict with 'type' ('interval' or 'cron') and 'value'.
                  None means no schedule configured — never due.
        last_run: Timestamp of last successful run. None means never run —
                  treat as due immediately for interval, due on first cron match
                  for cron.

    Returns:
        True if the schedule should fire now.
    """
    if not schedule:
        return False

    stype = schedule.get("type", "interval")
    svalue = schedule.get("value")

    if stype == "interval":
        try:
            interval_mins = max(1, int(svalue))
        except (TypeError, ValueError):
            return False
        if last_run is None:
            return True
        return datetime.now() - last_run >= timedelta(minutes=interval_mins)

    elif stype == "cron":
        try:
            from croniter import croniter
            base = last_run if last_run is not None else datetime.now() - timedelta(seconds=1)
            cron = croniter(str(svalue), base)
            next_run = cron.get_next(datetime)
            return datetime.now() >= next_run
        except Exception:
            return False

    return False


def scheduled_event_processor_loop(
    *,
    is_running: Callable[[], bool],
    get_wake_event: Callable[[], Any],
    get_scheduling_service: Callable[[], Any],
    get_stream_checker_service: Callable[[], Any],
    logger: Any,
    check_interval: int = 30,
):
    """Run scheduled-event processing loop until is_running() becomes False."""
    logger.info("Scheduled event processor thread started")

    while is_running():
        try:
            wake_event = get_wake_event()
            if wake_event is None:
                logger.error("Wake event is None; using fallback sleep.")
                time.sleep(check_interval)
            else:
                wake_event.wait(timeout=check_interval)
                wake_event.clear()

            service = get_scheduling_service()
            stream_checker = get_stream_checker_service()
            due_events = service.get_due_events()

            if due_events:
                logger.info(f"Found {len(due_events)} scheduled event(s) due for execution")

                for event in due_events:
                    event_id = event.get("id")
                    channel_name = event.get("channel_name", "Unknown")
                    program_title = event.get("program_title", "Unknown")

                    logger.info(
                        f"Executing scheduled event {event_id} for {channel_name} "
                        f"(program: {program_title})"
                    )

                    try:
                        success = service.execute_scheduled_check(event_id, stream_checker)
                        if success:
                            logger.info(f"Successfully executed and removed scheduled event {event_id}")
                        else:
                            logger.warning(f"Failed to execute scheduled event {event_id}")
                    except Exception as exc:
                        logger.error(f"Error executing scheduled event {event_id}: {exc}", exc_info=True)

        except Exception as exc:
            logger.error(f"Error in scheduled event processor: {exc}", exc_info=True)

    logger.info("Scheduled event processor thread stopped")


def epg_refresh_processor_loop(
    *,
    is_running: Callable[[], bool],
    clear_running: Callable[[], None],
    get_wake_event: Callable[[], Any],
    get_scheduling_service: Callable[[], Any],
    logger: Any,
    initial_delay_seconds: int,
    error_retry_seconds: int,
):
    """Run periodic EPG refresh loop until is_running() becomes False.

    Uses the same {type, value} schedule structure as automation periods.
    Reads the schedule on every iteration so config changes take effect
    without restart.
    """
    logger.info("EPG refresh processor thread started")

    time.sleep(initial_delay_seconds)

    epg_last_run: Optional[datetime] = None

    while is_running():
        try:
            service = get_scheduling_service()
            config = service.get_config()

            # Support both legacy integer key and new schedule object.
            # _load_config() migrates on first load but guard here for safety.
            epg_schedule = config.get("epg_schedule")
            if epg_schedule is None:
                legacy_mins = config.get("epg_refresh_interval_minutes", 60)
                epg_schedule = {"type": "interval", "value": int(legacy_mins)}

            if _is_schedule_due(epg_schedule, epg_last_run):
                logger.info("Fetching EPG data and matching programs to auto-create rules...")
                result = service.match_programs_to_rules()
                logger.info(f"EPG refresh complete. Created {result.get('created', 0)} events.")
                epg_last_run = datetime.now()

            wake_event = get_wake_event()
            if wake_event is None:
                logger.critical("EPG refresh wake event is None. Stopping processor.")
                clear_running()
                break

            # Sleep for the minimum of: 60s poll interval or remaining time to next due
            wake_event.wait(timeout=60)
            wake_event.clear()

        except Exception as exc:
            logger.error(f"Error in EPG refresh processor: {exc}", exc_info=True)
            wake_event = get_wake_event()
            if wake_event and is_running():
                wake_event.wait(timeout=error_retry_seconds)
                wake_event.clear()
            else:
                break

    logger.info("EPG refresh processor thread stopped")


def udi_refresh_processor_loop(
    *,
    is_running: Callable[[], bool],
    get_wake_event: Callable[[], Any],
    get_scheduling_service: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
    logger: Any,
    check_interval: int = 60,
):
    """Run periodic UDI cache refresh loop until is_running() becomes False.

    Fires refresh_all() on the UDI manager according to the schedule stored in
    the scheduling config (udi_refresh_schedule). Supports both interval and
    cron schedule types, identical to automation periods.

    Guards:
    - is_network_ready(): skips until startup network refresh completes.
    - is_automation_busy(): skips the slot if a cycle or single-channel check
      is running. Does not queue — waits for the next scheduled slot.
    - Schedule null/not configured: worker is dormant, logs nothing.
    """
    logger.info("UDI refresh processor thread started")

    while is_running():
        try:
            wake_event = get_wake_event()
            if wake_event is None:
                logger.error("UDI refresh wake event is None; using fallback sleep.")
                time.sleep(check_interval)
            else:
                wake_event.wait(timeout=check_interval)
                wake_event.clear()

            udi = get_udi_manager()

            # Guard: startup network refresh must be complete
            if not udi.is_network_ready():
                logger.debug(
                    "UDI refresh processor waiting — network refresh not yet complete"
                )
                continue

            # Read schedule on every iteration so config changes take effect
            # without restart.
            service = get_scheduling_service()
            schedule = service.get_udi_refresh_schedule()

            if not schedule:
                # No schedule configured — worker is dormant
                continue

            last_run = udi.get_udi_refresh_last_run()

            if not _is_schedule_due(schedule, last_run):
                continue

            # Guard: skip if a cycle or single-channel check is running
            if udi.is_automation_busy():
                logger.info(
                    "UDI refresh skipping slot — automation is currently busy. "
                    "Will retry at next scheduled time."
                )
                continue

            # Guard: skip if queue-based stream checking is active.
            # The _worker_loop processes queued channels outside check_single_channel
            # so is_automation_busy() does not cover it. stream_checking_mode
            # reflects queue activity, in-progress checks, and sync batch state.
            try:
                from apps.stream.stream_checker_service import get_stream_checker_service
                checker_status = get_stream_checker_service().get_status()
                if checker_status.get('stream_checking_mode', False):
                    logger.info(
                        "UDI refresh skipping slot — stream checking is active. "
                        "Will retry at next scheduled time."
                    )
                    continue
            except Exception as _sc_err:
                logger.debug(f"Could not check stream checking mode: {_sc_err}")

            logger.info(
                f"Scheduled UDI refresh firing "
                f"(schedule: {schedule.get('type')} {schedule.get('value')})..."
            )
            success = udi.refresh_all()
            if success:
                udi.set_udi_refresh_last_run()
                logger.info("✓ Scheduled UDI refresh completed")
            else:
                logger.warning("Scheduled UDI refresh failed — will retry at next scheduled time")

        except Exception as exc:
            logger.error(f"Error in UDI refresh processor: {exc}", exc_info=True)

    logger.info("UDI refresh processor thread stopped")
