#!/usr/bin/env python3

from __future__ import annotations

import faulthandler, signal
faulthandler.enable(all_threads=True)

import os, socket, struct, numpy as np

# ---- Aetheros SDK ---------------------------------------------------
import aossdk.aos as aos
from aos import logDebug, logInfo, logWarn, logError


# Open the socket to the waveform provider service via the AOS SDK
def open_data_socket():
    fd = aos.open_socket(0)
    if fd < 0:
        raise RuntimeError("open_socket failed")
    return fd

# Requests the GEISA waveform metadata from the waveform provider via the AOS SDK (delivered via RPC)
def get_metadata():
    logInfo("Requesting GEISA metadata")
    res = aos.RpcResult()
    metadata = aos.getMetadata(res)
    if not res:
        raise RuntimeError("No GEISA metadata yet, or comms failure")
    else:
        logInfo("Received GEISA metadata")
    return metadata

# Dumps 10 frames of a scaled channel to a csv
def dump_one_channel_to_csv(csv_path, which='Va'):

    # Get Metadata
    metadata = get_metadata()

    total_ch = metadata.total_channels      # total channels per sample "time step"
    vch = metadata.voltage_channels         # number of voltage channels
    ich = metadata.current_channels         # number of current channels
    assert total_ch == vch + ich and total_ch > 0

    # The stream is interleaved by channel within each time step:
    # [V1, V2, ..., I1, I2, ...] for sample #0,
    # [V1, V2, ..., I1, I2, ...] for sample #1, etc.
    # We pick the column index of the channel we want to dump.
    chan_map = {'Va':0, 'Vc':(1 if vch>=2 else None),
                'Ia':vch + 0,
                'Ic':(vch + 1 if ich>=2 else None)}
    idx = chan_map.get(which)
    if idx is None:
        raise ValueError(f"Channel {which} not present (vch={vch}, ich={ich})")

    # Open the waveform provider socket to stream raw GEISA data frames
    fd = open_data_socket()
    socketStream = os.fdopen(fd, 'rb', buffering=0)

    with open(csv_path, 'w') as out:
        frames_to_dump = 10
        for frameCnt in range(frames_to_dump):
            # Read from the socket with a large buffer
            frame = socketStream.read(65536)
            if not frame:
                break
            if len(frame) < aos.geisa_waveform_frame_size():
                # Too short to contain header + data; skip
                continue

            payload = frame[aos.geisa_waveform_frame_size():]

            # The payload is a flat byte array of int16 (little-endian) samples,
            # interleaved across 'total_ch' channels for each time step.
            #
            # Example: if total_ch == 3, the layout is:
            #   [ch0_s0, ch1_s0, ch2_s0, ch0_s1, ch1_s1, ch2_s1, ...]
            #
            # We first view the bytes as a 1-D array of int16 ('<i2' = little-endian int16).
            samples = np.frombuffer(payload, dtype='<i2')

            # The total number of *int16 values* must be a multiple of total_ch
            # so we can reshape into rows = "time steps" and cols = "channels".
            if samples.size % total_ch != 0:
                # Corrupted/partial frame; skip
                continue  

            # number of time steps (samples-per-channel) in this frame
            nsamp = samples.size // total_ch

            # Reshape from flat [ch0_s0, ch1_s0, ..., chN_s0, ch0_s1, ...]
            # to a 2-D array of shape (nsamp, total_ch), where:
            #   row i = time step i
            #   column j = channel j at that time step
            samples = samples.reshape(nsamp, total_ch)

            # Select the single channel column we want to dump
            col = samples[:, idx].astype(np.float64)

            # Scale the raw data using metadata scales:
            # voltage channels use voltage_scale; current channels use current_scale
            if idx < vch:   # chosen column is a voltage
                col *= metadata.voltage_scale
            else:           # chosen column is a current
                col *= metadata.current_scale
            
            logInfo(f"Writing frame number {frameCnt} to {csv_path}")

            out.write("\n".join(f"{x:.6f}" for x in col))
            out.write("\n")

    socketStream.close()


def main():
    logInfo("Starting app...")

    app = aos.AppMain()
    dump_one_channel_to_csv("/tmp/va.csv", which="Va")

    logInfo("Exiting App...")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())