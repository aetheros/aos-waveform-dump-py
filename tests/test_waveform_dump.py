from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aos_waveform_dump as dump


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self) -> None:
        config = dump.load_config(ROOT / "waveform_dump_config.json")
        self.assertEqual(config["frames"], 16)
        self.assertEqual(config["channels"], ["Va", "Ia", "Ic"])

    def test_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"framez": 2}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown config"):
                dump.load_config(path)

    def test_duplicate_channel_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"channels": ["Va", "Va"]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicates"):
                dump.load_config(path)


class FrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = {
            "total-channels": 3,
            "voltage-channels": 1,
            "current-channels": 2,
            "voltage-scale": 0.5,
            "current-scale": 0.25,
            "sampling-frequency-hz": 4000,
        }

    def test_decode_i16_frame_scales_each_channel(self) -> None:
        frame = dump.FRAME_HEADER.pack(1234, 7, 0) + struct.pack(
            "<hhhhhh", 100, 20, -20, -100, -40, 40
        )
        decoded = dump.decode_frame(frame, self.metadata, dump.FRAME_HEADER.size)
        self.assertEqual(decoded.timestamp_ms, 1234)
        self.assertEqual(decoded.sequence, 7)
        self.assertEqual(decoded.rows, [(50.0, 5.0, -5.0), (-50.0, -10.0, 10.0)])

    def test_partial_row_is_rejected(self) -> None:
        frame = dump.FRAME_HEADER.pack(0, 0, 0) + struct.pack("<hh", 1, 2)
        with self.assertRaisesRegex(ValueError, "multiple"):
            dump.decode_frame(frame, self.metadata, dump.FRAME_HEADER.size)

    def test_metadata_rejects_non_i16(self) -> None:
        metadata = SimpleNamespace(
            data_type=1,
            voltage_channels=1,
            current_channels=1,
            total_channels=2,
            sampling_frequency_hz=4000,
            voltage_scale=0.1,
            current_scale=0.1,
            frame_period_ms=64,
            cycle_aligned=False,
        )
        with self.assertRaisesRegex(ValueError, "expects i16"):
            dump.metadata_dict(metadata)


class ExpectationTests(unittest.TestCase):
    def test_expected_values_pass(self) -> None:
        config = {
            "expect": {
                "sampling-frequency-hz": 4000,
                "voltage-rms": 60.0,
                "voltage-rms-tolerance": 1.0,
                "total-real-power-watts": 300.0,
                "real-power-tolerance-watts": 25.0,
            }
        }
        checks = dump.evaluate(
            config,
            {"sampling-frequency-hz": 4000},
            {"Va": {"rms": 60.2}},
            297.0,
            True,
        )
        self.assertTrue(all(check["pass"] for check in checks))

    def test_sequence_gap_fails(self) -> None:
        checks = dump.evaluate({"expect": {}}, {}, {}, 0.0, False)
        self.assertFalse(checks[0]["pass"])


if __name__ == "__main__":
    unittest.main()
