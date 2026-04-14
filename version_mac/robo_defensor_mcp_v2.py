#!/usr/bin/env python3
"""
MCP Server for controlling Robo Defensor via BLE Advertising on macOS.

Uses raw HCI commands via PyUSB to the ASUS USB-BT500 (RTL8761B) adapter,
bypassing CoreBluetooth limitations (which blocks custom manufacturer data
and doesn't support connectable advertising control).

Fixes vs version_mac v1:
  - Uses PyUSB + raw HCI instead of CoreBluetooth (reliable manufacturer data)
  - ADV_IND (connectable undirected) — robot ignores non-connectable
  - Simultaneous scan during pairing to capture bid1/bid2 from robot response
  - Motor commands use actual robot bid bytes + mode=0 (single device, not broadcast)
  - data[13] = 128 (0x80) matching Android bridge
  - box_type default = 2

Protocol reverse-engineered from com.qunyu.robolab APK.

ASUS USB-BT500 macOS setup (RTL8761B has NO native macOS driver):
  macOS does not ship a Realtek BT driver, so the adapter is not claimed by
  any kernel driver — this lets PyUSB/libusb access it directly, which is
  what we want.

  Firmware is loaded AUTOMATICALLY on startup via bumble-rtk-util.
  The RTL8761B starts in bare ROM mode and needs firmware before HCI works.
  Bumble handles this via USB HCI vendor commands.
  (see github.com/google/bumble/issues/747 — confirmed working on macOS)

  One-time setup:
    pip install bumble pyusb libusb-package "mcp[cli]"
    bumble-rtk-fw-download          # downloads rtl8761b_fw.bin + config

  If macOS tries to switch its BT stack to the external adapter, prevent it:
    sudo nvram bluetoothHostControllerSwitchBehavior="never"

  If claim_interface fails, run with sudo.

  Firmware files:  github.com/amcabezas/bt_rtl8761b-fw
  Bumble RTL docs: google.github.io/bumble/drivers/realtek.html
  Bumble macOS:    google.github.io/bumble/platforms/macos.html
"""

import asyncio
import random
import shutil
import subprocess
import threading
import time
import logging
from dataclasses import dataclass
from typing import Optional

import usb.core
import usb.util

try:
    import libusb_package
    _USB_BACKEND = libusb_package.get_libusb1_backend()
except ImportError:
    _USB_BACKEND = None

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("robo-defensor")

# ---------------------------------------------------------------------------
# USB / HCI constants
# ---------------------------------------------------------------------------

ASUS_VENDOR_ID      = 0x0B05
ASUS_PRODUCT_ID     = 0x1BF6   # RTL8761B on macOS (confirmed)
ASUS_PRODUCT_ID_ALT = 0x190E   # alternate PID

BT_CLASS    = 0xE0
BT_SUBCLASS = 0x01
BT_PROTOCOL = 0x01

HCI_RESET                  = 0x0C03
HCI_LE_SET_ADV_PARAMETERS  = 0x2006
HCI_LE_SET_ADV_DATA        = 0x2008
HCI_LE_SET_ADV_ENABLE      = 0x200A
HCI_LE_SET_SCAN_PARAMETERS = 0x200B
HCI_LE_SET_SCAN_ENABLE     = 0x200C

MFR_ID_CONTROLLER = bytes([0x00, 0xC2])   # 0xC200 — what we advertise
MFR_ID_ROBOT      = 0xFFF0                # 65520 — robot's response manufacturer ID

# ---------------------------------------------------------------------------
# QUNYU Protocol Constants
# ---------------------------------------------------------------------------

ADDRESS   = bytes([0xC1, 0xC2, 0xC3, 0xC4, 0xC5])
CHECK_KEY = 66   # 0x42

CH37 = [141,210,87,161,61,167,102,176,117,49,17,72,150,119,248,
        227,70,233,171,208,158,83,51,216,186,152,8,36,203,59,
        252,113,163,244,85,104,207,169,25,108,93,76]
CH38 = [214,197,68,32,89,222,225,143,27,165,175,66,123,78,205,
        96,235,98,34,144,44,239,239,199,141,210,87,161,61,167,
        102,176,117,49,17,72,150,119,248,227,70,233]
CH39 = [31,55,74,95,133,246,156,154,193,214,197,68,32,89,222,
        225,143,27,165,175,66,123,78,205,96,235,98,34,144,44,
        239,239,199,141,210,87,161,61,167,102,176,117]

DATA_255 = [186, 221, 255]   # gear 0/1/2 → forward speed byte
DATA_0   = [58,  23,  0  ]   # gear 0/1/2 → backward speed byte
NEUTRAL  = 128

# ---------------------------------------------------------------------------
# QY_rf_payload encryption  (reverse-engineered from libqylib.so)
# ---------------------------------------------------------------------------

def _invert_8(b: int) -> int:
    b &= 0xFF
    result = 0
    for _ in range(8):
        result = (result << 1) | (b & 1)
        b >>= 1
    return result


def _crc16_with_bitrev(addr: bytes, data: bytes) -> int:
    crc = 0xFFFF
    for i in range(len(addr) - 1, -1, -1):
        crc ^= (addr[i] << 8) & 0xFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    for byte in data:
        rev = _invert_8(byte)
        crc ^= (rev << 8) & 0xFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    crc = (~crc) & 0xFFFF
    result = 0
    for _ in range(16):
        result = (result << 1) | (crc & 1)
        crc >>= 1
    return result


def _whitening_init(channel: int) -> list:
    return [(channel >> i) & 1 for i in range(7)]


def _whitening_encode(data: bytearray, state: list) -> bytearray:
    result = bytearray(len(data))
    for i in range(len(data)):
        byte_val = 0
        for bit in range(8):
            out = state[0]
            fb = state[0] ^ state[4]
            state[0:6] = state[1:7]
            state[6] = fb
            byte_val |= (((data[i] >> bit) & 1) ^ out) << bit
        result[i] = byte_val
    return result


def qy_rf_payload(address: bytes, input_data: bytes) -> bytes:
    addr_len  = len(address)
    data_len  = len(input_data)
    total     = addr_len + data_len + 20
    buf       = bytearray(total)

    buf[15] = 0x71; buf[16] = 0x0F; buf[17] = 0x55

    for i in range(addr_len):
        buf[18 + i] = address[addr_len - 1 - i]
    for i in range(data_len):
        buf[18 + addr_len + i] = input_data[i]
    for i in range(addr_len + 3):
        buf[15 + i] = _invert_8(buf[15 + i])

    crc = _crc16_with_bitrev(address, input_data)
    off = 18 + addr_len + data_len
    buf[off] = crc & 0xFF;  buf[off + 1] = (crc >> 8) & 0xFF

    wlen = addr_len + data_len + 2
    buf[18:18 + wlen] = _whitening_encode(buf[18:18 + wlen], _whitening_init(63))
    buf[0:total]      = _whitening_encode(buf[0:total],       _whitening_init(37))

    out_len = addr_len + data_len + 5
    return bytes(buf[15:15 + out_len])

# ---------------------------------------------------------------------------
# BLEAdvertiserHCI — raw USB HCI to ASUS USB-BT500
# ---------------------------------------------------------------------------

class BLEAdvertiserHCI:
    """Send raw HCI commands to the ASUS USB-BT500 to advertise BLE packets."""

    def __init__(self):
        self._dev         = None
        self._ep_in       = None
        self._ep_bulk_in  = None
        self._initialized = False
        self._lock        = threading.Lock()
        self._scan_lock   = threading.Lock()

    def _find_device(self):
        kw = {"backend": _USB_BACKEND} if _USB_BACKEND else {}
        for pid in (ASUS_PRODUCT_ID, ASUS_PRODUCT_ID_ALT):
            dev = usb.core.find(idVendor=ASUS_VENDOR_ID, idProduct=pid, **kw)
            if dev is not None:
                logger.info(f"Found ASUS USB-BT500: {ASUS_VENDOR_ID:#06x}:{pid:#06x}")
                return dev
        # class-scan fallback for other BT 5.0 USB adapters
        for dev in usb.core.find(find_all=True, **kw):
            try:
                for cfg in dev:
                    for intf in cfg:
                        if (intf.bInterfaceClass    == BT_CLASS and
                                intf.bInterfaceSubClass == BT_SUBCLASS and
                                intf.bInterfaceProtocol == BT_PROTOCOL):
                            logger.info(f"Found BT adapter: {dev.idVendor:#06x}:{dev.idProduct:#06x}")
                            return dev
            except Exception:
                continue
        return None

    def _ensure_init(self) -> bool:
        if self._initialized:
            return True
        with self._lock:
            if self._initialized:
                return True
            dev = self._find_device()
            if dev is None:
                logger.error("No USB Bluetooth adapter found. Plug in the ASUS USB-BT500.")
                return False

            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except Exception as e:
                logger.debug(f"detach_kernel_driver: {e}")

            try:
                dev.set_configuration()
            except Exception as e:
                logger.debug(f"set_configuration: {e}")

            try:
                usb.util.claim_interface(dev, 0)
            except Exception as e:
                logger.warning(f"claim_interface: {e}")

            cfg  = dev.get_active_configuration()
            intf = cfg[(0, 0)]
            ep_in = ep_bulk_in = None
            for ep in intf:
                direction = usb.util.endpoint_direction(ep.bEndpointAddress)
                ep_type   = usb.util.endpoint_type(ep.bmAttributes)
                if direction == usb.util.ENDPOINT_IN:
                    if ep_type == usb.util.ENDPOINT_TYPE_INTR and ep_in is None:
                        ep_in = ep
                    elif ep_type == usb.util.ENDPOINT_TYPE_BULK and ep_bulk_in is None:
                        ep_bulk_in = ep

            self._dev        = dev
            self._ep_in      = ep_in
            self._ep_bulk_in = ep_bulk_in

            # Load RTL8761B firmware via bumble before any HCI commands.
            # The chip starts in bare ROM mode; without firmware, HCI
            # advertising commands silently fail. HCI_RESET can also
            # revert to ROM mode, so we load firmware first and skip
            # the reset — bumble-rtk-util already resets as part of
            # its loading sequence.
            if not self._load_firmware():
                logger.warning("Firmware load failed — adapter may be in ROM mode. "
                               "Falling back to HCI_RESET.")
                if not self._hci_cmd(HCI_RESET, b"", wait_ms=1500):
                    logger.error("HCI Reset also failed")
                    return False

            self._initialized = True
            logger.info("BLEAdvertiserHCI initialized (ASUS USB-BT500)")
            return True

    def _load_firmware(self) -> bool:
        """Load RTL8761B firmware using bumble-rtk-util.

        Must be called before HCI_RESET, which would revert the chip
        to ROM mode. bumble-rtk-util does its own reset internally.

        Requires: pip install bumble && bumble-rtk-fw-download
        """
        rtk_util = shutil.which("bumble-rtk-util")
        if rtk_util is None:
            logger.warning("bumble-rtk-util not found. "
                           "Install bumble: pip install bumble && bumble-rtk-fw-download")
            return False

        # Release the interface so bumble can claim the device
        try:
            usb.util.release_interface(self._dev, 0)
        except Exception:
            pass

        logger.info("Loading RTL8761B firmware via bumble-rtk-util...")
        try:
            result = subprocess.run(
                [rtk_util, "load"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                logger.info("Firmware loaded successfully")
            else:
                logger.warning(f"bumble-rtk-util exit code {result.returncode}: "
                               f"{result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logger.warning("bumble-rtk-util timed out after 30s")
            return False
        except Exception as e:
            logger.warning(f"bumble-rtk-util failed: {e}")
            return False

        # Re-claim interface after bumble releases it
        try:
            usb.util.claim_interface(self._dev, 0)
        except Exception as e:
            logger.warning(f"Re-claim after firmware load: {e}")

        return result.returncode == 0

    # ------------------------------------------------------------------
    # HCI command / event helpers
    # ------------------------------------------------------------------

    def _hci_cmd(self, opcode: int, params: bytes, wait_ms: int = 1000) -> bool:
        pkt = bytes([opcode & 0xFF, (opcode >> 8) & 0xFF, len(params)]) + params
        try:
            self._dev.ctrl_transfer(
                bmRequestType=0x20,
                bRequest=0x00,
                wValue=0x0000,
                wIndex=0x0000,
                data_or_wLength=pkt,
                timeout=wait_ms,
            )
        except Exception as e:
            logger.error(f"ctrl_transfer opcode={opcode:#06x}: {e}")
            return False
        self._drain_event(timeout=wait_ms)
        return True

    def _drain_event(self, timeout: int = 500):
        if self._ep_in is None:
            return
        try:
            data = bytes(self._ep_in.read(64, timeout=timeout))
            logger.debug(f"HCI event: {data.hex()}")
        except usb.core.USBTimeoutError:
            pass
        except Exception as e:
            logger.debug(f"event read: {e}")

    def _read_raw_event(self, timeout: int = 300) -> bytes | None:
        if self._ep_in is None:
            return None
        try:
            return bytes(self._ep_in.read(256, timeout=timeout))
        except usb.core.USBTimeoutError:
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Advertising
    # ------------------------------------------------------------------

    def _adv_params(self) -> bool:
        """LE_Set_Advertising_Parameters — ADV_IND (connectable undirected), 100 ms."""
        return self._hci_cmd(HCI_LE_SET_ADV_PARAMETERS, bytes([
            0xA0, 0x00,   # interval min (100 ms)
            0xA0, 0x00,   # interval max (100 ms)
            0x00,          # ADV_IND — connectable undirected (critical: robot ignores non-connectable)
            0x00,          # own address: public
            0x00,          # peer address type: public
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   # peer address (ignored)
            0x07,          # all 3 advertising channels
            0x00,          # filter policy: none
        ]))

    def _adv_data(self, payload: bytes) -> bool:
        """LE_Set_Advertising_Data with manufacturer-specific AD."""
        ad_body   = bytes([0xFF]) + MFR_ID_CONTROLLER + payload
        ad_struct = bytes([len(ad_body)]) + ad_body
        if len(ad_struct) > 31:
            ad_struct = ad_struct[:31]
        sig_len  = len(ad_struct)
        hci_data = bytes([sig_len]) + ad_struct + bytes(31 - sig_len)
        return self._hci_cmd(HCI_LE_SET_ADV_DATA, hci_data)

    def _adv_enable(self, on: bool) -> bool:
        return self._hci_cmd(HCI_LE_SET_ADV_ENABLE, bytes([0x01 if on else 0x00]))

    def advertise(self, payload: bytes) -> bool:
        if not self._ensure_init():
            return False
        self._adv_enable(False)
        if not self._adv_params():
            return False
        if not self._adv_data(payload):
            return False
        return self._adv_enable(True)

    def stop(self):
        if self._dev:
            try: self._adv_enable(False)
            except Exception: pass
            try: self._scan_enable(False)
            except Exception: pass
            try: usb.util.release_interface(self._dev, 0)
            except Exception: pass
            try: usb.util.dispose_resources(self._dev)
            except Exception: pass
            self._dev = None
            self._initialized = False

    # ------------------------------------------------------------------
    # Scanning  (capture robot's bid1/bid2 response during pairing)
    # ------------------------------------------------------------------

    def _scan_params(self) -> bool:
        """LE_Set_Scan_Parameters — passive, 10 ms interval/window."""
        return self._hci_cmd(HCI_LE_SET_SCAN_PARAMETERS, bytes([
            0x00,       # passive scan
            0x10, 0x00, # scan interval 10 ms
            0x10, 0x00, # scan window  10 ms
            0x00,       # own address: public
            0x00,       # filter policy: none
        ]))

    def _scan_enable(self, on: bool) -> bool:
        return self._hci_cmd(HCI_LE_SET_SCAN_ENABLE, bytes([0x01 if on else 0x00, 0x00]))

    def scan_for_robot(self, app_id1: int = 17, app_id2: int = 34,
                       duration: float = 5.0) -> tuple | None:
        """
        Start LE scan and look for the robot's advertising response.

        Robot responds with manufacturer ID 0xFFF0, where:
          payload[0] = bid1, payload[1] = bid2,
          payload[2] = appId1, payload[3] = appId2,
          payload[7] = boxType

        Returns (bid1, bid2, box_type) or None if not found.
        """
        if not self._ensure_init():
            return None

        with self._scan_lock:
            logger.info("Starting LE scan for robot response...")
            self._scan_params()
            self._scan_enable(True)

            deadline = time.time() + duration
            result   = None
            while time.time() < deadline:
                raw = self._read_raw_event(timeout=200)
                if raw:
                    found = self._parse_adv_report(raw, app_id1, app_id2)
                    if found:
                        result = found
                        logger.info(f"Robot found: bid1={found[0]} bid2={found[1]} boxType={found[2]}")
                        break

            self._scan_enable(False)
            return result

    @staticmethod
    def _parse_adv_report(raw: bytes, app_id1: int, app_id2: int) -> tuple | None:
        """Parse HCI_LE_Meta_Event / LE_Advertising_Report."""
        if len(raw) < 13:
            return None
        if raw[0] != 0x3E:   # HCI_LE_Meta_Event
            return None
        if raw[2] != 0x02:   # LE_Advertising_Report
            return None

        num_reports = raw[3]
        offset = 4
        for _ in range(num_reports):
            if offset + 9 > len(raw):
                break
            data_len = raw[offset + 8]
            if offset + 9 + data_len > len(raw):
                break
            ad_bytes = raw[offset + 9: offset + 9 + data_len]
            offset  += 9 + data_len + 1   # +1 for RSSI

            found = BLEAdvertiserHCI._parse_ad_mfr(ad_bytes, app_id1, app_id2)
            if found:
                return found
        return None

    @staticmethod
    def _parse_ad_mfr(ad_data: bytes, app_id1: int, app_id2: int) -> tuple | None:
        """Walk AD structures, find manufacturer-specific (0xFF) with robot identity."""
        i = 0
        while i < len(ad_data):
            ad_len = ad_data[i]
            if ad_len == 0:
                break
            if i + ad_len >= len(ad_data):
                break
            ad_type = ad_data[i + 1]
            if ad_type == 0xFF and ad_len >= 10:
                mfr_id  = ad_data[i + 2] | (ad_data[i + 3] << 8)
                payload = ad_data[i + 4: i + 1 + ad_len]
                if mfr_id == MFR_ID_ROBOT and len(payload) >= 8:
                    if payload[2] == app_id1 and payload[3] == app_id2:
                        bid1     = payload[0]
                        bid2     = payload[1]
                        box_type = payload[7]
                        return (bid1, bid2, box_type)
            i += 1 + ad_len
        return None


# ---------------------------------------------------------------------------
# Robot state
# ---------------------------------------------------------------------------

@dataclass
class RobotState:
    connected: bool = False
    gear:      int  = 2       # 0=slow 1=med 2=fast
    motor_a:   int  = NEUTRAL
    motor_b:   int  = NEUTRAL
    motor_c:   int  = NEUTRAL
    motor_d:   int  = NEUTRAL
    app_id1:   int  = 17
    app_id2:   int  = 34
    bid1:      int  = 48      # default from known robot (updated by scan)
    bid2:      int  = 242     # -14 signed = 0xF2 = 242 unsigned
    box_type:  int  = 2       # robot's box type (updated by scan)
    send_task: Optional[asyncio.Task] = None


robot      = RobotState()
advertiser = BLEAdvertiserHCI()

# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def build_verify_code() -> bytes:
    """10-byte pairing/verify packet."""
    d = bytearray([0, 0, robot.app_id1, robot.app_id2, 0xFE, 0xFF, 0, 0, 0, 0])
    d[9] = (sum(d[0:9]) + CHECK_KEY) & 0xFF
    return bytes(d)


def build_command_10(c: int, d_val: int) -> bytes:
    """10-byte motor command (simpler, always fits in BLE advertising)."""
    data = bytearray([
        robot.bid1 & 0xFF, robot.bid2 & 0xFF,
        robot.app_id1, robot.app_id2,
        c & 0xFF, d_val & 0xFF,
        0,                              # single-device mode
        robot.motor_a & 0xFF, robot.motor_b & 0xFF,
        0                               # checksum
    ])
    data[9] = (sum(data[0:9]) + CHECK_KEY) & 0xFF
    return bytes(data)


def build_command_14(c: int, d_val: int) -> bytes:
    """
    14-byte motor command with obfuscation.

    Key fixes vs v1:
      - data[0]/[1] = robot bid1/bid2 (not 0/0)
      - data[6] = 0 (single-device mode, not 0xFF broadcast)
      - data[13] = 128 (0x80, matching Android bridge)
    """
    rand = random.randint(1, 255)
    data = bytearray(14)
    data[0]  = robot.bid1 & 0xFF          # robot device ID (was 0)
    data[1]  = robot.bid2 & 0xFF          # robot device ID (was 0)
    data[2]  = robot.app_id1
    data[3]  = robot.app_id2
    data[4]  = c     & 0xFF
    data[5]  = d_val & 0xFF
    data[6]  = 0                           # single-device mode (was 0xFF broadcast)
    data[7]  = robot.motor_a & 0xFF
    data[8]  = robot.motor_b & 0xFF
    data[10] = rand
    data[11] = 0
    data[12] = NEUTRAL & 0xFF
    data[13] = 128                         # 0x80 (was 0)

    # Obfuscation
    i1 = rand & 0x1F
    data[4]  = (data[4]  + CH37[i1])     & 0xFF
    data[5]  = (data[5]  + CH37[i1 + 1]) & 0xFF
    i2 = (rand & 0xF8) // 8
    data[6]  = (data[6]  + CH38[i2])     & 0xFF
    data[7]  = (data[7]  + CH39[i2])     & 0xFF
    data[8]  = (data[8]  + CH39[i2 + 1]) & 0xFF
    i3 = (rand & 0x3E) // 2
    data[11] = (data[11] + CH38[i3])     & 0xFF
    data[12] = (data[12] + CH38[i3 + 1]) & 0xFF
    data[13] = (data[13] + CH38[i3 + 3]) & 0xFF

    data[9] = (sum(data[0:9]) + sum(data[10:14]) + CHECK_KEY) & 0xFF
    return bytes(data)


def build_current_command() -> bytes:
    return build_command_14(robot.motor_c, robot.motor_d)


def encrypt_and_send(raw: bytes) -> bool:
    return advertiser.advertise(qy_rf_payload(ADDRESS, raw))


# ---------------------------------------------------------------------------
# Send loop
# ---------------------------------------------------------------------------

async def _send_loop():
    while robot.connected:
        try:
            encrypt_and_send(build_current_command())
            await asyncio.sleep(0.15)
        except Exception as e:
            logger.error(f"send loop: {e}")
            await asyncio.sleep(0.5)


def _start_send_loop():
    if robot.send_task is None or robot.send_task.done():
        robot.send_task = asyncio.create_task(_send_loop())


def _stop_send_loop():
    if robot.send_task and not robot.send_task.done():
        robot.send_task.cancel()
        robot.send_task = None


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Robo Defensor Controller",
    instructions="""Control the Robo Defensor robot via Bluetooth Low Energy advertising.
Uses raw HCI commands on the ASUS USB-BT500 USB adapter (bypasses CoreBluetooth).
Call pair() first — it advertises the verify code AND scans for the robot's response
to capture its device IDs. Then use movement commands.

Gear levels: 0=slow, 1=medium, 2=fast (default 2).
""",
)


@mcp.tool()
async def pair() -> str:
    """
    Pair with the Robo Defensor robot.

    Simultaneously:
      1. Advertises the verify code via BLE (ADV_IND, connectable).
      2. Scans for the robot's advertising response to capture bid1/bid2.

    Robot must be powered on with LED blinking.
    ASUS USB-BT500 adapter must be plugged in.
    """
    if robot.connected:
        return "Already connected. Call disconnect() first."

    if not advertiser._ensure_init():
        return "ERROR: USB Bluetooth adapter not found. Plug in the ASUS USB-BT500."

    verify = build_verify_code()
    logger.info(f"Verify code: {verify.hex()}")

    # Phase 1: advertise verify code for ~2s to wake robot
    for _ in range(14):
        encrypt_and_send(verify)
        await asyncio.sleep(0.15)

    # Phase 2: scan for robot response in background, keep advertising
    scan_result: list = [None]

    def _do_scan():
        scan_result[0] = advertiser.scan_for_robot(
            robot.app_id1, robot.app_id2, duration=5.0
        )

    scan_thread = threading.Thread(target=_do_scan, daemon=True)
    scan_thread.start()

    t_end = time.time() + 5.5
    while time.time() < t_end and scan_result[0] is None:
        encrypt_and_send(verify)
        await asyncio.sleep(0.15)

    scan_thread.join(timeout=1.0)

    # Update bid bytes from scan
    if scan_result[0]:
        b1, b2, bt = scan_result[0]
        robot.bid1     = b1
        robot.bid2     = b2
        robot.box_type = bt
        logger.info(f"Robot ID captured: bid1={b1} bid2={b2} boxType={bt}")
        bid_msg = f"Robot found! bid1={b1} bid2={b2} boxType={bt}"
    else:
        bid_msg = (f"Robot response not detected in scan. "
                   f"Using stored IDs: bid1={robot.bid1} bid2={robot.bid2}. "
                   f"(Try power-cycling the robot if it doesn't move.)")
        logger.warning(bid_msg)

    robot.connected = True
    _start_send_loop()

    return (
        f"Paired!\n"
        f"{bid_msg}\n"
        f"Gear: {robot.gear} (0=slow 1=med 2=fast)\n"
        f"Commands: forward() backward() left() right() stop() set_gear()"
    )


@mcp.tool()
async def scan_for_robot() -> str:
    """Scan for the robot's BLE advertising and update its device IDs."""
    if not advertiser._ensure_init():
        return "ERROR: USB adapter not ready."
    result = advertiser.scan_for_robot(robot.app_id1, robot.app_id2, duration=6.0)
    if result:
        robot.bid1, robot.bid2, robot.box_type = result
        return f"Robot found: bid1={robot.bid1} bid2={robot.bid2} boxType={robot.box_type}"
    return "Robot not found in scan. Make sure it's powered on."


@mcp.tool()
async def disconnect() -> str:
    """Disconnect from the robot and stop all motor commands."""
    _stop_send_loop()
    advertiser.stop()
    robot.connected = False
    robot.motor_a = robot.motor_b = robot.motor_c = robot.motor_d = NEUTRAL
    return "Disconnected."


@mcp.tool()
async def forward(duration: float = 0) -> str:
    """Move forward. duration=0 means continuous until stop()."""
    if not robot.connected:
        return "Not paired. Call pair() first."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_0[g]
    robot.motor_d = DATA_255[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = robot.motor_d = NEUTRAL
        return f"Moved forward {duration}s."
    return "Moving forward."


@mcp.tool()
async def backward(duration: float = 0) -> str:
    """Move backward. duration=0 means continuous."""
    if not robot.connected:
        return "Not paired."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_255[g]
    robot.motor_d = DATA_0[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = robot.motor_d = NEUTRAL
        return f"Moved backward {duration}s."
    return "Moving backward."


@mcp.tool()
async def left(duration: float = 0) -> str:
    """Spin left. duration=0 means continuous."""
    if not robot.connected:
        return "Not paired."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_255[g]
    robot.motor_d = DATA_255[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = robot.motor_d = NEUTRAL
        return f"Spun left {duration}s."
    return "Spinning left."


@mcp.tool()
async def right(duration: float = 0) -> str:
    """Spin right. duration=0 means continuous."""
    if not robot.connected:
        return "Not paired."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_0[g]
    robot.motor_d = DATA_0[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = robot.motor_d = NEUTRAL
        return f"Spun right {duration}s."
    return "Spinning right."


@mcp.tool()
async def forward_left(duration: float = 0) -> str:
    """Arc forward-left."""
    if not robot.connected:
        return "Not paired."
    g = min(robot.gear, 2)
    robot.motor_c = NEUTRAL
    robot.motor_d = DATA_255[g]
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = robot.motor_d = NEUTRAL
        return f"Forward-left {duration}s."
    return "Moving forward-left."


@mcp.tool()
async def forward_right(duration: float = 0) -> str:
    """Arc forward-right."""
    if not robot.connected:
        return "Not paired."
    g = min(robot.gear, 2)
    robot.motor_c = DATA_0[g]
    robot.motor_d = NEUTRAL
    if duration > 0:
        await asyncio.sleep(duration)
        robot.motor_c = robot.motor_d = NEUTRAL
        return f"Forward-right {duration}s."
    return "Moving forward-right."


@mcp.tool()
async def stop() -> str:
    """Stop all motors."""
    if not robot.connected:
        return "Not paired."
    robot.motor_a = robot.motor_b = robot.motor_c = robot.motor_d = NEUTRAL
    return "Stopped."


@mcp.tool()
async def set_gear(gear: int) -> str:
    """Set speed gear: 0=slow, 1=medium, 2=fast."""
    if not robot.connected:
        return "Not paired."
    robot.gear = max(0, min(2, gear))
    return f"Gear set to {robot.gear}."


@mcp.tool()
async def shoot_on() -> str:
    """Activate shooter (motor A)."""
    if not robot.connected:
        return "Not paired."
    robot.motor_a = DATA_255[robot.gear]
    return "Shooting ON."


@mcp.tool()
async def shoot_off() -> str:
    """Deactivate shooter."""
    if not robot.connected:
        return "Not paired."
    robot.motor_a = NEUTRAL
    return "Shooting OFF."


@mcp.tool()
async def status() -> str:
    """Get current robot state."""
    return (
        f"Connected:  {robot.connected}\n"
        f"Gear:       {robot.gear} (0=slow 1=med 2=fast)\n"
        f"Motors:     A={robot.motor_a} B={robot.motor_b} C={robot.motor_c} D={robot.motor_d}\n"
        f"Robot IDs:  bid1={robot.bid1} bid2={robot.bid2} boxType={robot.box_type}\n"
        f"AppID:      {robot.app_id1}.{robot.app_id2}\n"
        f"Adapter:    {'ready' if advertiser._initialized else 'not initialized'}\n"
        f"Sending:    {robot.send_task is not None and not robot.send_task.done()}"
    )


@mcp.tool()
async def send_raw_motors(a: int = 128, b: int = 128,
                          c: int = 128, d: int = 128) -> str:
    """Set raw motor bytes (0-255, 128=neutral). a=shooter b=unused c=left/right d=fwd/back."""
    if not robot.connected:
        return "Not paired."
    robot.motor_a = a & 0xFF
    robot.motor_b = b & 0xFF
    robot.motor_c = c & 0xFF
    robot.motor_d = d & 0xFF
    return f"Raw motors: A={a} B={b} C={c} D={d}"


@mcp.tool()
async def dance(pattern: str = "spin") -> str:
    """Run a movement pattern: 'spin', 'square', 'zigzag'."""
    if not robot.connected:
        return "Not paired."
    g    = min(robot.gear, 2)
    step = 0.6

    if pattern == "spin":
        for _ in range(2):
            robot.motor_c = DATA_0[g];   robot.motor_d = DATA_0[g]
            await asyncio.sleep(step * 2)
            robot.motor_c = DATA_255[g]; robot.motor_d = DATA_255[g]
            await asyncio.sleep(step * 2)
    elif pattern == "square":
        for _ in range(4):
            robot.motor_c = DATA_0[g];   robot.motor_d = DATA_255[g]
            await asyncio.sleep(step)
            robot.motor_c = DATA_0[g];   robot.motor_d = DATA_0[g]
            await asyncio.sleep(step * 0.5)
    elif pattern == "zigzag":
        for _ in range(4):
            robot.motor_c = DATA_0[g];   robot.motor_d = NEUTRAL
            await asyncio.sleep(step)
            robot.motor_c = NEUTRAL;     robot.motor_d = DATA_255[g]
            await asyncio.sleep(step)

    robot.motor_c = robot.motor_d = NEUTRAL
    return f"Completed '{pattern}'."


if __name__ == "__main__":
    mcp.run(transport="stdio")
