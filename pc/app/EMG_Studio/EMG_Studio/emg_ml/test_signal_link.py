#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_signal_link.py
Hardware Link & Signal Integrity Diagnostic Tool

Features:
1. Auto scans and connects to receiver serial port (ESP32-C3).
2. Verifies VOFA+ JustFloat packets (8-channel float + 4-byte tail).
3. Computes real-time transmission health:
   - Packet receive rate (Hz)
   - Channel raw voltage amplitude (mV)
   - Channel activity / RMS (to verify electrode contact)
   - Packet loss / frame integrity counter
"""

import sys
import time
import struct
import argparse
import numpy as np
import serial
from serial.tools import list_ports

TAIL_BYTES = b'\x00\x00\x80\x7F'
BYTES_PER_SAMPLE = 36  # 8 * 4 + 4

def scan_available_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("[ERROR] No serial ports found! Please check USB cable and driver.")
        return []
    print("\nAvailable Serial Ports:")
    for idx, p in enumerate(ports):
        print(f"  [{idx}] {p.device} ({p.description})")
    return ports

def test_link(port, baudrate=921600, duration=10.0):
    print("=" * 70)
    print(f"Connecting to: {port} at {baudrate} baud...")
    try:
        ser = serial.Serial(port, baudrate, timeout=0.2)
        ser.reset_input_buffer()
    except Exception as e:
        print(f"[ERROR] Failed to open port: {e}")
        return

    print("Connected! Listening for EMG data stream (Press Ctrl+C to stop)...")
    print("Tip: Squeeze your fist or touch electrodes to observe channel values jump!")
    print("=" * 70)

    raw_buf = bytearray()
    valid_samples = 0
    corrupt_frames = 0
    recent_values = np.zeros(8, dtype=np.float32)
    history_window = []  # For RMS calculation (last 100 samples)
    
    t_start = time.time()
    last_stat_time = time.time()
    last_sample_count = 0

    try:
        while True:
            # Check elapsed test duration if set
            elapsed = time.time() - t_start
            if duration > 0 and elapsed >= duration:
                print(f"\n[INFO] Test duration reached ({duration}s).")
                break

            waiting = ser.in_waiting
            if waiting > 0:
                raw_buf.extend(ser.read(waiting))

                while len(raw_buf) >= BYTES_PER_SAMPLE:
                    tail_idx = raw_buf.find(TAIL_BYTES)
                    if tail_idx == -1:
                        if len(raw_buf) > 2048:
                            del raw_buf[:-3]
                            corrupt_frames += 1
                        break

                    if tail_idx >= 32:
                        payload = raw_buf[tail_idx - 32:tail_idx]
                        floats = struct.unpack('<8f', payload)
                        recent_values = np.array(floats, dtype=np.float32)
                        history_window.append(recent_values)
                        if len(history_window) > 100:
                            history_window.pop(0)

                        valid_samples += 1
                        del raw_buf[:tail_idx + 4]
                    else:
                        del raw_buf[:tail_idx + 4]
                        corrupt_frames += 1
            else:
                time.sleep(0.002)

            # Print diagnostic stats every 0.5s
            now = time.time()
            if now - last_stat_time >= 0.5:
                dt = now - last_stat_time
                d_samples = valid_samples - last_sample_count
                inst_rate = d_samples / dt
                last_stat_time = now
                last_sample_count = valid_samples

                # Calculate RMS / peak-to-peak to assess contact
                if history_window:
                    arr = np.array(history_window)
                    ch_ptp = np.ptp(arr, axis=0)  # Peak-to-peak amplitude
                else:
                    ch_ptp = np.zeros(8)

                # Format terminal display
                ch_str = " | ".join([f"CH{i+1}:{recent_values[i]:4.0f}mV (±{ch_ptp[i]:3.0f})" for i in range(8)])
                status_icon = "🟢 OK" if inst_rate > 1500 else ("🟡 SLOW" if inst_rate > 500 else "🔴 NO SIGNAL")

                sys.stdout.write(f"\r[{status_icon}] Rate: {inst_rate:5.0f} Sps | Samples: {valid_samples:6d} | {ch_str}")
                sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n\nTest interrupted by user.")
    finally:
        ser.close()

    # Final summary report
    print("\n" + "=" * 70)
    print("Link Quality Summary:")
    print(f"  - Total Valid Samples Received : {valid_samples}")
    print(f"  - Corrupt / Misaligned Frames  : {corrupt_frames}")
    if valid_samples > 0:
        avg_rate = valid_samples / (time.time() - t_start)
        print(f"  - Average Data Throughput      : {avg_rate:.1f} samples/sec")
        print("\nElectrode Channel Health Check:")
        for ch in range(8):
            val = recent_values[ch]
            status = "NORMAL"
            if val < 50.0:
                status = "LOW / SHORT TO GND?"
            elif val > 3250.0:
                status = "SATURATED / HIGH NOISE?"
            print(f"    CH{ch+1}: {val:6.1f} mV  -->  [{status}]")
    else:
        print("  - [WARNING] No valid packets received! Please check:")
        print("    1. Is the transmitter hand ring powered on?")
        print("    2. Is the receiver ESP-NOW channel matching (channel 1)?")
        print("    3. Is baud rate set to 921600?")
    print("=" * 70)

def main():
    parser = argparse.ArgumentParser(description="EMG 8-Channel Link Integrity Test")
    parser.add_argument("--port", type=str, default=None, help="Serial port (e.g., COM3)")
    parser.add_argument("--baud", type=int, default=921600, help="Baud rate (default: 921600)")
    parser.add_argument("--duration", type=float, default=0, help="Test duration in seconds (0 = infinite)")
    args = parser.parse_args()

    port = args.port
    if port is None:
        ports = scan_available_ports()
        if not ports:
            sys.exit(1)
        try:
            choice = input("\nSelect port index (Press Enter for default [0]): ").strip()
            idx = int(choice) if choice else 0
            port = ports[idx].device
        except Exception:
            port = ports[0].device

    test_link(port, args.baud, args.duration)

if __name__ == "__main__":
    main()
