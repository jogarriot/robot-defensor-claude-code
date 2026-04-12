# Robo Defensor BLE Control - Complete Documentation

## Status: WORKING ✅

We can control the Robo Defensor robot from a Mac via an Android phone bridge.

---

## Architecture

```
Mac (Claude Code MCP)  →  HTTP  →  Android Phone (BLE Bridge App)  →  BLE Advertising  →  Robot
```

- **Mac** sends HTTP commands to phone on local WiFi
- **Phone** encrypts commands with native `libqylib.so` and broadcasts via BLE advertising
- **Robot** receives advertising packets and executes motor commands

macOS cannot do BLE advertising with custom manufacturer data (CoreBluetooth blocks it), so the Android phone is required as a relay.

---

## Working Configuration

### Phone App
- **Package name:** `com.qunyu.robolab` (MUST match — native lib checks this)
- **Server:** HTTP on port 8765
- **Native lib:** `libqylib.so` embedded in APK, loaded via `System.loadLibrary("qylib")`

### BLE Advertising Settings
- **Manufacturer ID:** `115200` (decimal, passed to `addManufacturerData`)
- **Advertise mode:** `LOW_LATENCY` (mode 2)
- **Connectable:** `true`
- **Timeout:** `180000` (180 seconds)
- **TX Power:** `HIGH` (level 3)

### Robot Identity (from pairing response)
- **bid1:** 48
- **bid2:** -14
- **boxType:** 2
- **mfrId in response:** 65520 (0xFFF0)

### Encryption
- **Address:** `{0xC1, 0xC2, 0xC3, 0xC4, 0xC5}` (5 bytes)
- **Function:** `QY_rf_payload(address, 5, data, dataLen, result)`
- **Result buffer size:** `input.length + 10` (NOT addr.length + input.length + 10)
  - 10-byte input → 20-byte result
  - 14-byte input → 24-byte result
- **Check key:** 66 (0x42)
- **App ID:** appId1=17, appId2=34

---

## Pairing Flow (MUST scan + advertise simultaneously)

1. Start BLE scan (looking for robot's response in manufacturer data)
2. SIMULTANEOUSLY start advertising verify code
3. Verify code: `[0, 0, appId1, appId2, 0xFE, 0xFF, 0, 0, 0, checksum]`
4. Checksum: `sum(bytes[0..8]) + checkKey(66)` → byte 9
5. Encrypt with `QY_rf_payload` → 20-byte result
6. Advertise as `addManufacturerData(115200, encrypted)`
7. Robot sees verify code → advertises back with `[bid1, bid2, appId1, appId2, ..., boxType, ...]`
8. Phone scan picks up response → pairing complete

**If robot doesn't pair:** Power cycle the robot. It gets stuck if a previous session didn't terminate cleanly.

---

## Motor Commands (10-byte format, WORKING)

### Packet Format
```
byte[0] = bid1 (48)      // Robot device ID
byte[1] = bid2 (-14)     // Robot device ID
byte[2] = appId1 (17)    // App ID
byte[3] = appId2 (34)    // App ID
byte[4] = c              // Motor C value (0-255, 128=neutral)
byte[5] = d              // Motor D value (0-255, 128=neutral)
byte[6] = 0              // Single device mode
byte[7] = a              // Motor A value (0-255, 128=neutral)
byte[8] = b              // Motor B value (0-255, 128=neutral)
byte[9] = checksum       // sum(bytes[0..8]) + checkKey(66)
```

Then encrypted with `QY_rf_payload` → 20-byte result → advertise.

### Motor Mapping (confirmed working)

| Action | a | b | c | d |
|--------|---|---|---|---|
| **Forward** | 128 | 128 | 0 | 255 |
| **Backward** | 128 | 128 | 255 | 0 |
| **Spin Right** | 128 | 128 | 255 | 255 |
| **Spin Left** | 128 | 128 | 0 | 0 |
| **Stop** | 128 | 128 | 128 | 128 |

- `c` and `d` are the main drive motors
- `c=0, d=255` = forward
- `c=255, d=255` = spin (180 degree turn confirmed)
- `c=0, d=0` = spin opposite direction
- `a` and `b` seem unused or for auxiliary motors
- 128 = neutral/stop for all motors

### Speed Control
- Values 0 and 255 = full speed
- Values closer to 128 = slower
- Exact speed tables from APK: `DATA_255 = [186, 221, 255]`, `DATA_0 = [58, 23, 0]` for gear 0/1/2

---

## HTTP API (phone bridge)

| Endpoint | Description |
|----------|-------------|
| `GET /pair` | Scan + advertise to pair with robot |
| `GET /move10?a=128&b=128&c=0&d=255` | Send 10-byte motor command |
| `GET /move?a=128&b=128&c=0&d=255` | Send 14-byte motor command |
| `GET /stop` | Stop all motors (sends 128,128,128,128) |
| `GET /scan` | GATT scan for named devices |
| `GET /status` | Connection status, native lib, bid, etc. |

---

## Common Issues

### Robot doesn't pair
- **Power cycle the robot** — it gets stuck after incomplete sessions
- Restart the bridge app: `adb shell am force-stop com.qunyu.robolab`
- Pairing is intermittent — may need 1-3 attempts

### App crashes / "address in use"
- Force stop and restart: `adb shell am force-stop com.qunyu.robolab && sleep 2 && adb shell am start -n "com.qunyu.robolab/com.robobridge.MainActivity"`
- Port 8765 conflict resolves after the old process dies

### ADB disconnects
- Reconnect USB cable
- Check: `adb devices`

### Advertising fails (DATA_TOO_LARGE)
- 10-byte commands (20-byte encrypted) always fit
- 14-byte commands (24-byte encrypted) are at the BLE limit (28 bytes of manufacturer data)
- Use `/move10` endpoint instead of `/move` if 14-byte fails

---

## Key Bugs We Fixed (for reference)

1. **Manufacturer ID:** `0xC200` (49664) → `115200` — robot ignores wrong ID
2. **Package name:** `com.robobridge` → `com.qunyu.robolab` — native lib returns zeros for wrong package
3. **Result buffer size:** `addr.length + input.length + 10` → `input.length + 10` — caused DATA_TOO_LARGE
4. **Advertising timeout:** `0` → `180000` — 0 means immediate stop
5. **Mode byte:** `0xFF` (broadcast) → `0` (single device) — robot ignores broadcast after pairing
6. **bid bytes:** `0, 0` → `48, -14` — must use robot's actual device ID
7. **Missing scan:** Must scan AND advertise simultaneously for pairing to work
8. **macOS can't advertise:** CoreBluetooth blocks custom manufacturer data — Android bridge required

---

## Files

| File | Purpose |
|------|---------|
| `android-bridge/app/src/main/java/com/robobridge/MainActivity.java` | Android BLE bridge app |
| `android-bridge/app/src/main/java/com/qunyu/method/BLEUtil.java` | JNI wrapper for native encryption |
| `android-bridge/app/src/main/AndroidManifest.xml` | App manifest (package: com.qunyu.robolab) |
| `android-bridge/build_apk.sh` | Build script for APK |
| `robo_defensor_mcp.py` | MCP server for Claude Code (needs update to use HTTP bridge) |

---

## Quick Start

```bash
# 1. Connect phone via USB
adb devices

# 2. Start bridge app
adb shell am force-stop com.qunyu.robolab
adb shell am start -n "com.qunyu.robolab/com.robobridge.MainActivity"

# 3. Wait for app to start
sleep 5

# 4. Pair with robot (robot must be on, LED blinking)
curl http://192.168.15.71:8765/pair
sleep 7
curl http://192.168.15.71:8765/status  # should show gatt=true

# 5. Move!
curl "http://192.168.15.71:8765/move10?a=128&b=128&c=0&d=255"   # forward
curl "http://192.168.15.71:8765/move10?a=128&b=128&c=255&d=255"  # spin
curl http://192.168.15.71:8765/stop
```
