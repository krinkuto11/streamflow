from flask import Blueprint, jsonify, request
from sqlalchemy import func, case
from datetime import datetime, timedelta
import logging

from apps.telemetry.telemetry_db import (
    CHANGELOG_RETENTION_DAYS,
    ChannelHealth,
    Run,
    StreamTelemetry,
    get_session,
)

logger = logging.getLogger(__name__)

telemetry_bp = Blueprint('telemetry', __name__)


def _requested_days() -> int:
    """Keep API query windows aligned with the retained telemetry history."""
    try:
        requested = int(request.args.get('days', 7))
    except (TypeError, ValueError):
        requested = 7
    return min(CHANGELOG_RETENTION_DAYS, max(1, requested))

@telemetry_bp.route('/global', methods=['GET'])
def get_global_telemetry():
    """Get global telemetry statistics (runs over time)."""
    days = _requested_days()
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    session = get_session()
    try:
        runs = session.query(Run).filter(Run.timestamp >= cutoff).order_by(Run.timestamp.asc()).all()
        
        data = []
        for r in runs:
            data.append({
                "id": r.id,
                "timestamp": r.timestamp.isoformat(),
                "duration_seconds": r.duration_seconds,
                "total_channels": r.total_channels,
                "total_streams": r.total_streams,
                "global_dead_count": r.global_dead_count,
                "global_revived_count": r.global_revived_count,
                "run_type": r.run_type
            })
            
        return jsonify({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Error fetching global telemetry: {e}")
        return jsonify({"success": False, "error": "An internal error has occurred."}), 500
    finally:
        session.close()

@telemetry_bp.route('/providers', methods=['GET'])
def get_provider_telemetry():
    """Get stream resilience and stats by provider."""
    days = _requested_days()
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    session = get_session()
    try:
        # We want to aggregate streams by provider over the last N days
        # We calculate total checked, total dead, average quality, avg bitrate
        stats = session.query(
            StreamTelemetry.provider_id,
            func.count(func.distinct(StreamTelemetry.stream_id)).label('total_streams'),
            func.count(func.distinct(case((StreamTelemetry.is_dead == True, StreamTelemetry.stream_id), else_=None))).label('dead_streams'),
            func.avg(StreamTelemetry.bitrate_kbps).label('avg_bitrate_kbps'),
            func.avg(StreamTelemetry.fps).label('avg_fps'),
            func.avg(StreamTelemetry.quality_score).label('avg_quality_score'),
            func.avg(StreamTelemetry.resolution_height).label('avg_res_height')
        ).join(Run).filter(Run.timestamp >= cutoff).group_by(StreamTelemetry.provider_id).all()
        
        # We need to map provider_id back to provider name. We can do this safely via UDI if accessible
        from apps.udi import get_udi_manager
        udi = get_udi_manager()
        accounts = {acc['id']: acc['name'] for acc in udi.get_m3u_accounts()} if udi else {}
        
        # Second query: Resolution Height Distribution (Stacked Bars)
        res_stats = session.query(
            StreamTelemetry.provider_id,
            StreamTelemetry.resolution_height,
            func.count(func.distinct(StreamTelemetry.stream_id)).label('count')
        ).join(Run).filter(Run.timestamp >= cutoff, StreamTelemetry.resolution_height.isnot(None)).group_by(
            StreamTelemetry.provider_id, StreamTelemetry.resolution_height
        ).all()
        
        provider_res_map = {}
        for pid, height, count in res_stats:
            if pid not in provider_res_map:
                provider_res_map[pid] = {"res_2160p": 0, "res_1080p": 0, "res_720p": 0, "res_576p": 0, "res_SD": 0}
            cat = "res_SD"
            if height >= 2160: cat = "res_2160p"
            elif height >= 1080: cat = "res_1080p"
            elif height >= 720: cat = "res_720p"
            elif height >= 576: cat = "res_576p"
            provider_res_map[pid][cat] += count

        data = []
        for s in stats:
            provider_id = s.provider_id
            res_breakdown = provider_res_map.get(provider_id, {"res_2160p": 0, "res_1080p": 0, "res_720p": 0, "res_576p": 0, "res_SD": 0})
            
            dead_streams = int(s.dead_streams) if s.dead_streams else 0
            availability_percentage = 100 - (dead_streams / s.total_streams * 100) if s.total_streams else 100
            item = {
                "provider_id": provider_id,
                "provider_name": accounts.get(provider_id, f"Provider {provider_id}" if provider_id else "Unknown Account"),
                "total_streams": s.total_streams,
                "dead_streams": dead_streams,
                "availability_percentage": availability_percentage,
                # Preserve the misspelled V7 field for existing API consumers.
                "availability_pecentage": availability_percentage,
                "avg_bitrate_kbps": round(s.avg_bitrate_kbps or 0, 2),
                "avg_fps": round(s.avg_fps or 0, 2),
                "avg_quality_score": round(s.avg_quality_score or 0, 2),
                "avg_res_height": round(s.avg_res_height or 0, 0)
            }
            item.update(res_breakdown)
            data.append(item)
            
        return jsonify({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Error fetching provider telemetry: {e}")
        return jsonify({"success": False, "error": "An internal error has occurred."}), 500
    finally:
        session.close()

@telemetry_bp.route('/channels/list', methods=['GET'])
def list_channels_for_telemetry():
    """Get a distinct list of channels that have telemetry data."""
    session = get_session()
    try:
        results = session.query(ChannelHealth.channel_id, ChannelHealth.channel_name).distinct().all()
        data = [{"id": r.channel_id, "name": r.channel_name or f"Channel {r.channel_id}"} for r in results]
        return jsonify({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Error fetching channel list for telemetry: {e}")
        return jsonify({"success": False, "error": "An internal error has occurred."}), 500
    finally:
        session.close()

@telemetry_bp.route('/channels/<int:channel_id>', methods=['GET'])
def get_channel_telemetry(channel_id):
    """Get history for a specific channel."""
    days = _requested_days()
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    session = get_session()
    try:
        history = session.query(ChannelHealth, Run.timestamp).join(Run).filter(
            ChannelHealth.channel_id == channel_id,
            Run.timestamp >= cutoff
        ).order_by(Run.timestamp.asc()).all()
        
        data = []
        for h, ts in history:
            data.append({
                "run_id": h.run_id,
                "timestamp": ts.isoformat(),
                "channel_name": h.channel_name,
                "offline": h.offline,
                "available_streams": h.available_streams,
                "dead_streams": h.dead_streams
            })
            
        return jsonify({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Error fetching channel telemetry: {e}")
        return jsonify({"success": False, "error": "An internal error has occurred."}), 500
    finally:
        session.close()
