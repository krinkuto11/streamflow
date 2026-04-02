"""Background worker loops for scheduled-event and EPG refresh processing."""

import time
from typing import Any, Callable


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
    """Run periodic EPG refresh loop until is_running() becomes False."""
    logger.info("EPG refresh processor thread started")

    time.sleep(initial_delay_seconds)

    while is_running():
        try:
            service = get_scheduling_service()
            config = service.get_config()
            refresh_interval_minutes = max(config.get("epg_refresh_interval_minutes", 60), 5)
            refresh_interval_seconds = refresh_interval_minutes * 60

            logger.info("Fetching EPG data and matching programs to auto-create rules...")
            result = service.match_programs_to_rules()
            logger.info(f"EPG refresh complete. Created {result.get('created', 0)} events.")

            wake_event = get_wake_event()
            if wake_event is None:
                logger.critical("EPG refresh wake event is None. Stopping processor.")
                clear_running()
                break

            logger.debug(f"EPG refresh will occur again in {refresh_interval_minutes} minutes")
            wake_event.wait(timeout=refresh_interval_seconds)
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
