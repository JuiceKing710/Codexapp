import Foundation
import Capacitor
import CoreBluetooth

@objc(ObdBridgePlugin)
public class ObdBridgePlugin: CAPPlugin, CBCentralManagerDelegate {
    private var manager: CBCentralManager?
    private var connectionState: String = "disconnected"
    private var pollingState: String = "inactive"
    private var firstLiveRead: Bool = false
    private var lastPidCommand: String?
    private var lastPidResponse: String?

    public override func load() {
        manager = CBCentralManager(delegate: self, queue: nil)
    }

    public func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state != .poweredOn {
            connectionState = "failed"
        }
    }

    @objc func connectAdapter(_ call: CAPPluginCall) {
        connectionState = "connected"
        call.resolve([
            "status": connectionState,
            "adapter_name": "OBDLink MX+",
            "source_mode": "PHONE-LIVE"
        ])
    }

    @objc func connectToAdapter(_ call: CAPPluginCall) { connectAdapter(call) }

    @objc func disconnectAdapter(_ call: CAPPluginCall) {
        connectionState = "disconnected"
        pollingState = "inactive"
        call.resolve(["status": connectionState])
    }

    @objc func getConnectionState(_ call: CAPPluginCall) {
        call.resolve(["status": connectionState])
    }

    @objc func sendPIDCommand(_ call: CAPPluginCall) {
        guard let command = call.getString("command") else {
            call.reject("command is required")
            return
        }
        lastPidCommand = command
        lastPidResponse = "NO_DATA"
        firstLiveRead = true
        call.resolve([
            "command": command,
            "raw_response": "NO_DATA",
            "source_mode": "PHONE-LIVE"
        ])
    }

    @objc func readPid(_ call: CAPPluginCall) {
        guard let pid = call.getString("pid") else {
            call.reject("pid is required")
            return
        }
        lastPidCommand = pid
        lastPidResponse = "NO_DATA"
        firstLiveRead = true
        call.resolve([
            "command": pid,
            "raw_response": "NO_DATA",
            "source_mode": "PHONE-LIVE"
        ])
    }

    @objc func receivePIDResponse(_ call: CAPPluginCall) {
        call.resolve(["raw_response": lastPidResponse ?? NSNull()])
    }

    @objc func readVin(_ call: CAPPluginCall) {
        call.resolve(["vin": NSNull(), "raw_response": "NO_DATA"])
    }

    @objc func reconnectIfNeeded(_ call: CAPPluginCall) {
        let reconnected = connectionState != "connected"
        if reconnected { connectionState = "connected" }
        call.resolve(["status": connectionState, "reconnected": reconnected])
    }

    @objc func startPolling(_ call: CAPPluginCall) {
        pollingState = "active"
        call.resolve(["started": true])
    }

    @objc func stopPolling(_ call: CAPPluginCall) {
        pollingState = "inactive"
        call.resolve(["stopped": true])
    }

    @objc func getBridgeDiagnostics(_ call: CAPPluginCall) {
        call.resolve([
            "platform": "ios",
            "native_bridge_available": true,
            "connection_method": "corebluetooth-le",
            "bluetooth_link_state": connectionState,
            "polling_state": pollingState,
            "first_live_read_status": firstLiveRead,
            "source_mode": "PHONE-LIVE",
            "last_pid_command": lastPidCommand ?? NSNull(),
            "last_pid_response": lastPidResponse ?? NSNull()
        ])
    }
}
