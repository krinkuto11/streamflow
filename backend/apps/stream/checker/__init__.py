"""Stateless checker behaviors composed onto one service/lock/state owner."""

from .queue import CheckerQueueMixin
from .ownership import CheckerOwnershipMixin
from .inventory import CheckerInventoryMixin
from .classification import CheckerClassificationMixin
from .bitrate import CheckerBitrateMixin
from .connectivity import CheckerConnectivityMixin
from .changelog import CheckerChangelogMixin
from .status import CheckerStatusMixin
from .snapshots import CheckerSnapshotsMixin
from .batch import CheckerBatchMixin
from .single_channel import CheckerSingleChannelMixin
from .single_stream import CheckerSingleStreamMixin
from .concurrent_channel import CheckerConcurrentChannelMixin
from .sequential_channel import CheckerSequentialChannelMixin
from .loop_probes import CheckerLoopProbesMixin
from apps.stream.queue_execution import StreamCheckQueueExecutionMixin
from apps.stream.stream_stats_writer import StreamStatsWriterMixin


class CheckerOperationsMixin(
    CheckerQueueMixin,
    CheckerOwnershipMixin,
    CheckerInventoryMixin,
    CheckerClassificationMixin,
    CheckerBitrateMixin,
    CheckerConnectivityMixin,
    CheckerChangelogMixin,
    CheckerStatusMixin,
    CheckerSnapshotsMixin,
    CheckerBatchMixin,
    CheckerSingleChannelMixin,
    CheckerSingleStreamMixin,
    CheckerConcurrentChannelMixin,
    CheckerSequentialChannelMixin,
    CheckerLoopProbesMixin,
    StreamCheckQueueExecutionMixin,
    StreamStatsWriterMixin,
):
    """Behaviors only: initialization, shared state, and lifecycle belong to the service."""
