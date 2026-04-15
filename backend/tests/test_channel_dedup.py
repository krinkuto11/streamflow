"""
Unit tests for UDIManager.get_channels() deduplication.

Covers:
  - Normal path: no duplicates → identical output to cache
  - Duplicate IDs: deduplicated, last-write-wins, length reduced
  - None IDs: malformed entries pass through unconditionally
  - Mixed: valid dupes + None-id entries together
  - Empty cache: returns empty list
  - Single channel: returns single-element list
  - Warning logged when duplicates are found

Run with:
    PYTHONPATH=backend pytest backend/tests/test_channel_dedup.py -v
"""

import unittest
from unittest.mock import MagicMock, patch


def _make_manager():
    """Return a UDIManager instance with all external dependencies mocked."""
    with patch("apps.udi.manager.UDIStorage"), \
         patch("apps.udi.manager.UDIFetcher"), \
         patch("apps.udi.manager.UDICache"), \
         patch("apps.udi.manager.get_dispatcharr_config"):
        from apps.udi.manager import UDIManager
        mgr = UDIManager()

    # Mark as initialized so _ensure_initialized() is a no-op
    mgr._initialized = True
    return mgr


class TestGetChannelsDeduplication(unittest.TestCase):
    """Tests for the deduplication logic in UDIManager.get_channels()."""

    def setUp(self):
        self.mgr = _make_manager()

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_empty_cache_returns_empty_list(self):
        self.mgr._channels_cache = []
        result = self.mgr.get_channels()
        self.assertEqual(result, [])

    def test_single_channel_returned_unchanged(self):
        ch = {"id": 1, "name": "BBC One"}
        self.mgr._channels_cache = [ch]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 1)

    def test_no_duplicates_returns_all_channels(self):
        """Cache with unique IDs must come back intact (same length, same IDs)."""
        self.mgr._channels_cache = [
            {"id": 1, "name": "BBC One"},
            {"id": 2, "name": "ITV"},
            {"id": 3, "name": "Channel 4"},
        ]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 3)
        ids = [ch["id"] for ch in result]
        self.assertEqual(ids, [1, 2, 3])

    # ------------------------------------------------------------------
    # Deduplication behaviour
    # ------------------------------------------------------------------

    def test_duplicate_id_reduces_length(self):
        """A cache containing the same id twice must yield only one entry."""
        self.mgr._channels_cache = [
            {"id": 1, "name": "Original"},
            {"id": 2, "name": "Other"},
            {"id": 1, "name": "Duplicate"},   # second occurrence of id 1
        ]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 2)

    def test_last_write_wins_for_duplicate_ids(self):
        """When an ID appears twice the *last* entry's data must be returned."""
        self.mgr._channels_cache = [
            {"id": 7, "name": "First"},
            {"id": 7, "name": "Last"},
        ]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Last")

    def test_insertion_order_preserved_for_first_occurrence(self):
        """The position of each ID in the output matches its first appearance."""
        self.mgr._channels_cache = [
            {"id": 10, "name": "A"},
            {"id": 20, "name": "B"},
            {"id": 10, "name": "A-dup"},  # duplicate of first
            {"id": 30, "name": "C"},
        ]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 3)
        self.assertEqual([ch["id"] for ch in result], [10, 20, 30])
        # Last-write-wins: position 0 should have the *updated* name
        self.assertEqual(result[0]["name"], "A-dup")

    def test_many_duplicates_of_same_id(self):
        """Twenty copies of the same channel collapse to one."""
        self.mgr._channels_cache = [{"id": 5, "name": f"Copy {i}"} for i in range(20)]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Copy 19")   # last wins

    def test_twenty_channels_page_scenario(self):
        """
        Regression: if a user sees 20 copies of one channel filling the page,
        their _channels_cache likely holds 20 copies. This must collapse to 1.
        """
        duplicated = [{"id": 42, "name": "ESPN"} for _ in range(20)]
        self.mgr._channels_cache = duplicated
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 42)

    def test_full_list_doubled_scenario(self):
        """
        Regression: if storage-load + network-append doubled the whole list,
        20 unique channels stored twice yields 20, not 40.
        """
        unique = [{"id": i, "name": f"Ch {i}"} for i in range(1, 21)]
        self.mgr._channels_cache = unique + unique   # doubled
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 20)
        ids = [ch["id"] for ch in result]
        self.assertEqual(sorted(ids), list(range(1, 21)))

    # ------------------------------------------------------------------
    # None-ID passthrough
    # ------------------------------------------------------------------

    def test_none_id_entry_passes_through(self):
        """Channels with id=None must not be dropped."""
        self.mgr._channels_cache = [
            {"id": 1, "name": "Good"},
            {"id": None, "name": "Malformed"},
        ]
        result = self.mgr.get_channels()
        # Both entries must survive
        self.assertEqual(len(result), 2)
        none_entries = [ch for ch in result if ch.get("id") is None]
        self.assertEqual(len(none_entries), 1)

    def test_missing_id_key_treated_as_none(self):
        """Channels without an 'id' key behave the same as id=None."""
        self.mgr._channels_cache = [
            {"id": 1, "name": "Good"},
            {"name": "No ID key"},
        ]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 2)

    def test_multiple_none_id_entries_all_preserved(self):
        """Multiple None-id entries must all pass through (no dedup between them)."""
        self.mgr._channels_cache = [
            {"id": None, "name": "Bad A"},
            {"id": None, "name": "Bad B"},
            {"id": 1,    "name": "Good"},
        ]
        result = self.mgr.get_channels()
        self.assertEqual(len(result), 3)

    # ------------------------------------------------------------------
    # Mixed scenarios
    # ------------------------------------------------------------------

    def test_mixed_dupes_and_none_ids(self):
        """Duplicate valid IDs deduplicated; None-id entries preserved."""
        self.mgr._channels_cache = [
            {"id": 1,    "name": "First"},
            {"id": None, "name": "Malformed"},
            {"id": 1,    "name": "Dup"},
            {"id": 2,    "name": "Other"},
        ]
        result = self.mgr.get_channels()
        # id=1 deduped to 1, id=None kept, id=2 kept → total 3
        self.assertEqual(len(result), 3)
        valid_ids = [ch["id"] for ch in result if ch.get("id") is not None]
        self.assertCountEqual(valid_ids, [1, 2])

    # ------------------------------------------------------------------
    # Return value is a new list (not the cache itself)
    # ------------------------------------------------------------------

    def test_returns_new_list_not_cache_reference(self):
        """Mutating the returned list must not mutate _channels_cache."""
        self.mgr._channels_cache = [{"id": 1, "name": "BBC"}]
        result = self.mgr.get_channels()
        result.append({"id": 99, "name": "Injected"})
        self.assertEqual(len(self.mgr._channels_cache), 1)

    # ------------------------------------------------------------------
    # Warning logging
    # ------------------------------------------------------------------

    def test_warning_logged_when_duplicates_found(self):
        self.mgr._channels_cache = [
            {"id": 1, "name": "A"},
            {"id": 1, "name": "B"},
        ]
        with patch("apps.udi.manager.logger") as mock_logger:
            self.mgr.get_channels()
            mock_logger.warning.assert_called_once()
            warning_msg = mock_logger.warning.call_args[0][0]
            self.assertIn("deduplicated", warning_msg)

    def test_no_warning_when_no_duplicates(self):
        self.mgr._channels_cache = [
            {"id": 1, "name": "A"},
            {"id": 2, "name": "B"},
        ]
        with patch("apps.udi.manager.logger") as mock_logger:
            self.mgr.get_channels()
            mock_logger.warning.assert_not_called()

    def test_no_warning_for_empty_cache(self):
        self.mgr._channels_cache = []
        with patch("apps.udi.manager.logger") as mock_logger:
            self.mgr.get_channels()
            mock_logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
