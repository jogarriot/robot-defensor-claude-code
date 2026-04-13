package com.qunyu.method;

public class BLEUtil {
    public static native void QY_rf_payload(byte[] address, int addrLen, byte[] data, int dataLen, byte[] result);
}
