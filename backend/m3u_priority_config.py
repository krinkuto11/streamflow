"""Legacy M3U priority-mode config facade.

Priority ordering now lives on automation profiles and stream-checking config,
but a few older tests and helper scripts still import this module directly.
This small facade preserves that read/write surface without reintroducing a
separate production config file.
"""

from typing import Dict


class M3UPriorityConfig:
    VALID_GLOBAL_PRIORITY_MODES = {"disabled", "same_resolution", "all_streams"}

    def __init__(self):
        self._config: Dict[str, str] = {
            "global_priority_mode": "disabled",
        }

    def get_global_priority_mode(self) -> str:
        return self._config.get("global_priority_mode", "disabled")

    def set_global_priority_mode(self, mode: str) -> bool:
        if mode not in self.VALID_GLOBAL_PRIORITY_MODES:
            return False
        self._config["global_priority_mode"] = mode
        return True
