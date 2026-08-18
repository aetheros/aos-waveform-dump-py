# AOS Waveform Dump Consumer

`aos_waveform_dump` is a small SDK consumer app for capturing and verifying AOS
waveform data. It demonstrates the same public interface used by a normal
consumer app:

- initialize the SDK with `aos.AppMain()`
- request metadata with `aos.getMetadata()`
- connect to the waveform stream with `aos.open_socket()`
- decode and scale GEISA waveform frames

The app is intended both as a test tool and as a straightforward example for
building, deploying, and running another waveform consumer.

## Default capture

The default configuration matches the preinstalled waveform simulator:

- waits up to 30 seconds for waveform metadata
- captures 16 frames, or 4,096 samples per channel at the simulator defaults
- captures `Va`, `Ia`, and `Ic`
- verifies 4,000 Hz, one voltage channel, and two current channels
- verifies approximately 60 Vrms and 300 W
- verifies that frame sequence numbers are contiguous

The app writes two files in its app sandbox:

| File | Contents |
| --- | --- |
| `waveform_dump_result.json` | Overall `PASS` or `FAIL`, metadata, checks, RMS values, and real power |
| `waveform_dump.csv` | Scaled sample data for the configured channels |

After completing one capture, the app remains idle so the files can be viewed.
Deactivate and reactivate it to run another capture.

## Project files

The development app consists of three files:

| File | Purpose |
| --- | --- |
| `aos_waveform_dump.py` | SDK consumer and capture logic |
| `waveform_dump_config.json` | Capture settings and expected values |
| `manifest.json` | AOS app identity, executable, included files, and permissions |

`manifest.json` lists the Python executable and configuration file and grants the
`waveform_provider` group needed to connect to the waveform socket.

## Deploy and run

These examples assume the waveform simulator is already installed on the card
and the SDK tools are on the development host's PATH. Replace `<device-id>` with
the card's SDK device ID.

Start the waveform simulator first:

```text
aosapp -s <device-id> activate aos_waveform_sim_provider
```

From the `aos-waveform-dump-py` source directory, validate the dump
configuration:

```text
python3 aos_waveform_dump.py --check-config
```

Deploy the source directly as a development app:

```text
aosapp -s <device-id> install
```

This command builds the development package from `manifest.json` and its listed
files, transfers it to the card, and installs it.

Start the capture:

```text
aosapp -s <device-id> activate aos_waveform_dump
```

## View the result and captured samples

Open the app through the SDK:

```text
aosapp -s <device-id> shell aos_waveform_dump
```

Inside the app sandbox, display the JSON result:

```text
python3 aos_waveform_dump.py --show-result
```

A successful default capture contains `"status": "PASS"`. The `checks` array
shows the actual and expected value for each verification.

To preview the captured samples:

```text
head -n 10 waveform_dump.csv
```

Exit the app sandbox when finished:

```text
exit
```

## Modify and redeploy the app

Keep the development-host files as the source of truth. After changing the
Python app, manifest, or configuration, validate and redeploy it with this
sequence:

```text
python3 aos_waveform_dump.py --check-config
aosapp -s <device-id> deactivate aos_waveform_dump
aosapp -s <device-id> uninstall aos_waveform_dump
aosapp -s <device-id> install
aosapp -s <device-id> activate aos_waveform_dump
```

Run these commands from the `aos-waveform-dump-py` source directory. Uninstalling
before reinstalling ensures the card receives the changed source files.

## Configure a capture

Edit `waveform_dump_config.json` on the development host before deployment.

The common settings are:

| Setting | Meaning |
| --- | --- |
| `frames` | Number of waveform frames to capture |
| `metadata-timeout-seconds` | How long to wait for the provider to become available |
| `channels` | Scaled channels to include in the CSV |
| `output-csv` | CSV path inside the app sandbox |
| `output-result` | JSON result path inside the app sandbox |
| `expect` | Optional metadata and signal checks |

The default R620-like channel names are `Va`, `Ia`, and `Ic`. A two-voltage,
two-current stream uses `Va`, `Vc`, `Ia`, and `Ic`. The app rejects a requested
channel that is not advertised by the provider.

### Capture without signal expectations

Use an empty `expect` object when the goal is to record an event without checking
for one steady-state voltage or power value:

```json
{
  "frames": 400,
  "metadata-timeout-seconds": 30,
  "channels": ["Va", "Ia", "Ic"],
  "output-csv": "/home/apps/waveform_dump.csv",
  "output-result": "/home/apps/waveform_dump_result.json",
  "expect": {}
}
```

At 4,000 Hz and 256 samples per frame, each frame spans 64 ms. A 400-frame
capture therefore covers 25.6 seconds.

### Verify a different steady load

If the simulator's first step is changed to 900 W, update the expected power:

```json
"expect": {
  "sampling-frequency-hz": 4000,
  "voltage-channels": 1,
  "current-channels": 2,
  "total-channels": 3,
  "voltage-rms": 60.0,
  "voltage-rms-tolerance": 1.0,
  "total-real-power-watts": 900.0,
  "real-power-tolerance-watts": 25.0,
  "power-voltage-multiplier": 2.0
}
```

The default multiplier is 2 because the R620-like simulator advertises 60 Vrms
but uses 120 V when converting total watts to current.

Unknown configuration keys, malformed frames, unsupported sample types, missing
channels, sequence gaps, and failed expectations produce a `FAIL` result.

## Use this as a consumer-app example

The essential SDK flow in `aos_waveform_dump.py` is:

```python
from aossdk import aos
import socket

app = aos.AppMain()

result = aos.RpcResult()
metadata = aos.getMetadata(result)
if not result:
    raise RuntimeError("waveform metadata is unavailable")

fd = aos.open_socket(0)
if fd < 0:
    raise RuntimeError("aos.open_socket failed")

with socket.socket(fileno=fd) as stream:
    frame = stream.recv(65536)
    payload = frame[aos.geisa_waveform_frame_size():]
```

Payload samples are interleaved by channel for each point in time. Integer
voltage samples are multiplied by `metadata.voltage_scale`; integer current
samples are multiplied by `metadata.current_scale`.

To turn this example into another development consumer:

1. Copy or modify the Python app.
2. Give the app unique `name` and `m2m` identifiers in `manifest.json`.
3. List every runtime file in the manifest and retain the `waveform_provider`
   permission group.
4. Deploy from that app's source directory with `aosapp -s <device-id> install`.
5. Start it with `aosapp -s <device-id> activate <app-name>`.

## Stop or remove the dump app

Stop the app while keeping it installed:

```text
aosapp -s <device-id> deactivate aos_waveform_dump
```

Remove the development app from the card:

```text
aosapp -s <device-id> deactivate aos_waveform_dump
aosapp -s <device-id> uninstall aos_waveform_dump
```

The preinstalled waveform simulator may remain installed and can be stopped
separately when it is no longer needed.
