#!/usr/bin/env python3
"""
MCP Server for controlling Robo Defensor via BLE Advertising.

The robot uses QUNYU BLE protocol where the controller (phone/Mac) broadcasts
commands as BLE advertising packets with manufacturer-specific data.
The robot passively scans for these packets.

Protocol reverse-engineered from com.qunyu.robolab APK.
"""

import asyncio
import random
import struct
import sys
import time
import logging
import threading
from dataclasses import dataclass
from typing import Optional

import objc
from Foundation import NSData, NSRunLoop, NSDate, NSObject
from CoreBluetooth import CBPeripheralManager

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("robo-defensor")

# --- BLE Advertising ---

kCBAdvDataManufacturerData = "kCBAdvDataManufacturerData"
MANUFACTURER_ID = bytes([0x00, 0xC2])  # 0xC200 little-endian


class AdvDelegate(NSObject):
    def init(self):
        self = objc.super(AdvDelegate, self).init()
        self.state = 0
        self.ready = False
        self.adv_ok = False
        return self

    def peripheralManagerDidUpdateState_(self, peripheral):
        self.state = peripheral.state()
        if self.state == 5:
            self.ready = True

    def peripheralManagerDidStartAdvertising_error_(self, peripheral, error):
        self.adv_ok = error is None


class BLEAdvertiser:
    """Manages BLE advertising from macOS using CoreBluetooth."""

    def __init__(self):
        self._delegate = None
        self._manager = None
        self._initialized = False
        self._lock = threading.Lock()

    def _ensure_init(self):
        if self._initialized:
            return True
        with self._lock:
            if self._initialized:
                return True
            self._delegate = AdvDelegate.alloc().init()
            self._manager = CBPeripheralManager.alloc().initWithDelegate_queue_(
                self._delegate, None
            )
            # Pump run loop until BT ready
            deadline = time.time() + 10
            while not self._delegate.ready and time.time() < deadline:
                NSRunLoop.currentRunLoop().runUntilDate_(
                    NSDate.dateWithTimeIntervalSinceNow_(0.1)
                )
            if self._delegate.ready:
                self._initialized = True
                logger.info("BLE Advertiser initialized")
            else:
                logger.error(f"BLE init failed, state={self._delegate.state}")
            return self._initialized

    def advertise(self, payload: bytes):
        """Start advertising with manufacturer-specific data."""
        if not self._ensure_init():
            return False
        self._manager.stopAdvertising()
        mfr_data = MANUFACTURER_ID + payload
        ns_data = NSData.dataWithBytes_length_(mfr_data, len(mfr_data))
        self._manager.startAdvertising_({kCBAdvDataManufacturerData: ns_data})
        # Pump run loop briefly for callback
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.05)
        )
        return True

    def stop(self):
        """Stop advertising."""
        if self._manager:
            self._manager.stopAdvertising()
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.02)
            )


# --- QY_rf_payload encryption (reverse-engineered from libqylib.so) ---

def _invert_8(b: int) -> int:
    """Reverse bits of a byte."""
    b = b & 0xFF
    result = 0
    for i in range(8):
        result = (result << 1) | (b & 1)
        b >>= 1
    return result


def _crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16 CCITT (polynomial 0x1021)."""
    crc = init
    for byte in data:
        crc ^= (byte << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _crc16_with_bitrev(addr: bytes, data: bytes) -> int:
    """CRC-16 as implemented in the native library.

    First part: CRC over address bytes (read backwards, no bit reversal)
    Second part: CRC over data bytes (with bit reversal on each byte)
    Final: invert and bit-reverse the 16-bit result.
    """
    crc = 0xFFFF

    # Process address bytes backwards
    for i in range(len(addr) - 1, -1, -1):
        crc ^= (addr[i] << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    # Process data bytes with bit reversal
    for byte in data:
        rev = _invert_8(byte)
        crc ^= (rev << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    # Invert and bit-reverse 16 bits
    crc = (~crc) & 0xFFFF
    # Reverse 16 bits
    result = 0
    for i in range(16):
        result = (result << 1) | (crc & 1)
        crc >>= 1
    return result


def _whitening_init(channel: int) -> list:
    """Initialize BLE whitening LFSR state from channel number."""
    state = [0] * 7
    for i in range(7):
        state[i] = (channel >> i) & 1
    return state


def _whitening_output(state: list) -> int:
    """Get one whitening bit and advance LFSR."""
    out = state[0]
    feedback = state[0] ^ state[4]
    state[0:6] = state[1:7]
    state[6] = feedback
    return out


def _whitening_encode(data: bytearray, state: list) -> bytearray:
    """Apply BLE whitening to data bytes."""
    result = bytearray(len(data))
    for i in range(len(data)):
        byte_val = 0
        for bit in range(8):
            wb = _whitening_output(state)
            byte_val |= (((data[i] >> bit) & 1) ^ wb) << bit
        result[i] = byte_val
    return result


def qy_rf_payload(address: bytes, input_data: bytes) -> bytes:
    """
    Replicate the native QY_rf_payload function.

    Builds a BLE-like advertising packet:
    1. Preamble/sync bytes
    2. Address (reversed + bit-reversed)
    3. Input data
    4. CRC-16
    5. Apply BLE whitening (channel 63 on data portion, channel 37 on full packet)

    Returns the encrypted payload ready for BLE advertising.
    """
    addr_len = len(address)
    data_len = len(input_data)

    # Allocate buffer
    total_size = addr_len + data_len + 20
    buf = bytearray(total_size)

    # Set preamble bytes at offset 15
    buf[15] = 0x71
    buf[16] = 0x0F
    buf[17] = 0x55

    # Copy address in reverse order starting at offset 18
    for i in range(addr_len):
        buf[18 + i] = address[addr_len - 1 - i]

    # Copy data after address
    for i in range(data_len):
        buf[18 + addr_len + i] = input_data[i]

    # Bit-reverse bytes from offset 15 for (addr_len + 3) bytes
    for i in range(addr_len + 3):
        buf[15 + i] = _invert_8(buf[15 + i])

    # Calculate CRC-16 over original address and data
    crc = _crc16_with_bitrev(address, input_data)

    # Store CRC at offset 18 + addr_len + data_len
    crc_offset = 18 + addr_len + data_len
    buf[crc_offset] = crc & 0xFF
    buf[crc_offset + 1] = (crc >> 8) & 0xFF

    # Whitening pass 1: channel 63 on data portion (from offset 18)
    whiten_len1 = addr_len + data_len + 2
    state63 = _whitening_init(63)
    whitened1 = _whitening_encode(buf[18:18 + whiten_len1], state63)
    buf[18:18 + whiten_len1] = whitened1

    # Whitening pass 2: channel 37 on full packet
    state37 = _whitening_init(37)
    whitened2 = _whitening_encode(buf[0:total_size], state37)
    buf[0:total_size] = whitened2

    # Output: bytes from offset 15, length = addr_len + data_len + 5
    output_len = addr_len + data_len + 5
    return bytes(buf[15:15 + output_len])


# --- QUNYU Protocol Constants ---

ADDRESS = bytes([0xC1, 0xC2, 0xC3, 0xC4, 0xC5])  # 5-byte BLE address
CHECK_KEY = 66  # 0x42

# Obfuscation tables
CH37 = [141, 210, 87, 161, 61, 167, 102, 176, 117, 49, 17, 72, 150, 119, 248,
        227, 70, 233, 171, 208, 158, 83, 51, 216, 186, 152, 8, 36, 203, 59,
        252, 113, 163, 244, 85, 104, 207, 169, 25, 108, 93, 76]
CH38 = [214, 197, 68, 32, 89, 222, 225, 143, 27, 165, 175, 66, 123, 78, 205,
        96, 235, 98, 34, 144, 44, 239, 239, 199, 141, 210, 87, 161, 61, 167,
        102, 176, 117, 49, 17, 72, 150, 119, 248, 227, 70, 233]
CH39 = [31, 55, 74, 95, 133, 246, 156, 154, 193, 214, 197, 68, 32, 89, 222,
        225, 143, 27, 165, 175, 66, 123, 78, 205, 96, 235, 98, 34, 144, 44,
        239, 239, 199, 141, 210, 87, 161, 61, 167, 102, 176, 117]

# Speed lookup tables for Robo Defensor (ControlUtil0)
DATA_255 = [186, 221, 255]  # gear 0,1,2 → forward speed
DATA_0 = [58, 23, 0]        # gear 0,1,2 → backward speed
NEUTRAL = 128

GROUP_IDS = [-128, -127, -126, -125, -124, -123, -122, -121, -120, -119]


@dataclass
class RobotState:
    connected: bool = False
    gear: int = 2          # 0-2 for Robo Defensor
    motor_a: int = 128     # neutral
    motor_b: int = 128     # neutral
    motor_c: int = 128     # data[2]
    motor_d: int = 128     # data[3]
    app_id1: int = 17      # default
    app_id2: int = 34      # default
    bid1: int = 0
    bid2: int = 0
    box_type: int = 255
    send_task: Optional[asyncio.Task] = None


robot = RobotState()
advertiser = BLEAdvertiser()


# --- Packet building ---

def build_verify_code() -> bytes:
    """Build the initial verification/pairing packet (10 bytes)."""
    data = bytearray([0, 0, robot.app_id1, robot.app_id2, 0xFE, 0xFF, 0, 0, 0, 0])
    data[9] = (sum(data[0:9]) + CHECK_KEY) & 0xFF
    return bytes(data)


def build_command_10(c: int, d: int) -> bytes:
    """Build 10-byte control packet (boxType 0 / broadcast)."""
    data = bytearray([
        0, 0,  # bid1, bid2 (broadcast)
        robot.app_id1, robot.app_id2,
        c & 0xFF, d & 0xFF,
        0xFF,   # mode byte (0xFF = broadcast)
        robot.motor_a & 0xFF, robot.motor_b & 0xFF,
        0       # checksum
    ])
    data[9] = (sum(data[0:9]) + CHECK_KEY) & 0xFF
    return bytes(data)


def build_command_14(c: int, d: int) -> bytes:
    """Build 14-byte control packet with obfuscation (boxType 2/255)."""
    rand_byte = random.randint(1, 255)
    data = bytearray(14)
    data[0] = 0  # broadcast
    data[1] = 0
    data[2] = robot.app_id1
    data[3] = robot.app_id2
    data[4] = c & 0xFF
    data[5] = d & 0xFF
    data[6] = 0xFF  # broadcast mode
    data[7] = robot.motor_a & 0xFF
    data[8] = robot.motor_b & 0xFF
    data[10] = rand_byte
    data[11] = 0
    data[12] = NEUTRAL & 0xFF
    data[13] = 0

    # Apply obfuscation using channel tables
    idx1 = rand_byte & 0x1F
    data[4] = (data[4] + CH37[idx1]) & 0xFF
    data[5] = (data[5] + CH37[idx1 + 1]) & 0xFF

    idx2 = (rand_byte & 0xF8) // 8
    data[6] = (data[6] + CH38[idx2]) & 0xFF
    data[7] = (data[7] + CH39[idx2]) & 0xFF
    data[8] = (data[8] + CH39[idx2 + 1]) & 0xFF

    idx3 = (rand_byte & 0x3E) // 2
    data[11] = (data[11] + CH38[idx3]) & 0xFF
    data[12] = (data[12] + CH38[idx3 + 1]) & 0xFF
    data[13] = (data[13] + CH38[idx3 + 3]) & 0xFF

    # Checksum
    data[9] = (sum(data[0:9]) + sum(data[10:14]) + CHECK_KEY) & 0xFF
    return bytes(data)


def build_current_command() -> bytes:
    """Build command packet for current motor state."""
    return build_command_14(robot.motor_c, robot.motor_d)


def encrypt_and_send(raw_data: bytes) -> bool:
    """Encrypt data with QY_rf_payload and send via BLE advertising."""
    encrypted = qy_rf_payload(ADDRESS, raw_data)
    return advertiser.advertise(encrypted)


# --- Continuous send loop ---

async def _send_loop():
    """Continuously send motor commands at ~100ms intervals."""
    while robot.connected:
        try:
            cmd = build_current_command()
            encrypt_and_send(cmd)
            await asyncio.sleep(0.15)
        except Exception as e:
            logger.error(f"Send error: {e}")
            await asyncio.sleep(0.5)


def _start_send_loop():
    if robot.send_task is None or robot.send_task.done():
        robot.send_task = asyncio.create_task(_send_loop())


def _stop_send_loop():
    if robot.send_task and not robot.send_task.done():
        robot.send_task.cancel()
        robot.send_task = None


# --- MCP Server ---

mcp = FastMCP(
    "Robo Defensor Controller",
    instructions="""Control the Robo Defensor robot via Bluetooth Low Energy advertising.

Use pair() first to establish connection with the robot. The robot must have Bluetooth on.
Once paired, use movement commands (forward, backward, left, right, stop).

The robot has differential steering with 3 gear levels (0=slow, 1=medium, 2=fast).
""",
)


@mcp.tool()
async def pair() -> str:
    """Pair with the Robo Defensor robot.

    Broadcasts the verification code via BLE advertising.
    The robot will respond by advertising its device ID.
    Make sure the robot is powered on with Bluetooth enabled.
    """
    if robot.connected:
        return "Already connected. Use disconnect() first."

    # Initialize BLE
    if not advertiser._ensure_init():
        return "ERROR: Bluetooth not available on this Mac."

    # Send verification code
    verify = build_verify_code()
    logger.info(f"Sending verify code: {verify.hex()}")
    encrypt_and_send(verify)

    # Keep advertising verification for a few seconds
    for _ in range(30):  # ~4.5 seconds
        encrypt_and_send(verify)
        await asyncio.sleep(0.15)

    # Assume connected (robot doesn't send ACK via this path easily)
    robot.connected = True
    robot.box_type = 255  # default/broadcast mode

    # Start continuous command loop
    _start_send_loop()

    return (
        "Pairing broadcast sent! Robot should now be responding.\n"
        "If the robot's LED blinks or it makes a sound, pairing was successful.\n"
        "Use forward(), backward(), left(), right(), stop() to control it.\n"
        f"Gear: {robot.gear} (0=slow, 1=medium, 2=fast)"
    )


@mcp.tool()
async def disconnect() -> str:
    """Disconnect from the robot."""
    _stop_send_loop()
    advertiser.stop()
    robot.connected = False
    robot.motor_a = NEUTRAL
    robot.motor_b = NEUTRAL
    robot.motor_c = NEUTRAL
    robot.motor_d = NEUTRAL
    return "Disconnected."


@mcp.tool()
async def forward(duration: float = 0) -> str:
    """Move forward. Duration in seconds (0 = continuous until stop)."""
    if not robot.connected:
        return "Not paired. Use pair() first."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_0[g]
    robot.motor_d = DATA_255[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = NEUTRAL
        robot.motor_d = NEUTRAL
        return f"Moved forward {duration}s, stopped."
    return "Moving forward"


@mcp.tool()
async def backward(duration: float = 0) -> str:
    """Move backward. Duration in seconds (0 = continuous until stop)."""
    if not robot.connected:
        return "Not paired. Use pair() first."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_255[g]
    robot.motor_d = DATA_0[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = NEUTRAL
        robot.motor_d = NEUTRAL
        return f"Moved backward {duration}s, stopped."
    return "Moving backward"


@mcp.tool()
async def left(duration: float = 0) -> str:
    """Turn left (spin). Duration in seconds (0 = continuous)."""
    if not robot.connected:
        return "Not paired. Use pair() first."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_255[g]
    robot.motor_d = DATA_255[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = NEUTRAL
        robot.motor_d = NEUTRAL
        return f"Turned left {duration}s, stopped."
    return "Turning left"


@mcp.tool()
async def right(duration: float = 0) -> str:
    """Turn right (spin). Duration in seconds (0 = continuous)."""
    if not robot.connected:
        return "Not paired. Use pair() first."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_0[g]
    robot.motor_d = DATA_0[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = NEUTRAL
        robot.motor_d = NEUTRAL
        return f"Turned right {duration}s, stopped."
    return "Turning right"


@mcp.tool()
async def forward_left(duration: float = 0) -> str:
    """Arc turn forward-left."""
    if not robot.connected:
        return "Not paired."
    g = min(robot.gear, 2)
    robot.motor_c = NEUTRAL
    robot.motor_d = DATA_255[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = NEUTRAL
        robot.motor_d = NEUTRAL
        return f"Forward-left {duration}s, stopped."
    return "Moving forward-left"


@mcp.tool()
async def forward_right(duration: float = 0) -> str:
    """Arc turn forward-right."""
    if not robot.connected:
        return "Not paired."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_0[g]
    robot.motor_d = NEUTRAL
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = NEUTRAL
        robot.motor_d = NEUTRAL
        return f"Forward-right {duration}s, stopped."
    return "Moving forward-right"


@mcp.tool()
async def stop() -> str:
    """Stop all motors."""
    if not robot.connected:
        return "Not paired."
    robot.motor_a = NEUTRAL
    robot.motor_b = NEUTRAL
    robot.motor_c = NEUTRAL
    robot.motor_d = NEUTRAL
    return "Stopped."


@mcp.tool()
async def set_gear(gear: int) -> str:
    """Set speed gear: 0=slow, 1=medium, 2=fast."""
    if not robot.connected:
        return "Not paired."
    robot.gear = max(0, min(2, gear))
    return f"Gear set to {robot.gear}"


@mcp.tool()
async def shoot_on() -> str:
    """Activate shooting mechanism (if equipped)."""
    if not robot.connected:
        return "Not paired."
    # Shooting uses motor_a channel
    robot.motor_a = DATA_255[robot.gear]
    return "Shooting ON"


@mcp.tool()
async def shoot_off() -> str:
    """Deactivate shooting mechanism."""
    if not robot.connected:
        return "Not paired."
    robot.motor_a = NEUTRAL
    return "Shooting OFF"


@mcp.tool()
async def status() -> str:
    """Get current robot state."""
    return (
        f"Connected: {robot.connected}\n"
        f"Gear: {robot.gear} (0=slow, 1=medium, 2=fast)\n"
        f"Motors: A={robot.motor_a}, B={robot.motor_b}, C={robot.motor_c}, D={robot.motor_d}\n"
        f"BoxType: {robot.box_type}\n"
        f"AppID: {robot.app_id1}.{robot.app_id2}\n"
        f"Sending: {robot.send_task is not None and not robot.send_task.done()}"
    )


@mcp.tool()
async def dance(pattern: str = "spin") -> str:
    """Execute a movement pattern: 'spin', 'square', 'zigzag'."""
    if not robot.connected:
        return "Not paired."

    g = min(robot.gear, 2)
    step = 0.6

    if pattern == "spin":
        for _ in range(2):
            robot.motor_c = DATA_0[g]
            robot.motor_d = DATA_0[g]
            await asyncio.sleep(step * 2)
            robot.motor_c = DATA_255[g]
            robot.motor_d = DATA_255[g]
            await asyncio.sleep(step * 2)
    elif pattern == "square":
        for _ in range(4):
            robot.motor_c = DATA_0[g]
            robot.motor_d = DATA_255[g]
            await asyncio.sleep(step)
            robot.motor_c = DATA_0[g]
            robot.motor_d = DATA_0[g]
            await asyncio.sleep(step * 0.5)
    elif pattern == "zigzag":
        for _ in range(4):
            robot.motor_c = DATA_0[g]
            robot.motor_d = NEUTRAL
            await asyncio.sleep(step)
            robot.motor_c = NEUTRAL
            robot.motor_d = DATA_255[g]
            await asyncio.sleep(step)

    robot.motor_c = NEUTRAL
    robot.motor_d = NEUTRAL
    return f"Completed '{pattern}'"


@mcp.tool()
async def send_raw_motors(a: int = 128, b: int = 128, c: int = 128, d: int = 128) -> str:
    """Set raw motor byte values (0-255, 128=neutral).

    Args:
        a: Motor A (0-255)
        b: Motor B (0-255)
        c: Motor C / left-right (0-255)
        d: Motor D / forward-back (0-255)
    """
    if not robot.connected:
        return "Not paired."
    robot.motor_a = a & 0xFF
    robot.motor_b = b & 0xFF
    robot.motor_c = c & 0xFF
    robot.motor_d = d & 0xFF
    return f"Raw motors: A={a}, B={b}, C={c}, D={d}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
