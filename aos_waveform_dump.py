#!/usr/bin/env python3
from __future__ import annotations

import argparse
import faulthandler
import json
import math
import os
import signal
import socket
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

faulthandler.enable(all_threads=True)

try:
    from aossdk import aos
except (ImportError, OSError):  # Config and frame tests run off target.
    aos = None

try:
    from aos import logError as _log_error
    from aos import logInfo as _log_info
except (ImportError, OSError):
    _log_error = _log_info = None


DATA_TYPE_I16 = 0
FRAME_HEADER = struct.Struct("<qII")
MAX_FRAME_BYTES = 65536
DEFAULT_CONFIG = Path(__file__).with_name("waveform_dump_config.json")
STOP_EVENT = threading.Event()


def log_info(message: str) -> None:
    if _log_info is not None:
        _log_info(message)
    else:
        print(f"INFO: {message}", flush=True)


def log_error(message: str) -> None:
    if _log_error is not None:
        _log_error(message)
    else:
        print(f"ERROR: {message}", flush=True)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


def _json_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{path} must be an object")
    return value


def _json_number(value: Any, path: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{path} must be finite and at least {minimum}")
    return result


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        raw = json.load(stream, parse_constant=_reject_json_constant)
    cfg = _json_object(raw, "config")
    allowed = {
        "frames",
        "metadata-timeout-seconds",
        "channels",
        "output-csv",
        "output-result",
        "expect",
    }
    unknown = sorted(set(cfg) - allowed)
    if unknown:
        raise ValueError(f"unknown config setting(s): {', '.join(unknown)}")

    frames = cfg.get("frames", 16)
    if isinstance(frames, bool) or not isinstance(frames, int):
        raise TypeError("frames must be an integer")
    if not 1 <= frames <= 10000:
        raise ValueError("frames must be between 1 and 10000")

    metadata_timeout = _json_number(
        cfg.get("metadata-timeout-seconds", 30),
        "metadata-timeout-seconds",
        minimum=1.0,
    )
    if metadata_timeout > 120.0:
        raise ValueError("metadata-timeout-seconds must not exceed 120")

    channels = cfg.get("channels", ["Va", "Ia", "Ic"])
    if (
        not isinstance(channels, list)
        or not channels
        or any(not isinstance(item, str) or not item for item in channels)
    ):
        raise TypeError("channels must be a non-empty array of names")
    if len(set(channels)) != len(channels):
        raise ValueError("channels must not contain duplicates")

    output_csv = cfg.get("output-csv", "/home/apps/waveform_dump.csv")
    output_result = cfg.get(
        "output-result", "/home/apps/waveform_dump_result.json"
    )
    if not isinstance(output_csv, str) or not output_csv:
        raise TypeError("output-csv must be a non-empty path")
    if not isinstance(output_result, str) or not output_result:
        raise TypeError("output-result must be a non-empty path")

    expect = _json_object(cfg.get("expect", {}), "expect")
    expect_allowed = {
        "sampling-frequency-hz",
        "voltage-channels",
        "current-channels",
        "total-channels",
        "voltage-rms",
        "voltage-rms-tolerance",
        "total-real-power-watts",
        "real-power-tolerance-watts",
        "power-voltage-multiplier",
    }
    unknown_expect = sorted(set(expect) - expect_allowed)
    if unknown_expect:
        raise ValueError(
            f"unknown expect setting(s): {', '.join(unknown_expect)}"
        )
    for key in (
        "sampling-frequency-hz",
        "voltage-channels",
        "current-channels",
        "total-channels",
    ):
        if key in expect:
            value = expect[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"expect.{key} must be a non-negative integer")
    for key in (
        "voltage-rms",
        "voltage-rms-tolerance",
        "real-power-tolerance-watts",
        "power-voltage-multiplier",
    ):
        if key in expect:
            _json_number(expect[key], f"expect.{key}")
    if "total-real-power-watts" in expect:
        value = expect["total-real-power-watts"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("expect.total-real-power-watts must be a number")
        if not math.isfinite(float(value)):
            raise ValueError("expect.total-real-power-watts must be finite")

    return {
        "frames": frames,
        "metadata-timeout-seconds": metadata_timeout,
        "channels": channels,
        "output-csv": output_csv,
        "output-result": output_result,
        "expect": expect,
    }


def channel_names(voltage_channels: int, current_channels: int, total: int) -> list[str]:
    voltage = {
        1: ["Va"],
        2: ["Va", "Vc"],
        3: ["Va", "Vb", "Vc"],
    }.get(voltage_channels, [f"V{index + 1}" for index in range(voltage_channels)])
    current = {
        1: ["Ia"],
        2: ["Ia", "Ic"],
        3: ["Ia", "Ib", "Ic"],
    }.get(current_channels, [f"I{index + 1}" for index in range(current_channels)])
    names = voltage + current
    names.extend(f"Aux{index + 1}" for index in range(total - len(names)))
    return names


def metadata_dict(metadata: Any) -> dict[str, Any]:
    result = {
        "data-type": int(metadata.data_type),
        "voltage-channels": int(metadata.voltage_channels),
        "current-channels": int(metadata.current_channels),
        "total-channels": int(metadata.total_channels),
        "sampling-frequency-hz": int(metadata.sampling_frequency_hz),
        "voltage-scale": float(metadata.voltage_scale),
        "current-scale": float(metadata.current_scale),
        "frame-period-ms": int(metadata.frame_period_ms),
        "cycle-aligned": bool(metadata.cycle_aligned),
    }
    if result["data-type"] != DATA_TYPE_I16:
        raise ValueError(
            f"unsupported waveform data type {result['data-type']}; this app expects i16"
        )
    if result["total-channels"] <= 0:
        raise ValueError("metadata total_channels must be positive")
    if (
        result["voltage-channels"] + result["current-channels"]
        > result["total-channels"]
    ):
        raise ValueError("metadata voltage/current channel counts exceed total_channels")
    if result["sampling-frequency-hz"] <= 0:
        raise ValueError("metadata sampling_frequency_hz must be positive")
    for key in ("voltage-scale", "current-scale"):
        if not math.isfinite(result[key]) or result[key] <= 0.0:
            raise ValueError(f"metadata {key} must be finite and positive")
    return result


@dataclass(frozen=True)
class DecodedFrame:
    timestamp_ms: int
    sequence: int
    flags: int
    rows: list[tuple[float, ...]]


def decode_frame(frame: bytes, metadata: dict[str, Any], header_size: int) -> DecodedFrame:
    if header_size < FRAME_HEADER.size or len(frame) < header_size:
        raise ValueError(f"frame is shorter than its {header_size}-byte header")
    payload = frame[header_size:]
    row_bytes = metadata["total-channels"] * 2
    if not payload or len(payload) % row_bytes:
        raise ValueError(
            f"payload length {len(payload)} is not a positive multiple of {row_bytes}"
        )
    timestamp_ms, sequence, flags = FRAME_HEADER.unpack_from(frame)
    raw_values = [item[0] for item in struct.iter_unpack("<h", payload)]
    rows: list[tuple[float, ...]] = []
    total = metadata["total-channels"]
    voltage_channels = metadata["voltage-channels"]
    for offset in range(0, len(raw_values), total):
        physical = []
        for index, value in enumerate(raw_values[offset : offset + total]):
            scale = (
                metadata["voltage-scale"]
                if index < voltage_channels
                else metadata["current-scale"]
            )
            physical.append(value * scale)
        rows.append(tuple(physical))
    return DecodedFrame(timestamp_ms, sequence, flags, rows)


class Statistics:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.total_squared = 0.0
        self.minimum = math.inf
        self.maximum = -math.inf

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.total_squared += value * value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def result(self) -> dict[str, float | int]:
        if not self.count:
            raise ValueError("cannot summarize an empty channel")
        return {
            "samples": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.total / self.count,
            "rms": math.sqrt(self.total_squared / self.count),
        }


def _check_exact(checks: list[dict[str, Any]], name: str, actual: Any, expected: Any) -> None:
    checks.append(
        {"name": name, "pass": actual == expected, "actual": actual, "expected": expected}
    )


def _check_close(
    checks: list[dict[str, Any]],
    name: str,
    actual: float,
    expected: float,
    tolerance: float,
) -> None:
    checks.append(
        {
            "name": name,
            "pass": abs(actual - expected) <= tolerance,
            "actual": actual,
            "expected": expected,
            "tolerance": tolerance,
        }
    )


def evaluate(
    config: dict[str, Any],
    metadata: dict[str, Any],
    channel_stats: dict[str, dict[str, float | int]],
    total_real_power_watts: float,
    sequence_contiguous: bool,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = [
        {
            "name": "sequence-contiguous",
            "pass": sequence_contiguous,
            "actual": sequence_contiguous,
            "expected": True,
        }
    ]
    expect = config["expect"]
    for key in (
        "sampling-frequency-hz",
        "voltage-channels",
        "current-channels",
        "total-channels",
    ):
        if key in expect:
            _check_exact(checks, key, metadata[key], expect[key])
    if "voltage-rms" in expect:
        if "Va" not in channel_stats:
            raise ValueError("voltage-rms expectation requires a Va channel")
        _check_close(
            checks,
            "voltage-rms",
            float(channel_stats["Va"]["rms"]),
            float(expect["voltage-rms"]),
            float(expect.get("voltage-rms-tolerance", 1.0)),
        )
    if "total-real-power-watts" in expect:
        _check_close(
            checks,
            "total-real-power-watts",
            total_real_power_watts,
            float(expect["total-real-power-watts"]),
            float(expect.get("real-power-tolerance-watts", 25.0)),
        )
    return checks


def get_metadata(timeout_seconds: float) -> Any:
    if aos is None:
        raise RuntimeError("AOS SDK Python bindings are unavailable")
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = aos.RpcResult()
        metadata = aos.getMetadata(result)
        if result:
            return metadata
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"waveform metadata remained unavailable for {timeout_seconds:g} seconds"
            )
        time.sleep(0.25)


def open_data_socket() -> socket.socket:
    if aos is None:
        raise RuntimeError("AOS SDK Python bindings are unavailable")
    fd = aos.open_socket(0)
    if fd < 0:
        raise RuntimeError("aos.open_socket failed")
    return socket.socket(fileno=fd)


def capture(config: dict[str, Any]) -> dict[str, Any]:
    metadata = metadata_dict(get_metadata(config["metadata-timeout-seconds"]))
    names = channel_names(
        metadata["voltage-channels"],
        metadata["current-channels"],
        metadata["total-channels"],
    )
    missing = sorted(set(config["channels"]) - set(names))
    if missing:
        raise ValueError(
            f"requested channel(s) not present: {', '.join(missing)}; available: {', '.join(names)}"
        )
    selected_indices = [names.index(name) for name in config["channels"]]
    stats = {name: Statistics() for name in names}
    output_csv = Path(config["output-csv"])
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    sampling_hz = metadata["sampling-frequency-hz"]
    header_size = int(aos.geisa_waveform_frame_size())
    previous_sequence: int | None = None
    sequence_contiguous = True
    power_sum = 0.0
    sample_count = 0

    with output_csv.open("w", encoding="utf-8", newline="") as csv, open_data_socket() as stream:
        csv.write("timestamp_ms,sequence,sample_index," + ",".join(config["channels"]) + "\n")
        for frame_index in range(config["frames"]):
            packet = stream.recv(MAX_FRAME_BYTES)
            if not packet:
                raise RuntimeError(f"waveform stream closed after {frame_index} frames")
            decoded = decode_frame(packet, metadata, header_size)
            if previous_sequence is not None:
                expected_sequence = (previous_sequence + 1) & 0xFFFFFFFF
                sequence_contiguous &= decoded.sequence == expected_sequence
            previous_sequence = decoded.sequence
            for row_index, row in enumerate(decoded.rows):
                for name, value in zip(names, row):
                    stats[name].add(value)
                voltage_channels = metadata["voltage-channels"]
                current_channels = metadata["current-channels"]
                if voltage_channels == 1:
                    instantaneous_power = row[0] * sum(
                        row[voltage_channels : voltage_channels + current_channels]
                    )
                else:
                    instantaneous_power = sum(
                        row[index] * row[voltage_channels + index]
                        for index in range(min(voltage_channels, current_channels))
                    )
                power_sum += instantaneous_power
                sample_count += 1
                timestamp = decoded.timestamp_ms + row_index * 1000.0 / sampling_hz
                selected = ",".join(f"{row[index]:.6f}" for index in selected_indices)
                csv.write(
                    f"{timestamp:.3f},{decoded.sequence},{row_index},{selected}\n"
                )

    channel_results = {name: value.result() for name, value in stats.items()}
    multiplier = float(config["expect"].get("power-voltage-multiplier", 1.0))
    total_real_power_watts = power_sum / sample_count * multiplier
    checks = evaluate(
        config,
        metadata,
        channel_results,
        total_real_power_watts,
        sequence_contiguous,
    )
    passed = all(check["pass"] for check in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "frames": config["frames"],
        "samples-per-channel": sample_count,
        "metadata": metadata,
        "channels": channel_results,
        "total-real-power-watts": total_real_power_watts,
        "checks": checks,
        "csv": str(output_csv),
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _request_stop(_signum: int, _frame: Any) -> None:
    STOP_EVENT.set()


def hold_until_deactivated() -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    log_info("capture complete; waiting for aosapp deactivate")
    while not STOP_EVENT.wait(1.0):
        pass


def run(config_path: Path, *, hold: bool = True) -> int:
    config = load_config(config_path)
    result_path = Path(config["output-result"])
    try:
        if aos is None:
            raise RuntimeError("AOS SDK Python bindings are unavailable")
        _app = aos.AppMain()
        result = capture(config)
    except Exception as error:  # noqa: BLE001 - top-level verifier records all failures.
        result = {"status": "FAIL", "error": f"{type(error).__name__}: {error}"}
        write_json_atomic(result_path, result)
        log_error(f"waveform dump FAIL: {result['error']}")
        if hold:
            hold_until_deactivated()
        return 1
    write_json_atomic(result_path, result)
    summary = (
        f"waveform dump {result['status']}: frames={result['frames']} "
        f"samples={result['samples-per-channel']} "
        f"Vrms={result['channels'].get('Va', {}).get('rms', float('nan')):.3f} "
        f"watts={result['total-real-power-watts']:.3f}"
    )
    (log_info if result["status"] == "PASS" else log_error)(summary)
    if hold:
        hold_until_deactivated()
    return 0 if result["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and verify AOS waveform data")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--show-result", action="store_true")
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.check_config:
        print(f"configuration valid: {args.config}")
        return 0
    if args.show_result:
        result_path = Path(config["output-result"])
        deadline = time.monotonic() + config["metadata-timeout-seconds"] + 15.0
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.25)
        with result_path.open(encoding="utf-8") as stream:
            print(stream.read(), end="")
        return 0
    return run(args.config, hold=not args.run_once)


if __name__ == "__main__":
    raise SystemExit(main())
