package com.robobridge;

import android.Manifest;
import android.app.Activity;
import android.bluetooth.*;
import android.bluetooth.le.*;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.TextView;
import android.widget.ScrollView;
import android.graphics.Typeface;

import java.io.*;
import java.net.*;
import java.util.*;

public class MainActivity extends Activity {

    static final int PORT = 8765;
    static BluetoothLeAdvertiser bleAdvertiser;
    static BluetoothAdapter btAdapter;
    static BluetoothGatt connectedGatt;
    static AdvertiseCallback advCallback;
    static Random random = new Random();
    static boolean nativeLoaded = false;
    static boolean gattConnected = false;
    static boolean lastAdvOk = false;
    static int lastAdvErr = 0;
    TextView logView;
    ScrollView scrollView;
    Handler handler;

    // QUNYU protocol
    static final byte CHECK_KEY = 66;
    static final byte[] ADDRESS = {(byte)0xC1,(byte)0xC2,(byte)0xC3,(byte)0xC4,(byte)0xC5};
    static final int[] CH37 = {141,210,87,161,61,167,102,176,117,49,17,72,150,119,248,227,70,233,171,208,158,83,51,216,186,152,8,36,203,59,252,113,163,244,85,104,207,169,25,108,93,76};
    static final int[] CH38 = {214,197,68,32,89,222,225,143,27,165,175,66,123,78,205,96,235,98,34,144,44,239,239,199,141,210,87,161,61,167,102,176,117,49,17,72,150,119,248,227,70,233};
    static final int[] CH39 = {31,55,74,95,133,246,156,154,193,214,197,68,32,89,222,225,143,27,165,175,66,123,78,205,96,235,98,34,144,44,239,239,199,141,210,87,161,61,167,102,176,117};
    static byte appId1 = 17, appId2 = 34;

    // BLE UUIDs for JIMUEDU/QYEDU devices (fee7 set)
    static final UUID SVC_UUID  = UUID.fromString("0000fee7-0000-1000-8000-00805f9b34fb");
    static final UUID WRITE_UUID = UUID.fromString("0000fec7-0000-1000-8000-00805f9b34fb");
    static final UUID NOTIFY_UUID = UUID.fromString("0000fec7-0000-1000-8000-00805f9b34fb");
    // Also try fee0 set
    static final UUID SVC_UUID2  = UUID.fromString("0000fee0-0000-1000-8000-00805f9b34fb");
    static final UUID WRITE_UUID2 = UUID.fromString("0000fee2-0000-1000-8000-00805f9b34fb");
    static final UUID NOTIFY_UUID2 = UUID.fromString("0000fee1-0000-1000-8000-00805f9b34fb");

    // Robot device name prefixes
    static final String[] ROBOT_NAMES = {"JIMUEDU", "QYEDU", "QUNYU_", "QYMA", "QYMC", "QYSP"};

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        scrollView = new ScrollView(this);
        logView = new TextView(this);
        logView.setTypeface(Typeface.MONOSPACE);
        logView.setTextSize(11);
        logView.setPadding(16,16,16,16);
        scrollView.addView(logView);
        setContentView(scrollView);
        handler = new Handler(Looper.getMainLooper());
        log("RoboBridge v2 starting...");

        if (Build.VERSION.SDK_INT >= 31) {
            requestPermissions(new String[]{
                Manifest.permission.BLUETOOTH_ADVERTISE,
                Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.ACCESS_FINE_LOCATION
            }, 1);
        } else {
            requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION}, 1);
        }
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] results) {
        for (int r : results) if (r != PackageManager.PERMISSION_GRANTED) { log("ERROR: Permissions denied"); return; }
        log("Permissions OK");
        initBLE();
    }

    void initBLE() {
        BluetoothManager btm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
        btAdapter = btm != null ? btm.getAdapter() : null;
        if (btAdapter == null || !btAdapter.isEnabled()) { log("ERROR: BT not enabled"); return; }
        bleAdvertiser = btAdapter.getBluetoothLeAdvertiser();
        log("BLE advertiser: " + (bleAdvertiser != null));

        // Load native encryption lib
        loadNativeLib();
        log("Native lib: " + nativeLoaded);

        String ip = getWifiIP();
        log("Server: http://" + ip + ":" + PORT);
        log("\nEndpoints:");
        log("  /scan    - scan for robot");
        log("  /pair    - GATT connect + advertise verify");
        log("  /move?a=&b=&c=&d= - motor control");
        log("  /stop    - stop motors");
        log("  /status  - check status");
        log("\nWaiting for commands...\n");

        new Thread(this::runServer).start();
    }

    // --- Pair: scan + advertise simultaneously (replicates Robolab flow) ---

    void doPair() {
        log("PAIR: Starting scan + advertise simultaneously...");

        // Step 1: Start BLE scan (looking for robot's response)
        BluetoothLeScanner scanner = btAdapter.getBluetoothLeScanner();
        if (scanner == null) { log("ERROR: No scanner"); return; }

        ScanCallback scanCb = new ScanCallback() {
            public void onScanResult(int type, ScanResult result) {
                // Check manufacturer data for our appId
                android.util.SparseArray<byte[]> mfrData = result.getScanRecord() != null ?
                    result.getScanRecord().getManufacturerSpecificData() : null;
                if (mfrData == null) return;
                for (int i = 0; i < mfrData.size(); i++) {
                    byte[] data = mfrData.valueAt(i);
                    int mfrId = mfrData.keyAt(i);
                    if (data != null && data.length > 7 &&
                        data[2] == appId1 && data[3] == appId2) {
                        robotBid1 = data[0];
                        robotBid2 = data[1];
                        robotBoxType = data[7] & 0xFF;
                        log("ROBOT FOUND! bid=" + robotBid1 + "," + robotBid2 + " boxType=" + robotBoxType + " mfrId=" + mfrId);
                        gattConnected = true; // Mark as connected
                        scanner.stopScan(this);
                        return;
                    }
                }
                // Log ALL manufacturer data for debugging
                for (int j = 0; j < mfrData.size(); j++) {
                    byte[] d2 = mfrData.valueAt(j);
                    int mid2 = mfrData.keyAt(j);
                    String nm = result.getDevice().getName();
                    if (d2 != null && d2.length > 4) {
                        log("  mfr 0x" + Integer.toHexString(mid2) + " len=" + d2.length + " " + (nm != null ? nm : result.getDevice().getAddress()));
                    }
                }
            }
            public void onScanFailed(int error) { log("Scan failed: " + error); }
        };

        ScanSettings settings = new ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        scanner.startScan(null, settings, scanCb);

        // Step 2: SIMULTANEOUSLY start advertising the verify code
        byte[] verify = makeVerify();
        byte[] encrypted = qyRfPayload(ADDRESS, verify);
        log("PAIR: Verify raw=" + bytesToHex(verify));
        log("PAIR: Verify enc=" + bytesToHex(encrypted));

        AdvertiseData advData = new AdvertiseData.Builder()
            .addManufacturerData(115200, encrypted).build();
        AdvertiseSettings advSettings = new AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
            .setConnectable(true).setTimeout(180000)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH).build();

        if (advCallback != null) {
            try { bleAdvertiser.stopAdvertising(advCallback); } catch (Exception e) {}
        }
        advCallback = new AdvertiseCallback() {
            public void onStartSuccess(AdvertiseSettings s) { log("PAIR: Advertising started OK"); }
            public void onStartFailure(int e) { log("PAIR: Advertising FAILED: " + e); }
        };
        bleAdvertiser.startAdvertising(advSettings, advData, advCallback);

        // Auto-stop scan after 5 seconds
        handler.postDelayed(() -> {
            try { scanner.stopScan(scanCb); } catch (Exception e) {}
            if (!gattConnected) {
                log("PAIR: Scan finished, robot not found in responses");
                log("PAIR: But advertising continues - robot may still connect");
            }
        }, 5000);
    }

    // --- GATT Scan & Connect (for GATT-based devices) ---

    BluetoothDevice foundRobot = null;

    void scanForRobot() {
        log("Scanning for robot (JIMUEDU/QYEDU)...");
        foundRobot = null;
        BluetoothLeScanner scanner = btAdapter.getBluetoothLeScanner();
        if (scanner == null) { log("ERROR: No scanner"); return; }

        ScanCallback cb = new ScanCallback() {
            public void onScanResult(int type, ScanResult result) {
                String name = result.getDevice().getName();
                if (name == null) return;
                for (String prefix : ROBOT_NAMES) {
                    if (name.contains(prefix)) {
                        foundRobot = result.getDevice();
                        log("FOUND: " + name + " addr=" + result.getDevice().getAddress() + " RSSI=" + result.getRssi());
                        scanner.stopScan(this);
                        return;
                    }
                }
            }
            public void onScanFailed(int error) { log("Scan failed: " + error); }
        };

        ScanSettings settings = new ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        scanner.startScan(null, settings, cb);

        // Auto-stop after 8 seconds
        handler.postDelayed(() -> {
            try { scanner.stopScan(cb); } catch (Exception e) {}
            if (foundRobot == null) log("No robot found in scan");
        }, 8000);
    }

    void connectGatt() {
        if (foundRobot == null) { log("No robot found. Run /scan first"); return; }
        log("GATT connecting to " + foundRobot.getName() + "...");
        connectedGatt = foundRobot.connectGatt(this, false, new BluetoothGattCallback() {
            public void onConnectionStateChange(BluetoothGatt gatt, int status, int newState) {
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    log("GATT connected! Discovering services...");
                    gatt.discoverServices();
                    gattConnected = true;
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    log("GATT disconnected");
                    gattConnected = false;
                }
            }
            public void onServicesDiscovered(BluetoothGatt gatt, int status) {
                log("Services discovered:");
                for (BluetoothGattService svc : gatt.getServices()) {
                    log("  SVC: " + svc.getUuid());
                    for (BluetoothGattCharacteristic c : svc.getCharacteristics()) {
                        log("    CHR: " + c.getUuid() + " props=" + c.getProperties());
                    }
                }
                // Enable notifications
                enableNotify(gatt);
                // Now also start advertising (for motor control)
                log("GATT ready! Starting BLE advertising for motor control...");
                sendBLEAdv(makeVerify());
            }
            public void onCharacteristicChanged(BluetoothGatt gatt, BluetoothGattCharacteristic c, byte[] value) {
                log("NOTIFY: " + bytesToHex(value));
            }
        }, BluetoothDevice.TRANSPORT_LE);
    }

    void enableNotify(BluetoothGatt gatt) {
        // Try fee7/fec7 first, then fee0/fee1
        UUID[][] pairs = {{SVC_UUID, NOTIFY_UUID}, {SVC_UUID2, NOTIFY_UUID2}};
        for (UUID[] pair : pairs) {
            BluetoothGattService svc = gatt.getService(pair[0]);
            if (svc != null) {
                BluetoothGattCharacteristic c = svc.getCharacteristic(pair[1]);
                if (c != null) {
                    gatt.setCharacteristicNotification(c, true);
                    BluetoothGattDescriptor desc = c.getDescriptor(UUID.fromString("00002902-0000-1000-8000-00805f9b34fb"));
                    if (desc != null) {
                        desc.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                        gatt.writeDescriptor(desc);
                    }
                    log("Notifications enabled on " + pair[1]);
                    return;
                }
            }
        }
        log("WARN: No notify characteristic found");
    }

    void writeGatt(byte[] data) {
        if (connectedGatt == null) return;
        UUID[][] pairs = {{SVC_UUID, WRITE_UUID}, {SVC_UUID2, WRITE_UUID2}};
        for (UUID[] pair : pairs) {
            BluetoothGattService svc = connectedGatt.getService(pair[0]);
            if (svc != null) {
                BluetoothGattCharacteristic c = svc.getCharacteristic(pair[1]);
                if (c != null) {
                    c.setValue(data);
                    connectedGatt.writeCharacteristic(c);
                    return;
                }
            }
        }
    }

    // --- BLE Advertising (for motor commands after GATT connect) ---

    void sendBLEAdv(byte[] rawCmd) {
        if (bleAdvertiser == null) return;
        byte[] encrypted = qyRfPayload(ADDRESS, rawCmd);
        AdvertiseData data = new AdvertiseData.Builder()
            .addManufacturerData(115200, encrypted).build();
        AdvertiseSettings settings = new AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
            .setConnectable(true).setTimeout(180000)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH).build();
        if (advCallback != null) {
            try { bleAdvertiser.stopAdvertising(advCallback); } catch (Exception e) {}
        }
        advCallback = new AdvertiseCallback() {
            public void onStartSuccess(AdvertiseSettings s) { lastAdvOk = true; }
            public void onStartFailure(int e) { lastAdvOk = false; lastAdvErr = e; log("ADV FAIL:" + e); }
        };
        bleAdvertiser.startAdvertising(settings, data, advCallback);
    }

    // Also write via GATT as the app does for some data paths
    void sendBoth(byte[] rawCmd) {
        sendBLEAdv(rawCmd);
        // Also try writing raw 16-byte default data via GATT
        if (gattConnected) {
            writeGatt(rawCmd);
        }
    }

    // --- Packet builders ---
    static byte[] makeVerify() {
        byte[] d = {0,0,appId1,appId2,(byte)0xFE,(byte)0xFF,0,0,0,0};
        d[9] = (byte)((sum(d,0,9)+CHECK_KEY)&0xFF);
        return d;
    }

    // Robot device ID from pairing
    static byte robotBid1 = 0, robotBid2 = 0;
    static int robotBoxType = 2;

    // Matches getDataSingle_14(i, i2, i3, i4, i5, i6) from BlueDataUtils
    // Called as: sendSingle(data[0], data[1], data[2], data[3])
    // → getDataSingle_14(data[0], data[1], data[2], data[3], 128, 0)
    // Packet layout: [bid1, bid2, appId1, appId2, i3(data[2]), i4(data[3]), 0, i(data[0]), i2(data[1]), CRC, rand, i6(0), i5(128), -128]
    static byte[] makeCmd14(int a, int b, int c, int d) {
        int rand = random.nextInt(255)+1;
        byte[] data = new byte[14];
        data[0] = robotBid1;   // Robot's device ID
        data[1] = robotBid2;
        data[2] = appId1;
        data[3] = appId2;
        data[4] = (byte)(c & 0xFF);     // i3 = data[2] from ControlUtil
        data[5] = (byte)(d & 0xFF);     // i4 = data[3]
        data[6] = 0;                     // 0 for single device (NOT 0xFF)
        data[7] = (byte)(a & 0xFF);     // i = data[0]
        data[8] = (byte)(b & 0xFF);     // i2 = data[1]
        data[10] = (byte)rand;
        data[11] = 0;                    // i6 = 0
        data[12] = (byte)128;           // i5 = 128
        data[13] = (byte)128;           // -128 signed = 128 unsigned = 0x80

        // Apply obfuscation (encryptData14 from BlueDataUtils)
        int i1 = rand & 0x1F;
        data[4] = (byte)((data[4] + CH37[i1]) & 0xFF);
        data[5] = (byte)((data[5] + CH37[i1 + 1]) & 0xFF);
        int i2x = (rand & 0xF8) / 8;
        data[6] = (byte)((data[6] + CH38[i2x]) & 0xFF);
        data[7] = (byte)((data[7] + CH39[i2x]) & 0xFF);
        data[8] = (byte)((data[8] + CH39[i2x + 1]) & 0xFF);
        int i3x = (rand & 0x3E) / 2;
        data[11] = (byte)((data[11] + CH38[i3x]) & 0xFF);
        data[12] = (byte)((data[12] + CH38[i3x + 1]) & 0xFF);
        data[13] = (byte)((data[13] + CH38[i3x + 3]) & 0xFF);

        // checking14: sum of [0..8] + [10..13] + checkKey
        data[9] = (byte)((sum(data,0,9) + sum(data,10,14) + CHECK_KEY) & 0xFF);
        return data;
    }

    // 10-byte command (boxType 0 style, simpler, always fits in advertising)
    static byte[] makeCmd10(int a, int b, int c, int d) {
        byte[] data = new byte[]{robotBid1, robotBid2, appId1, appId2,
            (byte)(c & 0xFF), (byte)(d & 0xFF), 0, (byte)(a & 0xFF), (byte)(b & 0xFF), 0};
        data[9] = (byte)((sum(data,0,9) + CHECK_KEY) & 0xFF);
        return data;
    }

    static int sum(byte[] a,int f,int t) { int s=0; for(int i=f;i<t;i++) s+=(a[i]&0xFF); return s; }

    // --- Native lib ---
    static void loadNativeLib() {
        if (nativeLoaded) return;
        try { System.loadLibrary("qylib"); nativeLoaded = true; return; } catch (Throwable t) {}
        String[] paths = {"/data/local/tmp/lib/libqylib.so"};
        for (String p : paths) {
            try { System.load(p); nativeLoaded = true; return; } catch (Throwable t) {}
        }
    }

    static byte[] qyRfPayload(byte[] addr, byte[] input) {
        // Match original BlueDataUtils: 10→20, 11→21, 14→24
        int resultSize = input.length + 10;
        byte[] result = new byte[resultSize];
        if (nativeLoaded) {
            try { com.qunyu.method.BLEUtil.QY_rf_payload(addr, addr.length, input, input.length, result); }
            catch (Throwable t) {}
        }
        return result;
    }

    // --- HTTP Server ---
    void runServer() {
        try {
            ServerSocket ss = new ServerSocket(PORT);
            while (true) {
                try { handleRequest(ss.accept()); } catch (Exception e) { log("!! " + e.getMessage()); }
            }
        } catch (Exception e) { log("Server error: " + e.getMessage()); }
    }

    void handleRequest(Socket client) throws IOException {
        BufferedReader in = new BufferedReader(new InputStreamReader(client.getInputStream()));
        PrintWriter out = new PrintWriter(client.getOutputStream(), true);
        String line = in.readLine();
        if (line == null) { client.close(); return; }
        while (true) { String h = in.readLine(); if (h == null || h.isEmpty()) break; }

        String path = line.split(" ")[1];
        String response = "OK";

        try {
            if (path.equals("/scan")) {
                handler.post(this::scanForRobot);
                response = "SCANNING";
                log(">> scan");
            } else if (path.equals("/pair")) {
                // Replicate exact Robolab flow: scan + advertise simultaneously
                handler.post(() -> doPair());
                response = "PAIRING";
                log(">> pair");
            } else if (path.startsWith("/move")) {
                int a=128,b=128,c=128,d=128;
                String q = path.contains("?") ? path.split("\\?")[1] : "";
                for (String p : q.split("&")) {
                    String[] kv = p.split("=");
                    if (kv.length==2) switch(kv[0]) {
                        case "a": a=Integer.parseInt(kv[1]); break;
                        case "b": b=Integer.parseInt(kv[1]); break;
                        case "c": c=Integer.parseInt(kv[1]); break;
                        case "d": d=Integer.parseInt(kv[1]); break;
                    }
                }
                byte[] cmd = makeCmd14(a,b,c,d);
                sendBoth(cmd);
                response = "MOVE a="+a+" b="+b+" c="+c+" d="+d;
            } else if (path.startsWith("/move10")) {
                // Use simpler 10-byte format
                int a=128,b=128,c=128,d=128;
                String q = path.contains("?") ? path.split("\\?")[1] : "";
                for (String p : q.split("&")) {
                    String[] kv = p.split("=");
                    if (kv.length==2) switch(kv[0]) {
                        case "a": a=Integer.parseInt(kv[1]); break;
                        case "b": b=Integer.parseInt(kv[1]); break;
                        case "c": c=Integer.parseInt(kv[1]); break;
                        case "d": d=Integer.parseInt(kv[1]); break;
                    }
                }
                sendBLEAdv(makeCmd10(a,b,c,d));
                response = "MOVE10 a="+a+" b="+b+" c="+c+" d="+d;
            } else if (path.equals("/stop")) {
                sendBoth(makeCmd14(128,128,128,128));
                response = "STOPPED";
            } else if (path.equals("/status")) {
                response = "native=" + nativeLoaded + " gatt=" + gattConnected + " robot=" + (foundRobot != null ? foundRobot.getName() : "none") + " adv=" + (bleAdvertiser!=null) + " advOk=" + lastAdvOk + " advErr=" + lastAdvErr + " bid=" + robotBid1 + "," + robotBid2 + " box=" + robotBoxType;
            }
        } catch (Exception e) {
            response = "ERROR: " + e.getMessage();
            log("!! " + e.getMessage());
        }

        out.print("HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: "+response.length()+"\r\n\r\n"+response);
        out.flush();
        client.close();
    }

    static String bytesToHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder();
        for (byte b : bytes) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    String getWifiIP() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (ni.getName().contains("wlan")) {
                    for (InetAddress addr : Collections.list(ni.getInetAddresses())) {
                        if (!addr.isLoopbackAddress() && addr.getHostAddress().contains("."))
                            return addr.getHostAddress();
                    }
                }
            }
        } catch (Exception e) {}
        return "unknown";
    }

    void log(String msg) {
        handler.post(() -> {
            logView.append(msg + "\n");
            scrollView.post(() -> scrollView.fullScroll(ScrollView.FOCUS_DOWN));
        });
    }
}
