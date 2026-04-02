"""Request schema validation helpers for the Flask API layer."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from apps.core.exceptions import ValidationError


def _ensure_dict(payload: Any, *, message: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError(message)
    return payload


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValidationError(f"{field_name} must be a boolean")


def _ensure_non_empty_list(value: Any, *, field_name: str) -> List[Any]:
    if not isinstance(value, list) or len(value) == 0:
        raise ValidationError(f"{field_name} must be a non-empty list")
    return value


def _normalize_profile_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    stream_checking = data.get("stream_checking")
    if stream_checking is None:
        return data

    if not isinstance(stream_checking, dict):
        raise ValidationError("stream_checking must be an object")

    normalized = dict(data)
    normalized_stream_checking = dict(stream_checking)

    if "remove_dead_streams" in normalized_stream_checking:
        normalized_stream_checking["remove_dead_streams"] = _parse_bool(
            normalized_stream_checking["remove_dead_streams"],
            field_name="stream_checking.remove_dead_streams",
        )

    normalized["stream_checking"] = normalized_stream_checking
    return normalized


@dataclass(frozen=True)
class RegexPatternCreateSchema:
    channel_id: str
    name: str
    regex: Any
    enabled: bool
    m3u_accounts: Optional[List[int]]

    @classmethod
    def from_payload(cls, payload: Any) -> "RegexPatternCreateSchema":
        data = _ensure_dict(payload, message="No pattern data provided")

        required_fields = ("channel_id", "name", "regex")
        missing = [field for field in required_fields if field not in data]
        if missing:
            raise ValidationError(
                "Missing required fields",
                details={"missing_fields": missing},
            )

        channel_id = str(data["channel_id"]).strip()
        name = str(data["name"]).strip()
        regex = data["regex"]

        if not channel_id:
            raise ValidationError("channel_id cannot be empty")
        if not name:
            raise ValidationError("name cannot be empty")
        if not isinstance(regex, (str, list)):
            raise ValidationError("regex must be a string or list")

        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be a boolean")

        m3u_accounts = data.get("m3u_accounts")
        if m3u_accounts is not None:
            if not isinstance(m3u_accounts, list):
                raise ValidationError("m3u_accounts must be a list of integer IDs")
            normalized_accounts: List[int] = []
            for account_id in m3u_accounts:
                try:
                    normalized_accounts.append(int(account_id))
                except (TypeError, ValueError):
                    raise ValidationError("m3u_accounts must be a list of integer IDs") from None
            m3u_accounts = normalized_accounts

        return cls(
            channel_id=channel_id,
            name=name,
            regex=regex,
            enabled=enabled,
            m3u_accounts=m3u_accounts,
        )


@dataclass(frozen=True)
class ChannelMatchSettingsSchema:
    match_by_tvg_id: Optional[bool]

    @classmethod
    def from_payload(cls, payload: Any) -> "ChannelMatchSettingsSchema":
        data = _ensure_dict(payload, message="No settings provided")

        match_by_tvg_id = None
        if "match_by_tvg_id" in data:
            match_by_tvg_id = _parse_bool(data["match_by_tvg_id"], field_name="match_by_tvg_id")

        if match_by_tvg_id is None:
            raise ValidationError("No supported settings provided")

        return cls(match_by_tvg_id=match_by_tvg_id)


@dataclass(frozen=True)
class GroupRegexConfigSchema:
    regex_patterns: List[Any]
    enabled: bool
    match_by_tvg_id: bool
    name: str
    m3u_accounts: Optional[List[int]]

    @classmethod
    def from_payload(cls, payload: Any) -> "GroupRegexConfigSchema":
        data = _ensure_dict(payload, message="No data provided")

        regex_patterns = data.get("regex_patterns")
        if regex_patterns is None:
            regex_patterns = data.get("regex", [])

        if not isinstance(regex_patterns, list):
            raise ValidationError("regex_patterns must be a list")

        enabled_raw = data.get("enabled", True)
        match_by_tvg_id_raw = data.get("match_by_tvg_id", False)
        enabled = _parse_bool(enabled_raw, field_name="enabled")
        match_by_tvg_id = _parse_bool(match_by_tvg_id_raw, field_name="match_by_tvg_id")

        name = str(data.get("name", "")).strip()

        m3u_accounts = data.get("m3u_accounts")
        if m3u_accounts is not None:
            if not isinstance(m3u_accounts, list):
                raise ValidationError("m3u_accounts must be a list of integer IDs")
            normalized_accounts: List[int] = []
            for account_id in m3u_accounts:
                try:
                    normalized_accounts.append(int(account_id))
                except (TypeError, ValueError):
                    raise ValidationError("m3u_accounts must be a list of integer IDs") from None
            m3u_accounts = normalized_accounts

        return cls(
            regex_patterns=regex_patterns,
            enabled=enabled,
            match_by_tvg_id=match_by_tvg_id,
            name=name,
            m3u_accounts=m3u_accounts,
        )


@dataclass(frozen=True)
class BulkRegexPatternsSchema:
    channel_ids: List[int]
    regex_patterns: List[Any]
    m3u_accounts: Optional[List[int]]

    @classmethod
    def from_payload(cls, payload: Any) -> "BulkRegexPatternsSchema":
        data = _ensure_dict(payload, message="No data provided")

        channel_ids_raw = _ensure_non_empty_list(data.get("channel_ids"), field_name="channel_ids")
        try:
            channel_ids = [int(cid) for cid in channel_ids_raw]
        except (TypeError, ValueError):
            raise ValidationError("channel_ids must be a list of integers") from None

        regex_patterns = _ensure_non_empty_list(
            data.get("regex_patterns"), field_name="regex_patterns"
        )

        m3u_accounts = data.get("m3u_accounts")
        if m3u_accounts is not None:
            if not isinstance(m3u_accounts, list):
                raise ValidationError("m3u_accounts must be a list of integer IDs")
            normalized_accounts: List[int] = []
            for account_id in m3u_accounts:
                try:
                    normalized_accounts.append(int(account_id))
                except (TypeError, ValueError):
                    raise ValidationError("m3u_accounts must be a list of integer IDs") from None
            m3u_accounts = normalized_accounts

        return cls(channel_ids=channel_ids, regex_patterns=regex_patterns, m3u_accounts=m3u_accounts)


@dataclass(frozen=True)
class AutomationProfileCreateSchema:
    profile_data: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "AutomationProfileCreateSchema":
        data = _ensure_dict(payload, message="No data provided")
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValidationError("Profile name is required")
        return cls(profile_data=_normalize_profile_payload(data))


@dataclass(frozen=True)
class AutomationProfileUpdateSchema:
    profile_data: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "AutomationProfileUpdateSchema":
        data = _ensure_dict(payload, message="No data provided")
        if not data:
            raise ValidationError("No data provided")
        return cls(profile_data=_normalize_profile_payload(data))


@dataclass(frozen=True)
class ProfileIdsBulkDeleteSchema:
    profile_ids: List[Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "ProfileIdsBulkDeleteSchema":
        data = _ensure_dict(payload, message="No data provided")
        profile_ids = _ensure_non_empty_list(data.get("profile_ids"), field_name="profile_ids")
        return cls(profile_ids=profile_ids)


@dataclass(frozen=True)
class SingleEntityProfileAssignmentSchema:
    entity_id: Any
    profile_id: Optional[Any]

    @classmethod
    def from_payload(cls, payload: Any, *, entity_field: str) -> "SingleEntityProfileAssignmentSchema":
        data = _ensure_dict(payload, message="No data provided")
        entity_id = data.get(entity_field)
        if entity_id is None:
            raise ValidationError(f"{entity_field} is required")
        return cls(entity_id=entity_id, profile_id=data.get("profile_id"))


@dataclass(frozen=True)
class MultiEntityProfileAssignmentSchema:
    entity_ids: List[Any]
    profile_id: Optional[Any]

    @classmethod
    def from_payload(cls, payload: Any, *, entity_field: str) -> "MultiEntityProfileAssignmentSchema":
        data = _ensure_dict(payload, message="No data provided")
        entity_ids = _ensure_non_empty_list(data.get(entity_field), field_name=entity_field)
        return cls(entity_ids=entity_ids, profile_id=data.get("profile_id"))


@dataclass(frozen=True)
class AutomationPeriodCreateSchema:
    period_data: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "AutomationPeriodCreateSchema":
        data = _ensure_dict(payload, message="No data provided")

        name = str(data.get("name", "")).strip()
        if not name:
            raise ValidationError("Period name is required")

        schedule = data.get("schedule")
        if not isinstance(schedule, dict):
            raise ValidationError("Schedule is required")

        schedule_type = str(schedule.get("type", "")).strip()
        schedule_value = schedule.get("value")
        if not schedule_type or schedule_value in (None, ""):
            raise ValidationError("schedule.type and schedule.value are required")

        return cls(period_data=data)


@dataclass(frozen=True)
class AutomationPeriodUpdateSchema:
    period_data: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "AutomationPeriodUpdateSchema":
        data = _ensure_dict(payload, message="No data provided")
        if not data:
            raise ValidationError("No data provided")

        if "schedule" in data:
            schedule = data.get("schedule")
            if not isinstance(schedule, dict):
                raise ValidationError("schedule must be an object")
            if "type" in schedule and schedule.get("value") in (None, ""):
                raise ValidationError("schedule.value is required when schedule.type is provided")

        return cls(period_data=data)


@dataclass(frozen=True)
class PeriodAssignmentSchema:
    entity_ids: List[Any]
    profile_id: Any
    replace: bool

    @classmethod
    def from_payload(cls, payload: Any, *, entity_field: str) -> "PeriodAssignmentSchema":
        data = _ensure_dict(payload, message="No data provided")
        entity_ids = _ensure_non_empty_list(data.get(entity_field), field_name=entity_field)

        profile_id = data.get("profile_id")
        if profile_id in (None, ""):
            raise ValidationError("profile_id is required")

        replace = _parse_bool(data.get("replace", False), field_name="replace")
        return cls(entity_ids=entity_ids, profile_id=profile_id, replace=replace)


@dataclass(frozen=True)
class PeriodRemovalSchema:
    entity_ids: List[Any]

    @classmethod
    def from_payload(cls, payload: Any, *, entity_field: str) -> "PeriodRemovalSchema":
        data = _ensure_dict(payload, message="No data provided")
        entity_ids = _ensure_non_empty_list(data.get(entity_field), field_name=entity_field)
        return cls(entity_ids=entity_ids)


@dataclass(frozen=True)
class BatchPeriodAssignmentsSchema:
    entity_ids: List[Any]
    period_assignments: List[Dict[str, Any]]
    replace: bool

    @classmethod
    def from_payload(cls, payload: Any, *, entity_field: str) -> "BatchPeriodAssignmentsSchema":
        data = _ensure_dict(payload, message="No data provided")

        entity_ids = _ensure_non_empty_list(data.get(entity_field), field_name=entity_field)
        period_assignments_raw = _ensure_non_empty_list(
            data.get("period_assignments"), field_name="period_assignments"
        )

        period_assignments: List[Dict[str, Any]] = []
        for assignment in period_assignments_raw:
            if not isinstance(assignment, dict):
                raise ValidationError("Each period assignment must be an object")
            if assignment.get("period_id") in (None, "") or assignment.get("profile_id") in (None, ""):
                raise ValidationError("Each period assignment must have period_id and profile_id")
            period_assignments.append(assignment)

        replace = _parse_bool(data.get("replace", False), field_name="replace")
        return cls(entity_ids=entity_ids, period_assignments=period_assignments, replace=replace)


@dataclass(frozen=True)
class BatchPeriodUsageSchema:
    channel_ids: List[Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "BatchPeriodUsageSchema":
        data = _ensure_dict(payload, message="No data provided")
        channel_ids = _ensure_non_empty_list(data.get("channel_ids"), field_name="channel_ids")
        return cls(channel_ids=channel_ids)


@dataclass(frozen=True)
class StreamSessionCreateSchema:
    channel_id: str
    regex_filter: Optional[str]
    pre_event_minutes: int
    stagger_ms: int
    timeout_ms: int
    epg_event: Optional[Dict[str, Any]]
    auto_created: bool
    auto_create_rule_id: Optional[str]
    enable_loop_detection: bool
    enable_looping_detection: bool
    enable_logo_detection: bool

    @classmethod
    def from_payload(cls, payload: Any) -> "StreamSessionCreateSchema":
        data = _ensure_dict(payload, message="No session data provided")

        channel_id = str(data.get("channel_id", "")).strip()
        if not channel_id:
            raise ValidationError("channel_id is required")

        try:
            pre_event_minutes = int(data.get("pre_event_minutes", 30))
            stagger_ms = int(data.get("stagger_ms", 200))
            timeout_ms = int(data.get("timeout_ms", 30000))
        except (TypeError, ValueError):
            raise ValidationError("pre_event_minutes, stagger_ms, and timeout_ms must be integers") from None

        regex_filter_raw = data.get("regex_filter")
        regex_filter = None if regex_filter_raw in (None, "") else str(regex_filter_raw)

        epg_event = data.get("epg_event")
        if epg_event is not None and not isinstance(epg_event, dict):
            raise ValidationError("epg_event must be an object")

        auto_created = _parse_bool(data.get("auto_created", False), field_name="auto_created")
        enable_loop_detection = _parse_bool(
            data.get("enable_loop_detection", False), field_name="enable_loop_detection"
        )
        enable_looping_detection = _parse_bool(
            data.get("enable_looping_detection", True), field_name="enable_looping_detection"
        )
        enable_logo_detection = _parse_bool(
            data.get("enable_logo_detection", True), field_name="enable_logo_detection"
        )

        auto_create_rule_id_raw = data.get("auto_create_rule_id")
        auto_create_rule_id = None if auto_create_rule_id_raw in (None, "") else str(auto_create_rule_id_raw)

        return cls(
            channel_id=channel_id,
            regex_filter=regex_filter,
            pre_event_minutes=pre_event_minutes,
            stagger_ms=stagger_ms,
            timeout_ms=timeout_ms,
            epg_event=epg_event,
            auto_created=auto_created,
            auto_create_rule_id=auto_create_rule_id,
            enable_loop_detection=enable_loop_detection,
            enable_looping_detection=enable_looping_detection,
            enable_logo_detection=enable_logo_detection,
        )


@dataclass(frozen=True)
class GroupStreamSessionsCreateSchema:
    group_id: str
    regex_filter: Optional[str]
    pre_event_minutes: int
    stagger_ms: int
    timeout_ms: int
    enable_loop_detection: bool
    enable_looping_detection: bool
    enable_logo_detection: bool

    @classmethod
    def from_payload(cls, payload: Any) -> "GroupStreamSessionsCreateSchema":
        data = _ensure_dict(payload, message="No group session data provided")

        group_id = str(data.get("group_id", "")).strip()
        if not group_id:
            raise ValidationError("group_id is required")

        try:
            pre_event_minutes = int(data.get("pre_event_minutes", 30))
            stagger_ms = int(data.get("stagger_ms", 200))
            timeout_ms = int(data.get("timeout_ms", 30000))
        except (TypeError, ValueError):
            raise ValidationError("pre_event_minutes, stagger_ms, and timeout_ms must be integers") from None

        regex_filter_raw = data.get("regex_filter")
        regex_filter = None if regex_filter_raw in (None, "") else str(regex_filter_raw)

        return cls(
            group_id=group_id,
            regex_filter=regex_filter,
            pre_event_minutes=pre_event_minutes,
            stagger_ms=stagger_ms,
            timeout_ms=timeout_ms,
            enable_loop_detection=_parse_bool(
                data.get("enable_loop_detection", False), field_name="enable_loop_detection"
            ),
            enable_looping_detection=_parse_bool(
                data.get("enable_looping_detection", True), field_name="enable_looping_detection"
            ),
            enable_logo_detection=_parse_bool(
                data.get("enable_logo_detection", True), field_name="enable_logo_detection"
            ),
        )


@dataclass(frozen=True)
class SessionIdsPayloadSchema:
    session_ids: List[str]

    @classmethod
    def from_payload(cls, payload: Any) -> "SessionIdsPayloadSchema":
        data = _ensure_dict(payload, message="No session data provided")
        session_ids = data.get("session_ids")

        if not isinstance(session_ids, list) or not session_ids:
            raise ValidationError("session_ids must be a non-empty list")

        normalized_ids = [str(session_id).strip() for session_id in session_ids if str(session_id).strip()]
        if not normalized_ids:
            raise ValidationError("session_ids must contain at least one valid id")

        return cls(session_ids=normalized_ids)


@dataclass(frozen=True)
class SessionSettingsUpdateSchema:
    review_duration: Optional[float]
    loop_review_duration: Optional[float]

    @classmethod
    def from_payload(cls, payload: Any) -> "SessionSettingsUpdateSchema":
        data = _ensure_dict(payload, message="No settings provided")

        review_duration = None
        loop_review_duration = None

        if "review_duration" in data:
            try:
                review_duration = float(data.get("review_duration"))
            except (TypeError, ValueError):
                raise ValidationError("review_duration must be a number") from None
            if review_duration < 0:
                raise ValidationError("review_duration must be greater than or equal to 0")

        if "loop_review_duration" in data:
            try:
                loop_review_duration = float(data.get("loop_review_duration"))
            except (TypeError, ValueError):
                raise ValidationError("loop_review_duration must be a number") from None
            if loop_review_duration < 0:
                raise ValidationError("loop_review_duration must be greater than or equal to 0")

        if review_duration is None and loop_review_duration is None:
            raise ValidationError("No settings provided")

        return cls(review_duration=review_duration, loop_review_duration=loop_review_duration)


@dataclass(frozen=True)
class SchedulingConfigUpdateSchema:
    config: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "SchedulingConfigUpdateSchema":
        data = _ensure_dict(payload, message="No configuration provided")
        if not data:
            raise ValidationError("No configuration provided")
        return cls(config=data)


@dataclass(frozen=True)
class ScheduledEventCreateSchema:
    event_data: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "ScheduledEventCreateSchema":
        data = _ensure_dict(payload, message="No event data provided")
        required_fields = ["channel_id", "program_start_time", "program_end_time", "program_title"]
        missing = [field for field in required_fields if field not in data]
        if missing:
            raise ValidationError("Missing required fields", details={"missing_fields": missing})
        return cls(event_data=data)


@dataclass(frozen=True)
class AutoCreateRuleCreateSchema:
    rule_data: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "AutoCreateRuleCreateSchema":
        data = _ensure_dict(payload, message="No rule data provided")

        required_fields = ["name", "regex_pattern"]
        missing = [field for field in required_fields if field not in data]
        if missing:
            raise ValidationError("Missing required fields", details={"missing_fields": missing})

        if "channel_id" not in data and "channel_ids" not in data:
            raise ValidationError("Missing required field: channel_id or channel_ids")

        return cls(rule_data=data)


@dataclass(frozen=True)
class AutoCreateRuleUpdateSchema:
    rule_data: Dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> "AutoCreateRuleUpdateSchema":
        data = _ensure_dict(payload, message="No rule data provided")
        if not data:
            raise ValidationError("No rule data provided")
        return cls(rule_data=data)


@dataclass(frozen=True)
class AutoCreateRuleTestSchema:
    channel_id: Any
    regex_pattern: str

    @classmethod
    def from_payload(cls, payload: Any) -> "AutoCreateRuleTestSchema":
        data = _ensure_dict(payload, message="No test data provided")
        required_fields = ["channel_id", "regex_pattern"]
        missing = [field for field in required_fields if field not in data]
        if missing:
            raise ValidationError("Missing required fields", details={"missing_fields": missing})
        return cls(channel_id=data["channel_id"], regex_pattern=str(data["regex_pattern"]))


@dataclass(frozen=True)
class AutoCreateRulesImportSchema:
    rules_data: List[Dict[str, Any]]

    @classmethod
    def from_payload(cls, payload: Any) -> "AutoCreateRulesImportSchema":
        if not isinstance(payload, list):
            raise ValidationError("Rules data must be an array")
        return cls(rules_data=payload)
