#!/usr/bin/env python3
"""
Panda Breath WebSocket diagnostic script.

Connects to the Panda Breath device's raw WebSocket endpoint, dumps every
byte of the handshake response and every frame received for 30 seconds.
Run from any machine on the same network as the device.

Usage:
    python3 diag_panda_ws.py 10.88.88.193
    python3 diag_panda_ws.py 10.88.88.193 --send '{"get_settings":1}'
"""

import asyncio
import base64
import json
import secrets
import sys
import time


HOST = sys.argv[1] if len(sys.argv) > 1 else "10.88.88.193"
SEND_MSG = None
PORT = 80
for i, arg in enumerate(sys.argv):
    if arg == "--send" and i + 1 < len(sys.argv):
        SEND_MSG = sys.argv[i + 1]
    if arg == "--port" and i + 1 < len(sys.argv):
        PORT = int(sys.argv[i + 1])


def hex_dump(data: bytes, label: str = "") -> None:
    if label:
        print(f"\n[{label}]")
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {i:04x}  {hex_part:<47}  {ascii_part}")


async def scan_ports(host):
    """Quick scan to find which ports are open."""
    print(f"Scanning {host} for open ports ...")
    candidates = [80, 443, 8080, 8888, 8899, 9000]
    open_ports = []
    for port in candidates:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
            w.close()
            open_ports.append(port)
            print(f"  port {port}: OPEN")
        except Exception:
            print(f"  port {port}: closed/timeout")
    return open_ports


async def main():
    # Port scan first
    open_ports = await scan_ports(HOST)
    ws_port = PORT
    if PORT not in open_ports and open_ports:
        ws_port = open_ports[0]
        print(f"\nPort {PORT} not reachable, trying {ws_port} instead")
    elif not open_ports:
        print(f"\nNo open ports found on {HOST} — device not reachable from this machine")
        return
    print()

    print(f"Connecting to {HOST}:{ws_port} ...")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(HOST, ws_port), timeout=10.0
        )
    except asyncio.TimeoutError:
        print("TCP connect TIMED OUT — device not reachable or port closed")
        return
    except OSError as e:
        print(f"TCP connect FAILED: {e}")
        return
    print("TCP connected")

    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = (
        f"GET /ws HTTP/1.1\r\n"
        f"Host: {HOST}:{ws_port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()
    print(f"\nSending HTTP Upgrade ({len(request)} bytes):")
    print(request.decode(errors='replace'))

    writer.write(request)
    await writer.drain()

    # Read HTTP response
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            if not chunk:
                print("Server closed connection before sending HTTP response")
                return
            buf += chunk
    except asyncio.TimeoutError:
        print(f"TIMEOUT waiting for HTTP response. Got so far ({len(buf)} bytes):")
        hex_dump(buf, "partial response")
        return

    # Split off any WS frames that arrived with the HTTP response
    header_end = buf.find(b"\r\n\r\n") + 4
    http_response = buf[:header_end]
    leftover = buf[header_end:]

    print(f"\nHTTP response ({len(http_response)} bytes):")
    print(http_response.decode(errors='replace'))

    if b" 101 " not in http_response:
        print("ERROR: Did not get 101 Switching Protocols!")
        hex_dump(http_response, "raw response")
        return

    print("✓ WS upgrade successful\n")

    # Optionally send a message
    if SEND_MSG:
        data = SEND_MSG.encode()
        n = len(data)
        if n < 126:
            frame = bytes([0x81, n]) + data
        else:
            frame = bytes([0x81, 126, n >> 8, n & 0xFF]) + data
        writer.write(frame)
        await writer.drain()
        print(f"Sent: {SEND_MSG}\n")

    # Process any leftover bytes from the HTTP read
    frame_buf = leftover

    print("Receiving frames (30s)...\n")
    deadline = time.time() + 30

    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=min(remaining, 2.0))
            if not chunk:
                print("Connection closed by device")
                break
            frame_buf += chunk
        except asyncio.TimeoutError:
            if SEND_MSG is None:
                # Send get_settings to keep device talking
                frame = bytes([0x81, 15]) + b'{"get_settings":1}'
                writer.write(frame)
                await writer.drain()
            continue

        # Parse frames from buffer
        while len(frame_buf) >= 2:
            b0 = frame_buf[0]
            b1 = frame_buf[1]
            fin = bool(b0 & 0x80)
            rsv = (b0 & 0x70) >> 4
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            n = b1 & 0x7F
            offset = 2

            if n == 126:
                if len(frame_buf) < 4:
                    break
                n = int.from_bytes(frame_buf[2:4], 'big')
                offset = 4
            elif n == 127:
                if len(frame_buf) < 10:
                    break
                n = int.from_bytes(frame_buf[2:10], 'big')
                offset = 10

            if masked:
                offset += 4

            if len(frame_buf) < offset + n:
                break

            raw_frame = frame_buf[:offset + n]
            mask_key = frame_buf[offset-4:offset] if masked else b''
            payload = frame_buf[offset:offset + n]

            if masked and mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            opcode_names = {0: "CONT", 1: "TEXT", 2: "BIN", 8: "CLOSE", 9: "PING", 10: "PONG"}
            opname = opcode_names.get(opcode, f"UNK(0x{opcode:02x})")

            print(f"Frame: opcode={opname} fin={fin} rsv={rsv} masked={masked} len={n}")

            if rsv != 0:
                print(f"  ⚠ RSV bits set: {rsv} (reserved bits should be 0)")
            if not fin and opcode in (8, 9, 10):
                print(f"  ⚠ Fragmented control frame (fin=False on control opcode) — this breaks websockets lib!")

            if opcode == 1:  # text
                try:
                    decoded = payload.decode()
                    parsed = json.loads(decoded)
                    print(f"  JSON: {json.dumps(parsed, indent=2)[:500]}")
                except Exception:
                    print(f"  Raw: {payload[:200]}")
            elif opcode == 8:  # close
                code = int.from_bytes(payload[:2], 'big') if len(payload) >= 2 else 0
                reason = payload[2:].decode(errors='replace') if len(payload) > 2 else ""
                print(f"  Close code={code} reason={reason!r}")
            elif opcode == 9:  # ping
                print(f"  Ping payload: {payload[:20].hex()}")
                # Send pong
                writer.write(bytes([0x8A, 0]))
                await writer.drain()
                print(f"  → Sent pong")
            else:
                if payload:
                    hex_dump(payload[:64], "payload")

            frame_buf = frame_buf[offset + n:]
            print()

    writer.close()
    print("Done.")


asyncio.run(main())
