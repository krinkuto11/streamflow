import subprocess
import time
import os

import pytest

if os.environ.get("RUN_ROBUST_ROUTING_TESTS") != "1":
    pytest.skip(
        "robust routing test requires a live orchestrator/UDP stream fixture",
        allow_module_level=True,
    )

fcntl = pytest.importorskip("fcntl", reason="requires Unix fcntl")

def test_ffmpeg_robust_routing():
    # Use a dummy but valid URL or local file if possible. 
    # For now, let's stick to a known reachable URL or a short local video if available.
    url = "http://orchestrator:8000/ace/getstream?id=c6a824a81b36f6c3529826477caf2ad94b45776d"
    port_a = 15000
    port_b = 25000
    
    # Robust multi-output command with fifo
    # Note: We repeat -map 0 for each output to be safe
    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-i', url,
        '-map', '0', '-c', 'copy', '-f', 'fifo', '-fifo_format', 'mpegts', '-drop_pkts_on_overflow', '1', '-attempt_recovery', '1', f'udp://127.0.0.1:{port_a}',
        '-map', '0', '-c', 'copy', '-f', 'fifo', '-fifo_format', 'mpegts', '-drop_pkts_on_overflow', '1', '-attempt_recovery', '1', f'udp://127.0.0.1:{port_b}',
        '-map', '0', '-c', 'copy', '-f', 'null', '-'
    ]
    
    print(f"Running command: {' '.join(cmd)}")
    
    # Start the primary router
    primary = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0 # No buffering
    )
    
    # Start sidecar listeners to verify content on both ports
    def start_listener(port):
        return subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-i', f'udp://127.0.0.1:{port}', '-t', '5', '-f', 'null', '-'],
            stderr=subprocess.PIPE
        )
    
    sidecar_a = start_listener(port_a)
    sidecar_b = start_listener(port_b)
    
    print("Monitoring progress for 15 seconds...")
    start_time = time.time()
    stats_found = False
    
    # Non-blocking setup for stderr
    fd = primary.stderr.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    
    while time.time() - start_time < 15:
        try:
            chunk = primary.stderr.read(1024)
            if chunk:
                text = chunk.decode('utf-8', errors='ignore')
                if 'speed=' in text:
                    print("Progress stats detected.")
                    stats_found = True
        except IOError:
            pass
            
        if primary.poll() is not None:
            print(f"Primary process died (code {primary.returncode})")
            break
            
        time.sleep(1)
    
    primary.terminate()
    sidecar_a.terminate()
    sidecar_b.terminate()
    
    # Check if listeners saw any data
    _, err_a = sidecar_a.communicate()
    _, err_b = sidecar_b.communicate()
    
    data_a = 'fps=' in err_a.decode('utf-8', errors='ignore')
    data_b = 'fps=' in err_b.decode('utf-8', errors='ignore')
    
    print(f"Data on Port A: {data_a}")
    print(f"Data on Port B: {data_b}")
    print(f"Stats on Stderr: {stats_found}")
    
    if data_a and data_b and stats_found:
        print("Verification SUCCESS")
    else:
        print("Verification FAILED")

if __name__ == "__main__":
    test_ffmpeg_robust_routing()
