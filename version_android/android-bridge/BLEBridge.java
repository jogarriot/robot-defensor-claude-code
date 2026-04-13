import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.os.ParcelUuid;
import android.util.SparseArray;

import java.io.*;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Random;

/**
 * BLE-to-HTTP bridge for Robo Defensor.
 * Runs as a simple HTTP server on port 8765.
 * Receives motor commands and broadcasts them as BLE advertising.
 */
public class BLEBridge {

    static final int PORT = 8765;
    static final int MFR_ID = 0xC200; // 115200 & 0xFFFF

    // QUNYU protocol constants
    static final byte CHECK_KEY = 66;
    static final byte[] ADDRESS = {(byte)0xC1, (byte)0xC2, (byte)0xC3, (byte)0xC4, (byte)0xC5};
    static final int[] CH37 = {141,210,87,161,61,167,102,176,117,49,17,72,150,119,248,227,70,233,171,208,158,83,51,216,186,152,8,36,203,59,252,113,163,244,85,104,207,169,25,108,93,76};
    static final int[] CH38 = {214,197,68,32,89,222,225,143,27,165,175,66,123,78,205,96,235,98,34,144,44,239,239,199,141,210,87,161,61,167,102,176,117,49,17,72,150,119,248,227,70,233};
    static final int[] CH39 = {31,55,74,95,133,246,156,154,193,214,197,68,32,89,222,225,143,27,165,175,66,123,78,205,96,235,98,34,144,44,239,239,199,141,210,87,161,61,167,102,176,117};

    static BluetoothLeAdvertiser advertiser;
    static AdvertiseCallback advCallback;
    static byte appId1 = 17, appId2 = 34;
    static Random random = new Random();

    // --- QY_rf_payload encryption ---

    static int invert8(int b) {
        b &= 0xFF;
        int result = 0;
        for (int i = 0; i < 8; i++) {
            result = (result << 1) | (b & 1);
            b >>= 1;
        }
        return result;
    }

    static int crc16(byte[] addr, byte[] data) {
        int crc = 0xFFFF;
        for (int i = addr.length - 1; i >= 0; i--) {
            crc ^= ((addr[i] & 0xFF) << 8) & 0xFFFF;
            for (int j = 0; j < 8; j++) {
                if ((crc & 0x8000) != 0) crc = ((crc << 1) ^ 0x1021) & 0xFFFF;
                else crc = (crc << 1) & 0xFFFF;
            }
        }
        for (byte b : data) {
            int rev = invert8(b & 0xFF);
            crc ^= (rev << 8) & 0xFFFF;
            for (int j = 0; j < 8; j++) {
                if ((crc & 0x8000) != 0) crc = ((crc << 1) ^ 0x1021) & 0xFFFF;
                else crc = (crc << 1) & 0xFFFF;
            }
        }
        crc = (~crc) & 0xFFFF;
        int result = 0;
        for (int i = 0; i < 16; i++) {
            result = (result << 1) | (crc & 1);
            crc >>= 1;
        }
        return result;
    }

    static int[] whiteningInit(int channel) {
        int[] state = new int[7];
        for (int i = 0; i < 7; i++) state[i] = (channel >> i) & 1;
        return state;
    }

    static int whiteningOutput(int[] state) {
        int out = state[0];
        int fb = state[0] ^ state[4];
        System.arraycopy(state, 1, state, 0, 6);
        state[6] = fb;
        return out;
    }

    static byte[] whiteningEncode(byte[] data, int[] state) {
        byte[] result = new byte[data.length];
        for (int i = 0; i < data.length; i++) {
            int val = 0;
            for (int bit = 0; bit < 8; bit++) {
                int wb = whiteningOutput(state);
                val |= ((((data[i] >> bit) & 1) ^ wb) << bit);
            }
            result[i] = (byte) val;
        }
        return result;
    }

    static byte[] qyRfPayload(byte[] address, byte[] inputData) {
        int addrLen = address.length;
        int dataLen = inputData.length;
        int totalSize = addrLen + dataLen + 20;
        byte[] buf = new byte[totalSize];

        buf[15] = 0x71;
        buf[16] = 0x0F;
        buf[17] = 0x55;

        for (int i = 0; i < addrLen; i++)
            buf[18 + i] = address[addrLen - 1 - i];
        for (int i = 0; i < dataLen; i++)
            buf[18 + addrLen + i] = inputData[i];

        for (int i = 0; i < addrLen + 3; i++)
            buf[15 + i] = (byte) invert8(buf[15 + i] & 0xFF);

        int crc = crc16(address, inputData);
        int crcOff = 18 + addrLen + dataLen;
        buf[crcOff] = (byte) (crc & 0xFF);
        buf[crcOff + 1] = (byte) ((crc >> 8) & 0xFF);

        int wLen1 = addrLen + dataLen + 2;
        int[] s63 = whiteningInit(63);
        byte[] w1 = new byte[wLen1];
        System.arraycopy(buf, 18, w1, 0, wLen1);
        byte[] e1 = whiteningEncode(w1, s63);
        System.arraycopy(e1, 0, buf, 18, wLen1);

        int[] s37 = whiteningInit(37);
        byte[] e2 = whiteningEncode(buf, s37);
        System.arraycopy(e2, 0, buf, 0, totalSize);

        int outLen = addrLen + dataLen + 5;
        byte[] out = new byte[outLen];
        System.arraycopy(buf, 15, out, 0, outLen);
        return out;
    }

    // --- Packet builders ---

    static byte[] makeVerify() {
        byte[] d = new byte[]{0, 0, appId1, appId2, (byte)0xFE, (byte)0xFF, 0, 0, 0, 0};
        d[9] = (byte)((sum(d, 0, 9) + CHECK_KEY) & 0xFF);
        return d;
    }

    static byte[] makeCmd14(int a, int b, int c, int d) {
        int rand = random.nextInt(255) + 1;
        byte[] data = new byte[14];
        data[2] = appId1;
        data[3] = appId2;
        data[4] = (byte)(c & 0xFF);
        data[5] = (byte)(d & 0xFF);
        data[6] = (byte)0xFF;
        data[7] = (byte)(a & 0xFF);
        data[8] = (byte)(b & 0xFF);
        data[10] = (byte)rand;
        data[12] = (byte)128;

        int i1 = rand & 0x1F;
        data[4] = (byte)((data[4] + CH37[i1]) & 0xFF);
        data[5] = (byte)((data[5] + CH37[i1+1]) & 0xFF);
        int i2 = (rand & 0xF8) / 8;
        data[6] = (byte)((data[6] + CH38[i2]) & 0xFF);
        data[7] = (byte)((data[7] + CH39[i2]) & 0xFF);
        data[8] = (byte)((data[8] + CH39[i2+1]) & 0xFF);
        int i3 = (rand & 0x3E) / 2;
        data[11] = (byte)(CH38[i3] & 0xFF);
        data[12] = (byte)((data[12] + CH38[i3+1]) & 0xFF);
        data[13] = (byte)(CH38[i3+3] & 0xFF);

        data[9] = (byte)((sum(data,0,9) + sum(data,10,14) + CHECK_KEY) & 0xFF);
        return data;
    }

    static byte[] makeCmd10(int a, int b, int c, int d) {
        byte[] data = new byte[]{0, 0, appId1, appId2, (byte)(c&0xFF), (byte)(d&0xFF), (byte)0xFF, (byte)(a&0xFF), (byte)(b&0xFF), 0};
        data[9] = (byte)((sum(data,0,9) + CHECK_KEY) & 0xFF);
        return data;
    }

    static int sum(byte[] arr, int from, int to) {
        int s = 0;
        for (int i = from; i < to; i++) s += (arr[i] & 0xFF);
        return s;
    }

    // --- BLE Advertising ---

    static void sendBLE(byte[] rawCmd) {
        if (advertiser == null) return;
        byte[] encrypted = qyRfPayload(ADDRESS, rawCmd);
        AdvertiseData data = new AdvertiseData.Builder()
            .addManufacturerData(0xC200, encrypted)
            .build();
        AdvertiseSettings settings = new AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
            .setConnectable(true)
            .setTimeout(0)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
            .build();

        if (advCallback != null) {
            try { advertiser.stopAdvertising(advCallback); } catch (Exception e) {}
        }
        advCallback = new AdvertiseCallback() {
            public void onStartSuccess(AdvertiseSettings s) {}
            public void onStartFailure(int e) { System.out.println("ADV_FAIL:" + e); }
        };
        advertiser.startAdvertising(settings, data, advCallback);
    }

    // --- HTTP Server ---

    static void handleRequest(Socket client) throws IOException {
        BufferedReader in = new BufferedReader(new InputStreamReader(client.getInputStream()));
        PrintWriter out = new PrintWriter(client.getOutputStream(), true);

        String line = in.readLine();
        if (line == null) { client.close(); return; }

        // Consume headers
        while (true) {
            String h = in.readLine();
            if (h == null || h.isEmpty()) break;
        }

        String response = "OK";
        String path = line.split(" ")[1];

        if (path.equals("/pair")) {
            byte[] v = makeVerify();
            for (int i = 0; i < 30; i++) {
                sendBLE(v);
                try { Thread.sleep(150); } catch (InterruptedException ie) {}
            }
            response = "PAIRED";
        } else if (path.startsWith("/move")) {
            // /move?a=128&b=128&c=0&d=255&fmt=14
            int a=128, b=128, c=128, d=128;
            String fmt = "14";
            String query = path.contains("?") ? path.split("\\?")[1] : "";
            for (String p : query.split("&")) {
                String[] kv = p.split("=");
                if (kv.length == 2) {
                    switch (kv[0]) {
                        case "a": a = Integer.parseInt(kv[1]); break;
                        case "b": b = Integer.parseInt(kv[1]); break;
                        case "c": c = Integer.parseInt(kv[1]); break;
                        case "d": d = Integer.parseInt(kv[1]); break;
                        case "fmt": fmt = kv[1]; break;
                    }
                }
            }
            byte[] cmd = fmt.equals("10") ? makeCmd10(a,b,c,d) : makeCmd14(a,b,c,d);
            sendBLE(cmd);
            response = "MOVE a="+a+" b="+b+" c="+c+" d="+d;
        } else if (path.equals("/stop")) {
            sendBLE(makeCmd14(128,128,128,128));
            response = "STOPPED";
        } else if (path.equals("/status")) {
            response = "RUNNING port=" + PORT + " adv=" + (advertiser != null);
        } else if (path.equals("/quit")) {
            response = "BYE";
            out.print("HTTP/1.1 200 OK\r\nContent-Length: " + response.length() + "\r\n\r\n" + response);
            out.flush();
            client.close();
            System.exit(0);
        }

        out.print("HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: " + response.length() + "\r\n\r\n" + response);
        out.flush();
        client.close();
    }

    public static void main(String[] args) throws Exception {
        System.out.println("BLE Bridge starting on port " + PORT);

        // Create Android system context via reflection (hidden API)
        android.os.Looper.prepareMainLooper();
        Class<?> atClass = Class.forName("android.app.ActivityThread");
        Object at = atClass.getMethod("systemMain").invoke(null);
        Context context = (Context) atClass.getMethod("getSystemContext").invoke(at);
        System.out.println("Got system context: " + context);

        // Init BLE via BluetoothManager
        BluetoothManager btManager = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        System.out.println("BluetoothManager: " + btManager);
        BluetoothAdapter adapter = null;
        if (btManager != null) adapter = btManager.getAdapter();
        System.out.println("Adapter from manager: " + adapter);
        if (adapter == null) {
            adapter = BluetoothAdapter.getDefaultAdapter();
            System.out.println("Adapter from default: " + adapter);
        }
        if (adapter != null) {
            System.out.println("Adapter state: " + adapter.getState() + " enabled: " + adapter.isEnabled());
            System.out.println("Adapter name: " + adapter.getName());
        }
        if (adapter == null) {
            System.out.println("ERROR: Bluetooth adapter is null");
            return;
        }
        if (!adapter.isEnabled()) {
            System.out.println("Bluetooth disabled, enabling...");
            adapter.enable();
            Thread.sleep(2000);
            System.out.println("Adapter enabled: " + adapter.isEnabled());
        }
        advertiser = adapter.getBluetoothLeAdvertiser();
        if (advertiser == null) {
            System.out.println("ERROR: BLE advertising not supported");
            return;
        }
        System.out.println("BLE advertiser ready");

        // Start HTTP server
        ServerSocket server = new ServerSocket(PORT);
        System.out.println("HTTP server listening on port " + PORT);

        while (true) {
            try {
                Socket client = server.accept();
                handleRequest(client);
            } catch (Exception e) {
                System.out.println("Error: " + e.getMessage());
            }
        }
    }
}
