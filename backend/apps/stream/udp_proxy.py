"""Shared UDP-to-HTTP stream proxy runtime utilities."""

import queue
import socket
import threading
import time
from datetime import datetime

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


class StreamProxy:
    """Manages a single UDP listener for a stream and broadcasts to multiple HTTP clients."""

    def __init__(self, stream_id, manager):
        self.stream_id = stream_id
        self.manager = manager
        self.port = 30000 + stream_id
        self.clients = {}  # dict of queue.Queue -> bool (needs_resync)
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.last_client_time = None
        self.LINGER_SECONDS = 60
        self.last_drop_log_time = 0

    def add_client(self):
        # Buffer for about 2-4 seconds of stream (2000 packets)
        client_queue = queue.Queue(maxsize=2000)
        with self.lock:
            # Always start in resync mode so the client waits for the first
            # valid 0x47 MPEG-TS sync byte.
            self.clients[client_queue] = True
            if not self.running:
                self._start()
        return client_queue

    def remove_client(self, client_queue):
        with self.lock:
            if client_queue in self.clients:
                del self.clients[client_queue]
            if not self.clients:
                self.last_client_time = datetime.now()
                logger.info(
                    f"Last client disconnected from stream {self.stream_id}. "
                    f"Entering {self.LINGER_SECONDS}s linger period."
                )

    def _start(self):
        self.running = True
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"ProxyUDP-{self.stream_id}",
        )
        self.thread.start()
        logger.info(f"Started shared UDP listener for stream {self.stream_id} on port {self.port}")

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", self.port))
            sock.settimeout(1.0)
            residual = b""
            while self.running:
                try:
                    data, _ = sock.recvfrom(65535)
                except socket.timeout:
                    with self.lock:
                        if not self.clients and self.last_client_time:
                            elapsed = (datetime.now() - self.last_client_time).total_seconds()
                            if elapsed >= self.LINGER_SECONDS:
                                logger.info(
                                    f"Linger timeout reached for stream {self.stream_id}. "
                                    "Shutting down listener."
                                )
                                self.running = False
                                self.manager.remove_proxy(self.stream_id)
                                break
                    continue

                if not data:
                    continue

                data = residual + data
                excess = len(data) % 188
                if excess > 0:
                    residual = data[-excess:]
                    data = data[:-excess]
                else:
                    residual = b""

                if not data:
                    continue

                with self.lock:
                    if not self.clients:
                        if self.last_client_time:
                            elapsed = (datetime.now() - self.last_client_time).total_seconds()
                            if elapsed >= self.LINGER_SECONDS:
                                logger.info(
                                    f"Linger timeout reached during data recv for stream {self.stream_id}. "
                                    "Shutting down."
                                )
                                self.running = False
                                self.manager.remove_proxy(self.stream_id)
                                break
                        continue

                    self.last_client_time = None

                    for client_queue, needs_resync in list(self.clients.items()):
                        try:
                            if needs_resync:
                                if data and data[0] == 0x47:
                                    self.clients[client_queue] = False
                                else:
                                    continue

                            client_queue.put_nowait(data)
                        except queue.Full:
                            self.clients[client_queue] = True
                            try:
                                while not client_queue.empty():
                                    client_queue.get_nowait()
                            except queue.Empty:
                                pass

                            now = time.time()
                            if now - self.last_drop_log_time > 5:
                                logger.warning(
                                    f"Shared proxy for {self.stream_id} buffer full - clearing queue "
                                    "(Jump-to-Live). Client processing too slow."
                                )
                                self.last_drop_log_time = now
        except Exception as exc:
            logger.error(f"UDP listener error for stream {self.stream_id}: {exc}")
        finally:
            sock.close()


class UDPProxyManager:
    """Manager for active UDP-to-HTTP proxies."""

    def __init__(self):
        self.proxies = {}
        self.lock = threading.Lock()

    def get_proxy(self, stream_id):
        with self.lock:
            if stream_id not in self.proxies:
                self.proxies[stream_id] = StreamProxy(stream_id, self)
            return self.proxies[stream_id]

    def remove_proxy(self, stream_id):
        with self.lock:
            if stream_id in self.proxies:
                del self.proxies[stream_id]
