"""Unit tests for the OpenStream monitoring backend (URL parsing + health mapping)."""

from apps.stream.openstream_monitor import OpenStreamStreamMonitor, parse_openstream_url

CID = "aabbccddeeff00112233445566778899aabbccdd"


def test_parse_getstream_url():
    base, cid = parse_openstream_url(f"http://host:6878/ace/getstream?id={CID}")
    assert base == "http://host:6878"
    assert cid == CID


def test_parse_short_path_url():
    base, cid = parse_openstream_url(f"http://10.0.0.5:6878/{CID}")
    assert base == "http://10.0.0.5:6878"
    assert cid == CID


def test_parse_uppercase_id_is_lowercased():
    _, cid = parse_openstream_url(f"http://h:6878/ace/getstream?id={CID.upper()}")
    assert cid == CID


def test_parse_non_acestream_url():
    assert parse_openstream_url("http://host/live/channel.m3u8") == (None, None)
    assert parse_openstream_url("") == (None, None)
    assert parse_openstream_url("not a url") == (None, None)


def _monitor():
    return OpenStreamStreamMonitor(url=f"http://host:6878/ace/getstream?id={CID}", stream_id=1)


def test_healthy_snapshot_maps_to_alive_keeping_up():
    m = _monitor()
    m._apply_snapshot({"health": {"state": "healthy", "keepUpMargin": 1.0, "trueBitrateKbps": 4200}})
    assert m.stats.is_alive is True
    assert m.stats.is_fatal is False
    assert m.is_buffering() is False
    assert m.stats.speed == 1.0
    assert m.stats.bitrate == 4200
    assert m.get_transport_health()["status"] == "healthy"


def test_draining_snapshot_is_buffering():
    m = _monitor()
    m._apply_snapshot({"health": {"state": "draining", "keepUpMargin": 0.7}})
    assert m.stats.is_alive is True
    assert m.is_buffering() is True
    assert m.get_transport_health()["status"] == "degraded"


def test_dead_is_fatal_only_after_it_lasts():
    """Dead sources recover (54/54 episodes, slowest 182 s), so a dead report
    ranks the source down at once but only evicts it after DEAD_FATAL_SECS."""
    from apps.stream.openstream_monitor import DEAD_FATAL_SECS
    m = _monitor()
    m._apply_snapshot({"health": {"state": "dead", "keepUpMargin": 0.0}}, now=1000.0)
    assert m.stats.is_alive is True and m.stats.is_fatal is False
    assert m.rank_score() == 0.0  # delivers nothing: ranked last already
    m._apply_snapshot({"health": {"state": "dead", "keepUpMargin": 0.0}}, now=1000.0 + DEAD_FATAL_SECS - 1)
    assert m.stats.is_alive is True
    m._apply_snapshot({"health": {"state": "dead", "keepUpMargin": 0.0}}, now=1000.0 + DEAD_FATAL_SECS)
    assert m.stats.is_alive is False and m.stats.is_fatal is True
    assert m.get_transport_health()["status"] == "dead"


def test_dead_blip_resets():
    from apps.stream.openstream_monitor import DEAD_FATAL_SECS
    m = _monitor()
    m._apply_snapshot({"health": {"state": "dead"}}, now=1000.0)
    m._apply_snapshot({"health": {"state": "healthy", "keepUpMargin": 1.0}}, now=1010.0)
    m._apply_snapshot({"health": {"state": "dead"}}, now=1000.0 + DEAD_FATAL_SECS)
    assert m.stats.is_alive is True  # the clock restarted with the second episode


def test_rank_score_is_delivered_share_over_the_window():
    from apps.stream.openstream_monitor import RANK_WINDOW_SECS
    m = _monitor()
    assert m.rank_score() is None
    snap = lambda kbps: {"kbps": kbps, "health": {"state": "healthy", "keepUpMargin": 1.0, "trueBitrateKbps": 5000}}
    m._apply_snapshot(snap(5000), now=0.0)    # full delivery
    m._apply_snapshot(snap(7000), now=1.0)    # catching up: capped at 1
    m._apply_snapshot(snap(2500), now=2.0)    # half
    m._apply_snapshot(snap(0), now=3.0)       # nothing
    assert m.rank_score() == 100 * (1 + 1 + 0.5 + 0) / 4
    m._apply_snapshot(snap(5000), now=3.0 + RANK_WINDOW_SECS + 0.5)  # older samples age out
    assert m.rank_score() == 100.0

def test_missing_health_is_warming():
    m = _monitor()
    m._apply_snapshot({"contentID": CID})  # session exists, no health yet
    assert m.stats.is_alive is True          # not dead — just starting
    assert m.is_buffering() is True          # warming counts as not-yet-healthy
    assert m.stats.speed == 0.0


def test_snapshot_captures_swarm_telemetry():
    m = _monitor()
    m._apply_snapshot({
        "peers": 12,
        "seeders": 4,
        "kbps": 5300.5,
        "health": {
            "state": "healthy",
            "keepUpMargin": 1.0,
            "reliabilityScore": 0.87,
            "latencySecs": 3,
            "trueBitrateKbps": 4200,
        },
    })
    assert m.stats.peers == 12
    assert m.stats.seeders == 4
    assert m.stats.download_kbps == 5300.5
    assert m.stats.reliability_score == 0.87
    assert m.stats.keepup_margin == 1.0
    assert m.stats.latency_secs == 3
    assert m.stats.swarm_state == "healthy"


def test_warming_still_reports_peers_and_download():
    """Peers/download are meaningful before health exists — keep them while warming."""
    m = _monitor()
    m._apply_snapshot({"peers": 8, "seeders": 2, "kbps": 1500})  # no health yet
    assert m.stats.speed == 0.0            # keep-up not measurable yet
    assert m.stats.peers == 8
    assert m.stats.seeders == 2
    assert m.stats.download_kbps == 1500


def test_media_probe_fills_resolution_and_fps():
    """Streamflow ranks ties by resolution then fps and syncs both to
    Dispatcharr; OpenStream's media probe provides them."""
    m = _monitor()
    m._apply_snapshot({
        "media": {"width": 1920, "height": 1080, "fps": 25, "bitrateKbps": 6000, "videoCodec": "H.264"},
        "health": {"state": "healthy", "keepUpMargin": 1.0, "trueBitrateKbps": 5800},
    })
    assert (m.stats.width, m.stats.height, m.stats.fps) == (1920, 1080, 25)
    assert m.stats.bitrate == 5800  # the VBR-correct edge bitrate wins over the probe's


def test_media_bitrate_used_before_health_exists():
    m = _monitor()
    m._apply_snapshot({"media": {"bitrateKbps": 6000}})
    assert m.stats.bitrate == 6000


def test_latency_prefers_behind_live_secs():
    m = _monitor()
    m._apply_snapshot({"behindLiveSecs": 8.6, "health": {"state": "healthy", "keepUpMargin": 1.0, "latencySecs": 3}})
    assert m.stats.latency_secs == 9
    m._apply_snapshot({"behindLiveSecs": -1, "health": {"state": "healthy", "keepUpMargin": 1.0, "latencySecs": 3}})
    assert m.stats.latency_secs == 3  # unknown -> fall back
