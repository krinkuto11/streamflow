"""Regression coverage for monitoring OR filters with Python inline flags."""

from datetime import datetime
import itertools
import re
from unittest.mock import Mock

import pytest

from apps.automation.automated_stream_manager import RegexChannelMatcher
from apps.automation.regex_composition import combine_regex_patterns
from apps.stream import stream_session_manager as sessions


@pytest.mark.parametrize('pattern', [
    r'(?i)^KXAN$', r'(?im)^KXAN$', r'(?i)(?m)^KXAN$',
    r'(?s)^A.B$', r'(?a)^\w+$', r'(?u)^\w+$',
    '(?x)^ K X A N $ # trailing comment',
    '(?x) # header\n (?i)^ KXAN $ # trailing',
    '(?#header)(?i)^KXAN$', r'(?i)(?-i:KXAN)',
    r'(?i:(?P<station>KXAN))', r'\(\?i\)', r'[()?i]+',
    r'(?i)^\#KXAN$', r'(?x)^[#]KXAN$ # comment',
    r'(?i)^KXAN|NBC$', '(?x)# empty expression',
])
@pytest.mark.parametrize('flags', [0, re.IGNORECASE])
def test_each_branch_keeps_standalone_flag_semantics(pattern, flags):
    other = r'^OTHER$'
    combined = re.compile(combine_regex_patterns([pattern, other]), flags)
    original = re.compile(pattern, flags)
    fallback = re.compile(other, flags)
    values = ['KXAN', 'kxan', 'NBC', 'nbc', 'OTHER', 'other', '#KXAN', '(?i)',
              'é', 'ABC', 'a\nb', 'A\nB', 'prefix\nKXAN\nsuffix', '', 'unrelated']
    for value in values:
        assert bool(combined.search(value)) == bool(original.search(value) or fallback.search(value)), value


def test_global_flags_do_not_leak_into_other_alternatives():
    pattern = re.compile(combine_regex_patterns([r'(?i)^KXAN$', r'^NBC$']))
    assert pattern.search('kxan')
    assert pattern.search('NBC')
    assert not pattern.search('nbc')


@pytest.mark.parametrize('patterns', [
    [r'^KXAN$', r'^NBC$'], [r'(?i:KXAN)', r'(?-i:NBC)'],
    [r'(KXAN)', r'(?P<other>NBC)'], [r'\(\?i\)', r'[()?i]+'],
    [r'(?#header)KXAN', r'^NBC\ HD$'],
])
def test_unflagged_multi_pattern_output_is_identical_to_legacy(patterns):
    legacy = '(' + '|'.join(f'(?:{p})' for p in patterns) + ')'
    assert combine_regex_patterns(patterns) == legacy


@pytest.mark.parametrize('pattern', ['(?i)^KXAN$', '(?x) A # comment', 'invalid[', ''])
def test_single_pattern_is_returned_byte_for_byte(pattern):
    assert combine_regex_patterns([pattern]) == pattern


@pytest.mark.parametrize('patterns', [
    ['foo(?i)bar', 'NBC'], ['(?a)(?u)KXAN', 'NBC'], ['(?L)KXAN', 'NBC'],
])
def test_invalid_patterns_are_not_silently_repaired(patterns):
    with pytest.raises(re.error):
        re.compile(combine_regex_patterns(patterns))


def test_exhaustive_small_flag_and_name_matrix():
    expressions = [
        r'^ab$', r'(?i)^ab$', r'(?m)^ab$', r'(?s)^a.b$',
        r'(?a)^\w+$', r'(?u)^\w+$', '(?x)^ a b $ # end',
        r'(?i)(?m)^ab$', r'(?i)^ab(?-i:C)$',
    ]
    names = [''.join(chars) for size in range(4) for chars in itertools.product('abAB\né', repeat=size)]
    for first, second in itertools.product(expressions, repeat=2):
        combined = re.compile(combine_regex_patterns([first, second]))
        originals = [re.compile(first), re.compile(second)]
        for name in names:
            assert bool(combined.search(name)) == any(p.search(name) for p in originals), (first, second, name)


def _matcher(config):
    matcher = object.__new__(RegexChannelMatcher)
    matcher._get_effective_channel_config = Mock(return_value=config)
    return matcher


def test_channel_filter_accepts_new_and_legacy_pattern_formats():
    expected = combine_regex_patterns([r'(?i)^KXAN$', r'(?i)^NBC$'])
    for config in (
        {'regex_patterns': [{'pattern': r'(?i)^KXAN$', 'm3u_accounts': [1]}, {'pattern': r'(?i)^NBC$'}]},
        {'regex': [r'(?i)^KXAN$', r'(?i)^NBC$']},
    ):
        matcher = _matcher(config)
        assert matcher.get_channel_regex_filter('1', group_id=7) == expected
        matcher._get_effective_channel_config.assert_called_once_with('1', 7)


@pytest.mark.parametrize('config', [None, {}, {'enabled': False, 'regex': ['KXAN']}, {'regex_patterns': []}])
def test_disabled_and_missing_configs_keep_caller_default(config):
    assert _matcher(config).get_channel_regex_filter('1', default=None) is None


def test_reported_inline_flags_discover_expected_session_streams(monkeypatch):
    matcher = _matcher({'regex_patterns': [
        {'pattern': r'(?i)^.*\bKXAN\b.*'},
        {'pattern': r'(?i)^.*\bNBC\b.*\bAUSTIN\b.*'},
    ]})
    manager = object.__new__(sessions.StreamSessionManager)
    manager.scoring_windows = {}
    session = sessions.SessionInfo(
        session_id='test', channel_id=1, channel_name='NBC Austin',
        created_at=datetime.now().isoformat(), is_active=True,
        regex_filter=matcher.get_channel_regex_filter('1'),
    )
    session.quarantined_stream_ids = [4]
    manager.sessions = {'test': session}
    inventory = [
        {'id': 1, 'name': 'US: KXAN FHD', 'url': 'http://test/1'},
        {'id': 2, 'name': 'us: nbc austin hd', 'url': 'http://test/2'},
        {'id': 3, 'name': 'NBC DALLAS HD', 'url': 'http://test/3'},
        {'id': 4, 'name': 'KXAN quarantined', 'url': 'http://test/4'},
        {'id': 5, 'name': 'KXANDT unrelated identity', 'url': 'http://test/5'},
    ]
    monkeypatch.setattr(sessions, 'get_udi_manager', lambda: Mock(get_streams=lambda: inventory))
    manager._discover_streams('test')
    assert set(session.streams) == {1, 2}
    assert all(source.status == 'review' for source in session.streams.values())
