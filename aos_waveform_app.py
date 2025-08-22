#!/usr/bin/env python3

from __future__ import annotations

import faulthandler, signal  # ensure segfaults print Python stack traces
faulthandler.enable(all_threads=True)

import argparse
import json
import os
import signal
import socket
import struct
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

# ---- Aetheros SDK ---------------------------------------------------
import aossdk.xsd as xsd
import aossdk.xsd_m2m as xsd_m2m
import aossdk.m2m as m2m
import aossdk.aos as aos
from aos import logDebug, logInfo, logWarn, logError

# ------------------------------------------------------------------------------
# Config dataclass
# ------------------------------------------------------------------------------
@dataclass
class AppConfig:
    content_interval_secs: int = 30
    fake_min_processing_time_us: int = 0
    max_stream_warning_interval_secs: int = 10
    data_post_rate: int = 500
    data_queue_max_length: int = 10
    dest_container: str = "/PN_CSE/GEISAWAVEFORM/cntWaveform"
    reconnect_delay_secs: int = 2
    local_test_mode: bool = False

# ------------------------------------------------------------------------------
# Fixed-length, rate-limited queue
# ------------------------------------------------------------------------------
class DataQueue:
    """
    Fixed-length thread-safe message queue, with retrieval rate limiting and abort state.

    When the queue fills, data can be dropped from the front or back of the queue,
    depending on the only_if_not_overflow parameter to postData().
    When only_if_not_overflow is False (the default), posting data when the queue is full will
    result in dropping the oldest entry to make way for the newest.
    If only_if_not_overflow is set to True, then the new data will not be added to the queue.
    In either case, the return value indicates overflow status.
    """

    def __init__(self, maxlen: int):
        self._mux = threading.Lock()
        self._cond = threading.Condition(self._mux)
        self._queue: deque[Tuple[int, bytes]] = deque()
        self._maxlen = maxlen  # when set to 0 => aborted
        self._seq = 0

        # stats
        self._last_measure_ts = 0.0
        self._posts = 0
        self._drops = 0
        self._gets = 0

    def abort(self):
        """Abort the queue (set maxlen=0) and wake up any waiting consumer."""
        with self._cond:
            self._maxlen = 0
            self._cond.notify_all()

    def full(self) -> bool:
        """Check if the queue is full. When full, postData would report overflow."""
        with self._mux:
            return self._maxlen > 0 and len(self._queue) == self._maxlen

    def _log_stats_if_due(self):
        now = time.time()
        stat_rate = 60.0
        if self._last_measure_ts == 0.0:
            self._last_measure_ts = now
            return
        if now - self._last_measure_ts >= stat_rate:
            secs = now - self._last_measure_ts
            ppm = round(self._posts / secs * 60) if secs > 0 else 0
            gpm = round(self._gets / secs * 60) if secs > 0 else 0
            dpm = round(self._drops / secs * 60) if secs > 0 else 0
            logInfo(f"queue stats per-minute: {ppm} posts, {gpm} gets, {dpm} drops")
            self._last_measure_ts = now
            self._posts = self._drops = self._gets = 0

    def postData(self, data: bytes, only_if_not_overflow: bool = False) -> bool:
        """Add data to the queue and notify consumer.
        An overflow condition exists if the queue is full and adding the new entry would cause the
        oldest entry to be removed to make room. Setting only_if_not_overflow to True prevents
        adding new data when the queue is already full.
        Returns True on overflow condition, False otherwise.
        """
        overflow = False
        with self._cond:
            if self._maxlen == 0:
                return False
            overflow = (len(self._queue) == self._maxlen)

            # stats
            self._posts += 1
            if overflow:
                self._drops += 1
            self._log_stats_if_due()

            if overflow:
                if only_if_not_overflow:
                    return True  # overflow occurred, we didn't add
                # drop oldest in favor of new
                self._queue.popleft()

            self._queue.append((self._seq, data))
            self._seq += 1
            self._cond.notify()

        return overflow

    def getData(self, not_sooner_than: float) -> Optional[Tuple[int, bytes]]:
        """Remove the oldest data entry from the queue.
        Pop data from front of the queue, with rate limiting and gap and abort detection.
        Returns None if in aborted state; otherwise returns (seq, data).
        """
        with self._cond:
            while self._maxlen > 0 and not self._queue:
                self._cond.wait(timeout=0.25)

            # rate limit until not_sooner_than
            while self._maxlen > 0 and time.time() < not_sooner_than:
                sleep_s = max(0.0, not_sooner_than - time.time())
                logInfo(f"Sleeping {int(round(sleep_s))} seconds")
                self._cond.wait(timeout=sleep_s)

            if self._maxlen == 0:
                return None

            seq, data = self._queue.popleft()
            self._gets += 1
            return seq, data

# ------------------------------------------------------------------------------
# oneM2M App + publisher thread
# ------------------------------------------------------------------------------
class WaveformApp:
    """
    Encapsulates oneM2M AE functionality, and manages sending data upstream as ContentInstances.
    The publisher thread pulls data off the DataQueue and publishes it to a oneM2M container.
    """
    def __init__(self, dq: DataQueue, local_test_mode: bool, max_instances: int = 2):
        self.app = m2m.AppEntity(True)
        self.app.setNotificationHandler(self._notification_cb)
        self.dq = dq
        self.local_test_mode = local_test_mode
        self.max_instances = max_instances
        self.publisher = threading.Thread(target=self._run, daemon=True)
        self._last_data_seqnum = -1
        self._content_interval = 30
        self._dest_container = "/PN_CSE/GEISAWAVEFORM/cntWaveform"
        self._stop = threading.Event()

    def start(self, content_interval_secs: int, dest_container: str) -> bool:
        self._content_interval = int(content_interval_secs)
        self._dest_container = dest_container

        if self.local_test_mode:
            logInfo("Local test mode: skipping AE activation and container creation; "
                    "contentInstances will NOT be posted.")
            self.publisher.start()
            return True

        # Activate with retry/back-off
        backoff = 30
        while not self.app.activate():
            reason = self.app.getActivationFailureReason()
            logError(f"App activation failed: reason: {reason} - trying again in {backoff} seconds")
            time.sleep(backoff)
            backoff = min(backoff * 2, 16 * 60)
        logInfo("App activated")

        # Ensure destination container exists
        parent, name = self._split_path(self._dest_container)
        if parent.endswith("/ram") and not parent.startswith("/"):
            # create the local RAM container on the AE to avoid flash wear
            base_parent, _ = self._split_path(parent)
            if not self._ensure_simple_container(base_parent, "ram", self.max_instances):
                logError(f"Failed to ensure ram container under '{base_parent}'")
                return False

        if not self._ensure_simple_container(parent, name, self.max_instances):
            logError(f"Failed to ensure container '{self._dest_container}'")
            return False

        self.publisher.start()
        return True

    def stop(self):
        self._stop.set()
        self.publisher.join(timeout=2.0)

    def _notification_cb(self, notification):
        """Notification callback for oneM2M events"""
        ev = getattr(notification, "notificationEvent", None)
        if ev is None:
            logWarn("notification has no notificationEvent")
            return
        et = getattr(ev, "notificationEventType", None)
        if et != xsd_m2m.NotificationEventType_Create_of_Direct_Child_Resource:
            logWarn(f"got notification type {et}")

    def _run(self):
        """Pull data from DataQueue and post as ContentInstances at a rate limited by content_interval."""
        next_at = time.time()
        while not self._stop.is_set():
            item = self.dq.getData(not_sooner_than=next_at)
            if item is None:
                return  # aborted
            seq, payload = item
            next_at = time.time() + self._content_interval

            if self._last_data_seqnum >= 0 and seq > self._last_data_seqnum + 1:
                logWarn(f"{seq - (self._last_data_seqnum + 1)} data payloads dropped by dataQueue")

            if self.local_test_mode:
                logInfo(f"[LOCAL TEST] Would post payload #{seq}, size: {len(payload)} bytes")
                self._last_data_seqnum = seq
                continue

            try:
                # Build xs:anyType for binary
                any_type = xsd.toAnyTypeUnnamed(payload)
                rsp = self.app.createSimpleContentInstance(self._dest_container, any_type)
                if rsp.responseStatusCode != xsd_m2m.ResponseStatusCode_CREATED:
                    logError(f"data #{seq} contentInstance create failed: {rsp.responseStatusCode}")
                else:
                    logInfo(f"Posted data payload #{seq}, size: {len(payload)} bytes")
                self._last_data_seqnum = seq
            except Exception as e:
                logError(f"Exception while posting data payload #{seq}: {e}")

    def _split_path(self, full: str) -> Tuple[str, str]:
        idx = full.rfind("/")
        if idx <= 0:
            name = full if idx < 0 else full[idx+1:]
            return f"./{self.app.getResourceName()}", name
        return full[:idx], full[idx+1:]

    def _ensure_simple_container(self, parent: str, name: str, max_instances: int) -> bool:
        cnt = xsd_m2m.Container.Create()
        cnt.creator = ""
        cnt.resourceName = name
        cnt.maxNrOfInstances = max_instances

        to = m2m.To()
        to.to = parent
        req = self.app.newRequest(xsd_m2m.Operation_Create, to)
        req.req.resourceType = xsd_m2m.ResourceType_container
        req.req.resultContent = xsd_m2m.ResultContent_Nothing
        req.req.primitiveContent = xsd_m2m.toAnyNamed(cnt)

        self.app.sendRequest(req)
        rsp = self.app.getResponse(req)
        return rsp.responseStatusCode in (
            xsd_m2m.ResponseStatusCode_CREATED,
            xsd_m2m.ResponseStatusCode_CONFLICT,
        )

# ------------------------------------------------------------------------------
# Stream consumer: read frames from UNIX socket, compress, enqueue
# ------------------------------------------------------------------------------
class StreamConsumer:
    """
    Consume and process streaming data, as GEISA formatted frames from aos.waveform_provider.
    Posts compressed data to DataQueue at a configured rate (post_data_rate).
    """
    def __init__(self, dq: DataQueue, post_data_rate: int, fake_min_us: int,
                 reconnect_delay_secs: int, max_warn_interval_secs: int):
        self.dq = dq
        self.post_data_rate = max(1, int(post_data_rate))
        self.fake_min_us = max(0, int(fake_min_us))
        self.reconnect_delay_secs = max(1, int(reconnect_delay_secs))
        self.warn_interval = max(1, int(max_warn_interval_secs))
        self._counter = 0
        self._expected_seq: Optional[int] = None
        self._last_warn_ts = 0.0
        self._md: Optional[aos.WaveformMetadata] = None
        self._fd: int = -1


    # --- Metadata helpers -----------------------------------------------------
    def _get_metadata(self) -> Optional[aos.WaveformMetadata]:
        """Try to retrieve waveform metadata via SDK.
        Returns metadata
        or None if unavailable.
        """
        try:
            res = aos.RpcResult()
            md = aos.getMetadata(res)

            if not res:
                logInfo(f"Metadata not ready yet or comm failure")
                return None
            return md
        except Exception:
            logWarn("Exception during metadata")
            return None

    # --- Frame parsing --------------------------------------------------------
    def _elem_size(self) -> int:
        if self._md.data_type == aos.DataType_i16:
            return 2
        if self._md.data_type == aos.DataType_i32:
            return 4
        return 4

    def _check_and_seq(self, frame: bytes) -> Tuple[bool, Optional[int]]:
        """Validate length vs. stride and extract sequence number"""
        total_channels = (self._md.total_channels if self._md else 0)
        stride = total_channels * self._elem_size()
        if stride <= 0:
            return True, None  # can't validate without metadata
        if len(frame) < aos.geisa_waveform_frame_size():
            logWarn(f"short frame: {len(frame)}")
            return False, None
        payload = len(frame) - aos.geisa_waveform_frame_size()
        if payload % stride != 0:
            logWarn(f"bad frame: payload={payload} stride={stride}")
            return False, None
        try:
            seq, = struct.unpack_from("<I", frame, 8)
        except struct.error:
            seq = None
        return True, seq

    # --- Compression with minimum processing time ----------------------------
    def _compress_with_floor(self, raw: bytes) -> bytes:
        start = time.perf_counter()
        out = zlib.compress(raw)
        if self.fake_min_us <= 0:
            return out
        # Busy-recompress to simulate work
        while (time.perf_counter() - start) * 1e6 < self.fake_min_us:
            out = zlib.compress(raw)
        return out

    # --- Warning rate limiter -------------------------------------------------
    def _warn_rate_limited(self, msg: str):
        now = time.time()
        if now - self._last_warn_ts >= self.warn_interval:
            logWarn(msg)
            self._last_warn_ts = now

    # --- Main loop ------------------------------------------------------------
    def run(self):
        logInfo("Starting read and post loop")

        while True:

            # Get metadata
            if self._md is None:
                md = self._get_metadata()
                if md is None:
                    logInfo("waiting for GEISA metadata")
                    time.sleep(5)
                    continue

                self._md = md
                logInfo(f"GEISA metadata: total_channels={md.total_channels} "
                        f"voltage_channels={md.voltage_channels} "
                        f"current_channels={md.current_channels}")

            if self._fd == -1:
                # Open provider socket via SDK helper
                try:
                    self._fd = aos.open_socket(0)
                    if self._fd < 0:
                        raise RuntimeError("aos.open_socket returned invalid fd")

                except Exception as e:
                    logError(f"could not open waveform provider socket: {e}")
                    time.sleep(self.reconnect_delay_secs)
                    continue

            # Read loop
            try:
                while True:
                    try:
                        frame = os.read(self._fd, 65536)
                    except InterruptedError:
                        continue

                    if not frame:
                        raise RuntimeError("waveform provider read failed (EOF)")

                    ok, seq = self._check_and_seq(frame)
                    if not ok:
                        continue

                    # sequence gap tracking
                    if seq is not None:
                        if self._expected_seq is None:
                            self._expected_seq = seq
                        elif seq != self._expected_seq:
                            self._warn_rate_limited(f"sequence gap: got {seq} expected {self._expected_seq}")
                        self._expected_seq = (seq + 1) & 0xFFFFFFFF

                    # compress and enqueue every N frames
                    comp = self._compress_with_floor(frame)
                    self._counter += 1
                    if self._counter >= self.post_data_rate:
                        self.dq.postData(comp)
                        self._counter = 0

            except Exception as e:
                logError(f"waveform provider read failed: {e}")
            finally:
                try:
                    if self._fd >= 0:
                        os.close(self._fd)
                        self._fd = -1
                except Exception:
                    pass

            time.sleep(self.reconnect_delay_secs)

# ------------------------------------------------------------------------------
# App config setup
# ------------------------------------------------------------------------------
def load_config(path: str) -> AppConfig:
    cfg = AppConfig()
    try:
        with open(path, "r") as f:
            js = json.load(f)
        if js.get("debug"):
            aos.setLogLevel(aos.LogLevel_LOG_DEBUG)
        if "content-interval-secs" in js:
            cfg.content_interval_secs = int(js["content-interval-secs"])
        if "fake-min-processing-time-us" in js:
            cfg.fake_min_processing_time_us = int(js["fake-min-processing-time-us"])
        if "max-stream-warning-interval-secs" in js:
            cfg.max_stream_warning_interval_secs = int(js["max-stream-warning-interval-secs"])
        if "max-stream-warning-rate-secs" in js:
            cfg.max_stream_warning_interval_secs = int(js["max-stream-warning-rate-secs"])
        if "post-data-rate" in js:
            cfg.data_post_rate = int(js["post-data-rate"])
        if "data-queue-max-length" in js:
            cfg.data_queue_max_length = int(js["data-queue-max-length"])
        if "dest-container" in js:
            cfg.dest_container = str(js["dest-container"])
        if "reconnect-delay-secs" in js:
            cfg.reconnect_delay_secs = int(js["reconnect-delay-secs"])
        if "local-test-mode" in js:
            cfg.local_test_mode = bool(js["local-test-mode"])

    except FileNotFoundError:
        logWarn("waveform_config.json not found; using defaults")
    except Exception as e:
        logError(f"Error reading waveform_config.json: {e}")
    return cfg

_shutdown = threading.Event()
def _handle_sig(signum, frame):
    _shutdown.set()

def main():
    logInfo("Starting app")
    app = aos.AppMain()

    parser = argparse.ArgumentParser()
    parser.add_argument("-d", action="store_true", help="Enable debug logging")
    parser.add_argument("--config", default="waveform_config.json")
    args = parser.parse_args()

    if args.d:
        aos.setLogLevel(aos.LogLevel_LOG_DEBUG)

    cfg = load_config(args.config)

    if cfg.dest_container.startswith("/") and cfg.dest_container.rfind("/") == 0:
        logError(f"invalid container path {cfg.dest_container}")
        return 1

    logInfo(f"contentInterval: {cfg.content_interval_secs}s")
    logInfo(f"fakeMinimumProcessingTime: {cfg.fake_min_processing_time_us}us")
    logInfo(f"maxStreamWarningInterval: {cfg.max_stream_warning_interval_secs}s")
    logInfo(f"postDataRate: {cfg.data_post_rate}")
    logInfo(f"dataQueueMaxLength: {cfg.data_queue_max_length}")
    logInfo(f"destination container: {cfg.dest_container}")

    dq = DataQueue(cfg.data_queue_max_length)
    app = WaveformApp(dq, cfg.local_test_mode)

    # signals
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    if not app.start(cfg.content_interval_secs, cfg.dest_container):
        return 1

    consumer = StreamConsumer(
        dq,
        post_data_rate=cfg.data_post_rate,
        fake_min_us=cfg.fake_min_processing_time_us,
        reconnect_delay_secs=cfg.reconnect_delay_secs,
        max_warn_interval_secs=cfg.max_stream_warning_interval_secs,
    )

    # run consumer in foreground; shutdown gracefully on signal
    t = threading.Thread(target=consumer.run, daemon=True)
    t.start()

    while not _shutdown.is_set():
        time.sleep(0.25)

    logInfo("disabling data queue")
    dq.abort()
    app.stop()
    logInfo("Exiting App...")
    return 0

if __name__ == "__main__":
    raise main()