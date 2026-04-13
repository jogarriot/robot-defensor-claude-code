#!/usr/bin/env swift
// ble_advertiser.swift - Raw BLE advertising with custom manufacturer data on macOS
// Uses CoreBluetooth CBPeripheralManager with a workaround for manufacturer data

import Foundation
import CoreBluetooth

class Advertiser: NSObject, CBPeripheralManagerDelegate {
    var peripheralManager: CBPeripheralManager!
    var advertisingData: [UInt8] = []
    var isReady = false
    let readySemaphore = DispatchSemaphore(value: 0)

    override init() {
        super.init()
        peripheralManager = CBPeripheralManager(delegate: self, queue: nil)
    }

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        if peripheral.state == .poweredOn {
            isReady = true
            readySemaphore.signal()
        } else {
            fputs("Bluetooth not ready: \(peripheral.state.rawValue)\n", stderr)
        }
    }

    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        if let error = error {
            fputs("Advertising error: \(error)\n", stderr)
        }
    }

    func waitForReady(timeout: TimeInterval = 10.0) -> Bool {
        let result = readySemaphore.wait(timeout: .now() + timeout)
        if result == .timedOut {
            fputs("State after timeout: \(peripheralManager.state.rawValue)\n", stderr)
        }
        return result == .success
    }

    func startAdvertising(data: [UInt8]) {
        // Stop any existing advertising
        peripheralManager.stopAdvertising()

        // On macOS, we can use CBPeripheralManager with advertisement data
        // The manufacturer data needs to be embedded in the advertising packet
        // We'll use the AppleAdvertisementData approach

        // Build the advertising data dictionary
        // CBAdvertisementDataManufacturerDataKey isn't officially documented for peripheralManager
        // but it works on macOS through the Objective-C bridge
        let mfrData = Data(data)

        // Try using the undocumented manufacturer data key
        let adDict: [String: Any] = [
            "kCBAdvDataManufacturerData": mfrData
        ]

        peripheralManager.startAdvertising(adDict)
    }

    func stopAdvertising() {
        peripheralManager.stopAdvertising()
    }
}

// --- Main ---

let args = CommandLine.arguments

if args.count < 2 {
    print("Usage: ble_advertiser <hex_data> [duration_ms]")
    print("       ble_advertiser stop")
    print("       ble_advertiser scan")
    print("")
    print("Broadcasts BLE advertising with manufacturer-specific data.")
    print("hex_data: hex string of the full advertising payload")
    print("duration_ms: how long to advertise (default: 3000)")
    exit(1)
}

let advertiser = Advertiser()

if !advertiser.waitForReady() {
    fputs("ERROR: Bluetooth not available\n", stderr)
    exit(1)
}

let command = args[1]

if command == "stop" {
    advertiser.stopAdvertising()
    print("Advertising stopped")
    exit(0)
}

if command == "scan" {
    // Just check BT is ready
    print("Bluetooth ready")
    exit(0)
}

// Parse hex data
let hexString = command.replacingOccurrences(of: " ", with: "")
var data: [UInt8] = []
var index = hexString.startIndex
while index < hexString.endIndex {
    let nextIndex = hexString.index(index, offsetBy: 2, limitedBy: hexString.endIndex) ?? hexString.endIndex
    if let byte = UInt8(hexString[index..<nextIndex], radix: 16) {
        data.append(byte)
    }
    index = nextIndex
}

if data.isEmpty {
    fputs("ERROR: No valid hex data\n", stderr)
    exit(1)
}

let duration = args.count > 2 ? (Int(args[2]) ?? 3000) : 3000

// Prepend manufacturer ID (115200 = 0x01C200, but BLE uses 16-bit: 0xC200 little-endian)
var mfrPacket: [UInt8] = [0x00, 0xC2]  // Manufacturer ID 0xC200 in little-endian
mfrPacket.append(contentsOf: data)

advertiser.startAdvertising(data: mfrPacket)
print("Advertising \(data.count) bytes for \(duration)ms")

// Keep alive for duration
Thread.sleep(forTimeInterval: Double(duration) / 1000.0)

advertiser.stopAdvertising()
print("Done")
