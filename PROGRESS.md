# Robo Defensor BLE Control - Progress Notes

## What WORKS (confirmed)

### Pairing ✅
- The robot DOES respond to our BLE advertising verify code
- Successful pair observed with these conditions:
  - Package name: `com.qunyu.robolab` (native lib requires this)
  - Native lib `libqylib.so` loaded from APK: `native=true`
  - Manufacturer ID: `115200` (NOT 0xC200)
  - Verify code: `[0, 0, 17, 34, 0xFE, 0xFF, 0, 0, 0, checksum]` with checkKey=66
  - Result buffer size: `input.length + 10` (10-byte input → 20-byte encrypted)
  - Advertising settings: LOW_LATENCY, connectable=true, timeout=180000, TX_POWER_HIGH
  - Phone must SCAN and ADVERTISE simultaneously (doPair method)

### Robot Response (from successful pair)
- Robot responded with: `bid=48,-14 boxType=2 mfrId=65520`
- Note: mfrId from robot response was `65520` (0xFFF0), not 115200
- boxType=2 means use `controlSingle_3` → `getDataSingle_14` format

### Advertising Size
- 10-byte input → 20-byte encrypted: **FITS** in BLE advertising (confirmed, pair works)
- 14-byte input → 24-byte encrypted: **MIGHT BE TOO LARGE** (first motor attempt returned `ADVERTISE_FAILED: 1` = DATA_TOO_LARGE when buffer was wrong size 25)
- After fixing buffer to 24 bytes: untested whether it actually succeeds

## What DOESN'T work (yet)

### Motor Commands ❌
- Robot paired but wheels did not move
- 8 motor combinations tested (a/b/c/d each at 0 and 255), none moved
- Possible causes:
  1. The 14-byte command advertising may be silently failing (DATA_TOO_LARGE)
  2. The motor command format may be wrong
  3. The bid/mode bytes in the command may be wrong

## Architecture

### Protocol Flow (from APK analysis)
1. Phone starts BLE scan AND advertises verify code SIMULTANEOUSLY
2. Robot sees verify code, advertises back with its bid1/bid2/boxType
3. Phone detects robot response in scan callback
4. Connection established - phone now sends motor commands via advertising
5. For boxType=2: uses 14-byte `getDataSingle_14` format with `encryptData14` obfuscation

### Motor Command Format (14-byte, boxType=2)
```
getDataSingle_14(i, i2, i3, i4, i5, i6):
  data[0] = bid1        // Robot device ID byte 1
  data[1] = bid2        // Robot device ID byte 2
  data[2] = appId1      // App ID byte 1 (17)
  data[3] = appId2      // App ID byte 2 (34)
  data[4] = i3          // Motor C (from ControlUtil data[2])
  data[5] = i4          // Motor D (from ControlUtil data[3])
  data[6] = 0           // Single device mode (NOT 0xFF broadcast)
  data[7] = i           // Motor A (from ControlUtil data[0])
  data[8] = i2          // Motor B (from ControlUtil data[1])
  data[9] = checksum    // checking14()
  data[10] = random     // Random byte for obfuscation
  data[11] = i6         // Extra param (usually 0)
  data[12] = i5         // Extra param (usually 128)
  data[13] = -128       // 0x80
  Then: encryptData14() obfuscation, checking14() checksum
  Then: QY_rf_payload() encryption → 24-byte result
```

### ControlUtil0 Motor Mapping (Robo Defensor)
```
DATA_255 = [186, 221, 255]  // gear 0,1,2
DATA_0   = [58, 23, 0]      // gear 0,1,2
NEUTRAL  = 128

forward():     data[2]=DATA_0[gear], data[3]=DATA_255[gear]
backward():    data[2]=DATA_255[gear], data[3]=DATA_0[gear]
left():        data[2]=DATA_255[gear], data[3]=DATA_255[gear]
right():       data[2]=DATA_0[gear], data[3]=DATA_0[gear]
forwardLeft(): data[2]=128, data[3]=DATA_255[gear]
forwardRight():data[2]=DATA_0[gear], data[3]=128
stop():        all = 128

sendSingle(data[0], data[1], data[2], data[3]) is called
```

## Key Files
- Bridge app: `/Users/jotas/2026/robolab/android-bridge/app/src/main/java/com/robobridge/MainActivity.java`
- Native wrapper: `/Users/jotas/2026/robolab/android-bridge/app/src/main/java/com/qunyu/method/BLEUtil.java`
- Original APK decompiled: `/tmp/robolab_decompiled/sources/com/qunyu/robolab/`
- Key original files:
  - `blueutils/BlePeripheralSendUtil.java` - main send logic
  - `blueutils/BlueAdvertiseManager.java` - advertising management
  - `blueutils/BlueDataUtils.java` - packet construction + encryption tables
  - `modular/expansion/data/ControlUtil0.java` - Robo Defensor motor mapping
  - `modular/expansion/control/cb01/ExpansionControl0Activity.java` - BT button handler
  - `blueutils/BlePeripheralConnectUtil.java` - connect flow

## Next Steps
1. Verify 14-byte command advertising actually starts (check advOk/advErr)
2. If 14-byte fails: try 10-byte format (controlSingle_1 / boxType=0 style)
3. The robot may accept 10-byte commands even with boxType=2
4. Also need to verify the bid values are being used correctly in motor commands
