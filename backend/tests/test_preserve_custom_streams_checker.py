"""Stream checker helpers behind stream_matching.preserve_custom_streams."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from apps.stream.checker.inventory import CheckerInventoryMixin  # noqa: E402


class TestCustomStreamHelpers(unittest.TestCase):
    def test_custom_stream_ids_picks_only_custom_in_order(self):
        streams = [
            {'id': 1, 'is_custom': False},
            {'id': 900, 'is_custom': True},
            {'id': 2},
            {'id': '901', 'is_custom': True},
            {'id': None, 'is_custom': True},
            'junk',
        ]
        self.assertEqual(CheckerInventoryMixin._custom_stream_ids(streams), [900, 901])

    def test_custom_stream_ids_empty(self):
        self.assertEqual(CheckerInventoryMixin._custom_stream_ids(None), [])
        self.assertEqual(CheckerInventoryMixin._custom_stream_ids([]), [])

    def test_append_custom_moves_custom_last(self):
        self.assertEqual(
            CheckerInventoryMixin._append_custom_stream_ids([900, 3, 1, 2], [900]),
            [3, 1, 2, 900],
        )

    def test_append_custom_restores_stream_cut_by_limit(self):
        # The slate was dropped (dead removal / stream limit); it is re-added last.
        self.assertEqual(
            CheckerInventoryMixin._append_custom_stream_ids([3, 1], [900]),
            [3, 1, 900],
        )

    def test_append_custom_noop_without_custom(self):
        self.assertEqual(CheckerInventoryMixin._append_custom_stream_ids([3, 1], []), [3, 1])


if __name__ == '__main__':
    unittest.main()
