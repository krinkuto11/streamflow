import os
import json
from datetime import datetime
from sqlalchemy.orm import sessionmaker

from apps.core.logging_config import setup_logging
logger = setup_logging(__name__)

# Re-exports from main database context
from apps.database.connection import get_session
from apps.database.models import Run, ChannelHealth, StreamTelemetry
from apps.telemetry.run_classification import classify_run_metadata

def _sanitize_bitrate(bitrate_str):
    if not bitrate_str:
        return None
    try:
        if isinstance(bitrate_str, (int, float)):
            return int(bitrate_str)
        s = str(bitrate_str).lower().replace(' ', '')
        if 'mbps' in s:
            return int(float(s.replace('mbps', '')) * 1000)
        elif 'kbps' in s:
            return int(float(s.replace('kbps', '')))
        return int(float(s))
    except ValueError:
        return None

def _sanitize_fps(fps_str):
    if not fps_str:
        return None
    try:
        if isinstance(fps_str, (int, float)):
            return float(fps_str)
        s = str(fps_str).lower().replace(' fps', '').strip()
        return float(s)
    except ValueError:
        return None

def _sanitize_resolution(res_str):
    if not res_str or 'x' not in str(res_str).lower():
        return None, None
    try:
        parts = str(res_str).lower().split('x')
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None

def _get_provider_id(m3u_account_name, session):
    from apps.udi import get_udi_manager
    udi = get_udi_manager()
    accounts = udi.get_m3u_accounts() if udi else []
    if accounts:
        for acc in accounts:
            if acc.get('name') == m3u_account_name or acc.get('id') == m3u_account_name:
                return acc.get('id')
    return None

def save_automation_run_telemetry(action, details, subentries=None, timestamp=None):
    """
    Parses the raw JSON details and inserts to the relational database.
    Replaces ChangelogManager logic.
    """
    session = get_session()
    try:
        run_ts = datetime.utcnow()
        if timestamp:
            try:
                run_ts = datetime.fromisoformat(timestamp)
            except:
                pass

        details = details or {}
        job_metadata = classify_run_metadata(action, details, subentries)

        # Create Run record
        duration = details.get('duration_seconds', details.get('duration', 0.0))
        if isinstance(duration, str):
            try: 
                duration = float(duration.lower().replace('s', '').strip())
            except: 
                duration = 0.0
            
        global_stats = details.get('global_stats', {})
        total_channels = details.get('total_channels_processed') or details.get('total_channels') or global_stats.get('total_channels_processed', 0)
        total_streams = details.get('total_streams', 0) or global_stats.get('total_streams', 0)
        dead_count = details.get('total_dead_streams') or details.get('dead_streams') or global_stats.get('total_dead_streams', 0)
        revived_count = details.get('total_revived_streams') or details.get('streams_revived') or global_stats.get('total_revived_streams', 0)

        run = Run(
            timestamp=run_ts,
            duration_seconds=duration,
            total_channels=total_channels,
            total_streams=total_streams,
            global_dead_count=dead_count,
            global_revived_count=revived_count,
            run_type=action,
            job_category=job_metadata["job_category"],
            job_outcome=job_metadata["job_outcome"],
            job_subject_ref=job_metadata["job_subject_ref"],
            job_correlation_id=job_metadata["job_correlation_id"],
            raw_details=json.dumps(details) if details else None,
            raw_subentries=json.dumps(subentries) if subentries else None
        )
        session.add(run)
        session.flush() # Get run.id

        # Process automation_run structure
        periods = details.get('periods', [])
        if not periods and 'summary' in details:
            periods = details.get('summary', {}).get('periods', [])
        
        for p in periods:
            for c in p.get('channels', []):
                channel_id = c.get('channel_id')
                channel_name = c.get('channel_name')
                
                channel_health = ChannelHealth(
                    run_id=run.id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    offline=False, # We don't have exactly this logic yet, maybe if available_streams==0
                    available_streams=0,
                    dead_streams=0
                )
                session.add(channel_health)
                session.flush()
                
                for step in c.get('steps', []):
                    if step.get('step') == 'Quality Check':
                        step_details = step.get('details', {})
                        dead_streams = step_details.get('dead_streams', [])
                        checked_streams = step_details.get('checked_streams', [])
                        
                        channel_health.dead_streams += len(dead_streams)
                        channel_health.available_streams += len(checked_streams)
                        channel_health.offline = (channel_health.available_streams == 0 and channel_health.dead_streams > 0)
                        
                        # Process dead streams
                        from apps.udi import get_udi_manager
                        udi = get_udi_manager()
                        
                        for ds in dead_streams:
                            stream_id = ds.get('id', ds.get('stream_id', 0))
                            provider_id = None
                            try:
                                provider_ident = ds.get('m3u_account')
                                if provider_ident:
                                    provider_id = _get_provider_id(provider_ident, session)
                                
                                if not provider_id and udi:
                                    stream_obj = udi.get_stream_by_id(stream_id)
                                    if stream_obj:
                                        provider_id = stream_obj.get('m3u_account_id')
                            except: pass

                            dtel = StreamTelemetry(
                                run_id=run.id,
                                channel_id=channel_id,
                                stream_id=stream_id,
                                provider_id=provider_id,
                                is_dead=True
                            )
                            session.add(dtel)
                        
                        # Process checked (healthy) streams
                        for cs in checked_streams:
                            width, height = _sanitize_resolution(cs.get('resolution'))
                            provider_ident = cs.get('m3u_account')
                            dtel = StreamTelemetry(
                                run_id=run.id,
                                channel_id=channel_id,
                                stream_id=cs.get('stream_id', 0),
                                provider_id=_get_provider_id(provider_ident, session),
                                bitrate_kbps=_sanitize_bitrate(cs.get('bitrate')),
                                resolution_width=width,
                                resolution_height=height,
                                fps=_sanitize_fps(cs.get('fps')),
                                codec=cs.get('video_codec'),
                                audio_codec=cs.get('audio_codec'),
                                quality_score=cs.get('score'),
                                is_dead=False,
                                is_hdr=bool(cs.get('hdr_format') or cs.get('is_hdr'))
                            )
                            session.add(dtel)
                            
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Error saving telemetry data: {e}", exc_info=True)
    finally:
        session.close()

def save_generic_telemetry(action, details, subentries=None, timestamp=None):
    """
    Fallback method to handle single checks or older playlist updates.
    Parse subentries to save stream telemetry.
    """
    session = get_session()
    try:
        run_ts = datetime.utcnow()
        if timestamp:
            try:
                run_ts = datetime.fromisoformat(timestamp)
            except: pass

        details = details or {}
        job_metadata = classify_run_metadata(action, details, subentries)

        run = Run(
            timestamp=run_ts,
            duration_seconds=details.get('duration_seconds', details.get('duration', 0.0)),
            total_channels=details.get('total_channels', 0) or details.get('total_channels_processed', 0),
            total_streams=details.get('total_streams', 0),
            global_dead_count=details.get('total_dead_streams', 0) or details.get('dead_streams', 0),
            global_revived_count=details.get('total_revived_streams', 0),
            run_type=action,
            job_category=job_metadata["job_category"],
            job_outcome=job_metadata["job_outcome"],
            job_subject_ref=job_metadata["job_subject_ref"],
            job_correlation_id=job_metadata["job_correlation_id"],
            raw_details=json.dumps(details) if details else None,
            raw_subentries=json.dumps(subentries) if subentries else None
        )
        session.add(run)
        session.flush()

        if subentries:
            for group in subentries:
                if group.get('group') == 'check':
                    for item in group.get('items', []):
                        cid = item.get('channel_id')
                        cname = item.get('channel_name')
                        stats = item.get('stats', {})
                        
                        ch = ChannelHealth(
                            run_id=run.id,
                            channel_id=cid,
                            channel_name=cname,
                            available_streams=stats.get('total_streams', 0) - stats.get('dead_streams', 0),
                            dead_streams=stats.get('dead_streams', 0)
                        )
                        session.add(ch)
                        session.flush()

                        # Save individual stream stats if present in generic item
                        stream_details = stats.get('stream_details', [])
                        for s_det in stream_details:
                            width, height = _sanitize_resolution(s_det.get('resolution'))
                            provider_ident = s_det.get('m3u_account')
                            dtel = StreamTelemetry(
                                run_id=run.id,
                                channel_id=cid,
                                stream_id=s_det.get('stream_id', 0),
                                provider_id=_get_provider_id(provider_ident, session),
                                bitrate_kbps=_sanitize_bitrate(s_det.get('bitrate')),
                                resolution_width=width,
                                resolution_height=height,
                                fps=_sanitize_fps(s_det.get('fps')),
                                codec=s_det.get('video_codec'),
                                audio_codec=s_det.get('audio_codec'),
                                quality_score=s_det.get('score'),
                                is_dead=(s_det.get('status') == 'dead'),
                                is_hdr=bool(s_det.get('hdr_format') or s_det.get('is_hdr'))
                            )
                            session.add(dtel)

        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Error saving generic telemetry: {e}", exc_info=True)
    finally:
        session.close()

