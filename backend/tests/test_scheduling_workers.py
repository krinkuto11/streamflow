from apps.background.scheduling_workers import epg_refresh_processor_loop


class ImmediateWake:
    def wait(self, timeout=None):
        return True

    def clear(self):
        pass


class Logger:
    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


def test_epg_refresh_processor_forces_fresh_auto_create_matching():
    calls = []

    class Service:
        def get_config(self):
            return {"epg_schedule": {"type": "interval", "value": 1}}

        def match_programs_to_rules(self, **kwargs):
            calls.append(kwargs)
            return {"created": 0}

    running_checks = {"count": 0}

    def is_running():
        running_checks["count"] += 1
        return running_checks["count"] <= 1

    epg_refresh_processor_loop(
        is_running=is_running,
        clear_running=lambda: None,
        get_wake_event=lambda: ImmediateWake(),
        get_scheduling_service=lambda: Service(),
        logger=Logger(),
        initial_delay_seconds=0,
        error_retry_seconds=0,
    )

    assert calls == [{"force_refresh": True}]
