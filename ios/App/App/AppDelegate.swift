import UIKit
import Capacitor
import CoreBluetooth
import WebKit

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // Override point for customization after application launch.
        return true
    }

    func applicationWillResignActive(_ application: UIApplication) {
        // Sent when the application is about to move from active to inactive state. This can occur for certain types of temporary interruptions (such as an incoming phone call or SMS message) or when the user quits the application and it begins the transition to the background state.
        // Use this method to pause ongoing tasks, disable timers, and invalidate graphics rendering callbacks. Games should use this method to pause the game.
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // Use this method to release shared resources, save user data, invalidate timers, and store enough application state information to restore your application to its current state in case it is terminated later.
        // If your application supports background execution, this method is called instead of applicationWillTerminate: when the user quits.
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        // Called as part of the transition from the background to the active state; here you can undo many of the changes made on entering the background.
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        // Restart any tasks that were paused (or not yet started) while the application was inactive. If the application was previously in the background, optionally refresh the user interface.
    }

    func applicationWillTerminate(_ application: UIApplication) {
        // Called when the application is about to terminate. Save data if appropriate. See also applicationDidEnterBackground:.
    }

    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        // Called when the app was launched with a url. Feel free to add additional processing here,
        // but if you want the App API to support tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(app, open: url, options: options)
    }

    func application(_ application: UIApplication, continue userActivity: NSUserActivity, restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void) -> Bool {
        // Called when the app was launched with an activity, including Universal Links.
        // Feel free to add additional processing here, but if you want the App API to support
        // tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(application, continue: userActivity, restorationHandler: restorationHandler)
    }

}

@objc(ZebBridgeViewController)
class ZebBridgeViewController: CAPBridgeViewController, WKScriptMessageHandler {
    private let bridgeMessageHandlerName = "zebBluetoothBridge"
    private lazy var bluetoothBridge = OBDLinkMXNativeBridge(eventSink: self)

    public override func webViewConfiguration(for instanceConfiguration: InstanceConfiguration) -> WKWebViewConfiguration {
        let configuration = super.webViewConfiguration(for: instanceConfiguration)
        configuration.userContentController.add(self, name: bridgeMessageHandlerName)
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: bootstrapScript(),
                injectionTime: .atDocumentStart,
                forMainFrameOnly: false
            )
        )
        return configuration
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        loadConfiguredDashboardIfNeeded()
    }

    deinit {
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: bridgeMessageHandlerName)
        bluetoothBridge.stop()
    }

    public func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == bridgeMessageHandlerName,
              let body = message.body as? [String: Any],
              let requestID = body["id"] as? String,
              let method = body["method"] as? String else {
            return
        }

        let args = body["args"] as? [String: Any] ?? [:]

        switch method {
        case "connectToAdapter":
            bluetoothBridge.connectToAdapter { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        case "disconnectAdapter":
            bluetoothBridge.disconnectAdapter { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        case "getConnectionState":
            bluetoothBridge.getConnectionState { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        case "requestBluetoothPermission":
            bluetoothBridge.requestBluetoothPermission { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        case "readPid":
            bluetoothBridge.readPid(args["pid"] as? String ?? "") { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        case "readVin":
            bluetoothBridge.readVin { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        case "startPolling":
            bluetoothBridge.startPolling(arguments: args) { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        case "stopPolling":
            bluetoothBridge.stopPolling { [weak self] result in
                self?.reply(to: requestID, with: result)
            }
        default:
            reject(
                requestID: requestID,
                payload: [
                    "code": "unsupported-native-method",
                    "message": "Unsupported native bridge method: \(method)"
                ]
            )
        }
    }

    private func loadConfiguredDashboardIfNeeded() {
        guard let dashboardURL = configuredDashboardURL() else { return }
        guard webView?.url?.absoluteString != dashboardURL.absoluteString else { return }
        DispatchQueue.main.async { [weak self] in
            self?.webView?.load(URLRequest(url: dashboardURL))
        }
    }

    private func configuredDashboardURL() -> URL? {
        // If provided via Info.plist, prefer that; otherwise fall back to default
        let defaultBase = "https://codexapp-j7jw.onrender.com"

        let rawURL = (Bundle.main.object(forInfoDictionaryKey: "ZebDashboardURL") as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)

        // No value in Info.plist: use default base + /dashboard
        if rawURL == nil || rawURL!.isEmpty {
            guard let base = URL(string: defaultBase) else { return nil }
            if base.path.isEmpty || base.path == "/" {
                return base.appendingPathComponent("dashboard")
            }
            return base
        }

        // Value provided in Info.plist
        guard let url = URL(string: rawURL!) else { return nil }
        if url.path.isEmpty || url.path == "/" {
            return url.appendingPathComponent("dashboard")
        }
        return url
    }

    private func bootstrapScript() -> String {
        let runtimeConfig = [
            "dashboardUrl": configuredDashboardURL()?.absoluteString ?? ""
        ]

        return """
        (function() {
          window.ZebRuntimeConfig = \(jsonObjectLiteral(runtimeConfig));
          const bridgeHandlerName = \(jsonStringLiteral(bridgeMessageHandlerName));
          const pending = new Map();
          let nextRequestId = 1;

          function toBridgeError(payload) {
            const detail = payload && typeof payload === 'object' ? payload : {};
            const error = new Error(detail.message || detail.code || 'native-bridge-error');
            Object.assign(error, detail);
            return error;
          }

          function invoke(method, args) {
            return new Promise((resolve, reject) => {
              const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers[bridgeHandlerName];
              if (!handler) {
                reject(toBridgeError({ code: 'native-bridge-unavailable', message: 'Native bridge unavailable.' }));
                return;
              }

              const id = String(nextRequestId++);
              pending.set(id, { resolve, reject });
              handler.postMessage({ id, method, args: args || {} });
            });
          }

          window.__zebNativeBridge = window.__zebNativeBridge || {};
          window.__zebNativeBridge.resolve = function(id, payload) {
            const entry = pending.get(String(id));
            if (!entry) { return; }
            pending.delete(String(id));
            entry.resolve(payload || {});
          };
          window.__zebNativeBridge.reject = function(id, payload) {
            const entry = pending.get(String(id));
            if (!entry) { return; }
            pending.delete(String(id));
            entry.reject(toBridgeError(payload));
          };
          window.__zebNativeBridge.dispatchNativeEvent = function(name, payload) {
            window.dispatchEvent(new CustomEvent(name, { detail: payload || {} }));
          };

          if (!window.MobileBluetoothService || window.MobileBluetoothService.__zebNative !== true) {
            window.MobileBluetoothService = {
              __zebNative: true,
              connectToAdapter() { return invoke('connectToAdapter'); },
              disconnectAdapter() { return invoke('disconnectAdapter'); },
              getConnectionState() { return invoke('getConnectionState'); },
              requestBluetoothPermission() { return invoke('requestBluetoothPermission'); },
              readPid(pid) { return invoke('readPid', { pid }); },
              readVin() { return invoke('readVin'); },
              startPolling(options) { return invoke('startPolling', options || {}); },
              stopPolling() { return invoke('stopPolling'); }
            };
          }
        })();
        """
    }

    private func reply(to requestID: String, with result: Result<[String: Any], NativeBridgeError>) {
        switch result {
        case .success(let payload):
            resolve(requestID: requestID, payload: payload)
        case .failure(let error):
            reject(requestID: requestID, payload: error.payload)
        }
    }

    private func resolve(requestID: String, payload: [String: Any]) {
        evaluateBridgeScript(
            "window.__zebNativeBridge && window.__zebNativeBridge.resolve(\(jsonStringLiteral(requestID)), \(jsonObjectLiteral(payload)));"
        )
    }

    private func reject(requestID: String, payload: [String: Any]) {
        evaluateBridgeScript(
            "window.__zebNativeBridge && window.__zebNativeBridge.reject(\(jsonStringLiteral(requestID)), \(jsonObjectLiteral(payload)));"
        )
    }

    private func evaluateBridgeScript(_ script: String) {
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(script, completionHandler: nil)
        }
    }

    private func jsonObjectLiteral(_ object: [String: Any]) -> String {
        guard JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object, options: []),
              let string = String(data: data, encoding: .utf8) else {
            return "{}"
        }
        return string
    }

    private func jsonStringLiteral(_ value: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [value], options: []),
              let string = String(data: data, encoding: .utf8) else {
            return "\"\""
        }
        return String(string.dropFirst().dropLast())
    }
}

extension ZebBridgeViewController: OBDLinkMXNativeBridgeEventSink {
    func nativeBridgeDidDispatchEvent(name: String, payload: [String: Any]) {
        evaluateBridgeScript(
            "window.__zebNativeBridge && window.__zebNativeBridge.dispatchNativeEvent(\(jsonStringLiteral(name)), \(jsonObjectLiteral(payload)));"
        )
    }
}

protocol OBDLinkMXNativeBridgeEventSink: AnyObject {
    func nativeBridgeDidDispatchEvent(name: String, payload: [String: Any])
}

struct NativeBridgeError: Error {
    let code: String
    let message: String
    let permissionState: String?

    var payload: [String: Any] {
        var result: [String: Any] = [
            "code": code,
            "message": message
        ]
        if let permissionState {
            result["permission_state"] = permissionState
        }
        return result
    }
}

final class OBDLinkMXNativeBridge: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    private struct CommandReply {
        let raw: String
        let latencyMs: Int
    }

    private struct QueuedCommand {
        let command: String
        let timeout: TimeInterval
        let completion: (Result<CommandReply, NativeBridgeError>) -> Void
    }

    private struct ActiveCommand {
        let request: QueuedCommand
        let startedAt: Date
        let timeoutWorkItem: DispatchWorkItem
    }

    private enum EventName {
        static let disconnected = "zeb-native-disconnected"
        static let poll = "zeb-native-poll"
    }

    typealias BridgeCompletion = (Result<[String: Any], NativeBridgeError>) -> Void

    private static let serviceUUID = CBUUID(string: "FFF0")
    private static let notifyUUID = CBUUID(string: "FFF1")
    private static let writeUUID = CBUUID(string: "FFF2")
    private static let adapterSetupCommands = ["ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP3"]
    private static let defaultPollingPids = ["010C", "0105", "0142", "010D"]
    private static let pidMetadata: [String: (key: String, unit: String?)] = [
        "0105": ("coolant_temp", "°C"),
        "010C": ("rpm", "rpm"),
        "010D": ("vehicle_speed", "km/h"),
        "010F": ("intake_air_temp", "°C"),
        "0111": ("throttle_position", "%"),
        "012F": ("fuel_level", "%"),
        "0142": ("control_module_voltage", "V"),
        "0902": ("vin", nil)
    ]
    private static let pidPrefixes: [String: [String]] = [
        "0105": ["41", "05"],
        "010C": ["41", "0C"],
        "010D": ["41", "0D"],
        "010F": ["41", "0F"],
        "0111": ["41", "11"],
        "012F": ["41", "2F"],
        "0142": ["41", "42"],
        "0902": ["49", "02", "01"]
    ]
    private static let timestampFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private let queue = DispatchQueue(label: "com.zebs.obdai.bluetooth")
    private weak var eventSink: OBDLinkMXNativeBridgeEventSink?
    private lazy var centralManager: CBCentralManager = CBCentralManager(delegate: self, queue: queue)

    private var didCreateCentralManager = false
    private var pendingStateWaiters: [() -> Void] = []
    private var connectWaiters: [BridgeCompletion] = []
    private var peripheral: CBPeripheral?
    private var notifyCharacteristic: CBCharacteristic?
    private var writeCharacteristic: CBCharacteristic?
    private var connectionStatus = "disconnected"
    private var adapterName = "OBDLink MX+"
    private var lastError: String?
    private var responseBuffer = ""
    private var scanTimeoutWorkItem: DispatchWorkItem?
    private var adapterInitialized = false
    private var adapterInitializationInProgress = false
    private var suppressDisconnectEvent = false
    private var queuedCommands: [QueuedCommand] = []
    private var activeCommand: ActiveCommand?
    private var pollingTimer: DispatchSourceTimer?
    private var pollingPids: [String] = []
    private var pollingIntervalMs = 500
    private var pollingTickInFlight = false

    init(eventSink: OBDLinkMXNativeBridgeEventSink) {
        self.eventSink = eventSink
        super.init()
        ensureCentralManager()
    }

    func stop() {
        queue.async {
            self.stopPollingInternal()
            self.cancelScan()
            if let peripheral = self.peripheral, peripheral.state != .disconnected {
                self.suppressDisconnectEvent = true
                self.centralManager.cancelPeripheralConnection(peripheral)
            }
            self.clearTransportState(status: "disconnected", error: nil)
        }
    }

    func requestBluetoothPermission(completion: @escaping BridgeCompletion) {
        queue.async {
            self.whenBluetoothStateKnown {
                completion(.success(self.connectionPayload()))
            }
        }
    }

    func getConnectionState(completion: @escaping BridgeCompletion) {
        queue.async {
            completion(.success(self.connectionPayload()))
        }
    }

    func connectToAdapter(completion: @escaping BridgeCompletion) {
        queue.async {
            self.connectWaiters.append(completion)
            self.whenBluetoothStateKnown {
                self.beginConnectionIfPossible()
            }
        }
    }

    func disconnectAdapter(completion: @escaping BridgeCompletion) {
        queue.async {
            self.stopPollingInternal()
            self.cancelScan()
            self.connectionStatus = "disconnected"
            self.lastError = nil

            let disconnectError = self.error(
                code: "native-bridge-disconnected",
                message: "Bluetooth disconnected."
            )
            self.failQueuedCommands(disconnectError)
            self.connectWaiters.removeAll()

            if let peripheral = self.peripheral, peripheral.state != .disconnected {
                self.suppressDisconnectEvent = true
                self.centralManager.cancelPeripheralConnection(peripheral)
            }

            self.clearTransportState(status: "disconnected", error: nil)
            completion(.success(self.connectionPayload(status: "disconnected")))
        }
    }

    func readPid(_ pid: String, completion: @escaping BridgeCompletion) {
        queue.async {
            let normalizedPID = self.normalizeCommand(pid)
            guard !normalizedPID.isEmpty else {
                completion(.failure(self.error(code: "invalid-pid", message: "PID is required.")))
                return
            }
            guard self.connectionStatus == "connected", self.adapterInitialized else {
                completion(.failure(self.error(code: "native-bridge-not-connected", message: "OBDLink MX+ is not connected.")))
                return
            }

            self.enqueueCommand(command: normalizedPID, timeout: self.timeout(for: normalizedPID)) { result in
                switch result {
                case .success(let reply):
                    completion(.success(self.readPayload(command: normalizedPID, reply: reply)))
                case .failure(let error):
                    completion(.failure(error))
                }
            }
        }
    }

    func readVin(completion: @escaping BridgeCompletion) {
        readPid("0902", completion: completion)
    }

    func startPolling(arguments: [String: Any], completion: @escaping BridgeCompletion) {
        queue.async {
            guard self.connectionStatus == "connected", self.adapterInitialized else {
                completion(.failure(self.error(code: "native-bridge-not-connected", message: "OBDLink MX+ is not connected.")))
                return
            }

            let requestedPids = (arguments["pids"] as? [String])?
                .map(self.normalizeCommand(_:))
                .filter { !$0.isEmpty } ?? Self.defaultPollingPids
            let requestedInterval = (arguments["intervalMs"] as? NSNumber)?.intValue
                ?? (arguments["interval_ms"] as? NSNumber)?.intValue
                ?? 500

            self.pollingPids = requestedPids.isEmpty ? Self.defaultPollingPids : requestedPids
            self.pollingIntervalMs = max(requestedInterval, 250)
            self.startPollingInternal()

            completion(.success([
                "status": "active",
                "polling_state": "active",
                "interval_ms": self.pollingIntervalMs,
                "pids": self.pollingPids
            ]))
        }
    }

    func stopPolling(completion: @escaping BridgeCompletion) {
        queue.async {
            self.stopPollingInternal()
            completion(.success([
                "status": "inactive",
                "polling_state": "inactive"
            ]))
        }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let waiters = pendingStateWaiters
        pendingStateWaiters.removeAll()
        waiters.forEach { $0() }

        if central.state == .poweredOn {
            return
        }

        if connectionStatus == "connecting" {
            failConnectWaiters(error(for: central.state))
            return
        }

        guard connectionStatus == "connected" else {
            return
        }

        handleUnexpectedDisconnect(reasonCode: error(for: central.state).code)
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let localName = advertisementData[CBAdvertisementDataLocalNameKey] as? String
        let candidateName = peripheral.name ?? localName ?? ""
        guard candidateName.uppercased().hasPrefix("OBDLINK") else {
            return
        }

        cancelScan()
        adapterName = candidateName
        self.peripheral = peripheral
        peripheral.delegate = self
        centralManager.connect(peripheral, options: nil)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        responseBuffer = ""
        peripheral.discoverServices([Self.serviceUUID])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        failConnectWaiters(
            self.error(
                code: "adapter-connect-failed",
                message: error?.localizedDescription ?? "Unable to connect to OBDLink MX+."
            )
        )
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        let reasonCode = (error?.localizedDescription).map { "adapter-disconnected-\($0)" } ?? "adapter-disconnected"
        let shouldEmitEvent = !suppressDisconnectEvent
        suppressDisconnectEvent = false

        let disconnectError = self.error(code: reasonCode, message: error?.localizedDescription ?? "OBDLink MX+ disconnected.")
        failQueuedCommands(disconnectError)
        clearTransportState(status: "disconnected", error: shouldEmitEvent ? reasonCode : lastError)

        if shouldEmitEvent {
            eventSink?.nativeBridgeDidDispatchEvent(
                name: EventName.disconnected,
                payload: connectionPayload(status: "disconnected", fallbackReason: reasonCode)
            )
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error {
            failConnectWaiters(self.error(code: "service-discovery-failed", message: error.localizedDescription))
            return
        }

        guard let service = peripheral.services?.first(where: { $0.uuid == Self.serviceUUID }) else {
            failConnectWaiters(self.error(code: "obdlink-service-missing", message: "OBDLink BLE UART service not found."))
            return
        }

        peripheral.discoverCharacteristics([Self.notifyUUID, Self.writeUUID], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error {
            failConnectWaiters(self.error(code: "characteristic-discovery-failed", message: error.localizedDescription))
            return
        }

        service.characteristics?.forEach { characteristic in
            if characteristic.uuid == Self.notifyUUID {
                notifyCharacteristic = characteristic
                peripheral.setNotifyValue(true, for: characteristic)
            } else if characteristic.uuid == Self.writeUUID {
                writeCharacteristic = characteristic
            }
        }

        initializeAdapterIfReady()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            failConnectWaiters(self.error(code: "notification-start-failed", message: error.localizedDescription))
            return
        }

        initializeAdapterIfReady()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            completeActiveCommand(.failure(self.error(code: "read-response-failed", message: error.localizedDescription)))
            return
        }

        guard characteristic.uuid == Self.notifyUUID, let data = characteristic.value else {
            return
        }

        responseBuffer += String(decoding: data, as: UTF8.self)
        guard responseBuffer.contains(">") else {
            return
        }

        completeActiveCommand(.success(buildCommandReply(from: responseBuffer)))
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            completeActiveCommand(.failure(self.error(code: "write-command-failed", message: error.localizedDescription)))
        }
    }

    private func ensureCentralManager() {
        guard !didCreateCentralManager else { return }
        _ = centralManager
        didCreateCentralManager = true
    }

    private func whenBluetoothStateKnown(_ work: @escaping () -> Void) {
        ensureCentralManager()
        switch centralManager.state {
        case .unknown, .resetting:
            pendingStateWaiters.append(work)
        default:
            work()
        }
    }

    private func beginConnectionIfPossible() {
        switch centralManager.state {
        case .poweredOn:
            break
        default:
            failConnectWaiters(error(for: centralManager.state))
            return
        }

        if connectionStatus == "connected", adapterInitialized {
            resolveConnectWaiters(payload: connectionPayload(status: "connected"))
            return
        }

        guard connectionStatus != "connecting" else {
            return
        }

        connectionStatus = "connecting"
        lastError = nil
        adapterInitialized = false
        adapterInitializationInProgress = false
        clearTransportState(status: "connecting", error: nil)
        cancelScan()
        stopPollingInternal()
        clearPendingCommandsWithoutFailure()

        let timeoutWorkItem = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.failConnectWaiters(
                self.error(code: "adapter-not-found", message: "OBDLink MX+ was not discovered before the scan timed out.")
            )
        }
        scanTimeoutWorkItem = timeoutWorkItem
        queue.asyncAfter(deadline: .now() + .seconds(12), execute: timeoutWorkItem)

        centralManager.scanForPeripherals(
            withServices: nil,
            options: [CBCentralManagerScanOptionAllowDuplicatesKey: false]
        )
    }

    private func initializeAdapterIfReady() {
        guard connectionStatus == "connecting",
              let peripheral,
              let notifyCharacteristic,
              let _ = writeCharacteristic,
              notifyCharacteristic.isNotifying,
              !adapterInitialized,
              !adapterInitializationInProgress else {
            return
        }

        adapterInitializationInProgress = true
        runCommandSequence(Self.adapterSetupCommands, index: 0) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success:
                self.adapterInitializationInProgress = false
                self.adapterInitialized = true
                self.connectionStatus = "connected"
                self.lastError = nil
                self.resolveConnectWaiters(payload: self.connectionPayload(status: "connected"))
                peripheral.delegate = self
            case .failure(let error):
                self.adapterInitializationInProgress = false
                self.failConnectWaiters(error)
            }
        }
    }

    private func runCommandSequence(_ commands: [String], index: Int, completion: @escaping (Result<Void, NativeBridgeError>) -> Void) {
        guard index < commands.count else {
            completion(.success(()))
            return
        }

        let command = commands[index]
        enqueueCommand(command: command, timeout: timeout(for: command)) { [weak self] result in
            switch result {
            case .success:
                self?.runCommandSequence(commands, index: index + 1, completion: completion)
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    private func enqueueCommand(command: String, timeout: TimeInterval, completion: @escaping (Result<CommandReply, NativeBridgeError>) -> Void) {
        let request = QueuedCommand(command: command, timeout: timeout, completion: completion)
        queuedCommands.append(request)
        processNextCommandIfPossible()
    }

    private func processNextCommandIfPossible() {
        guard activeCommand == nil else { return }
        guard let peripheral, let writeCharacteristic else { return }
        guard !queuedCommands.isEmpty else { return }

        let request = queuedCommands.removeFirst()
        responseBuffer = ""

        let timeoutWorkItem = DispatchWorkItem { [weak self] in
            self?.completeActiveCommand(
                .failure(
                    self?.error(
                        code: "command-timeout-\(request.command.lowercased())",
                        message: "Timed out waiting for \(request.command) response."
                    ) ?? NativeBridgeError(code: "command-timeout", message: "Timed out waiting for OBD response.", permissionState: nil)
                )
            )
        }

        activeCommand = ActiveCommand(request: request, startedAt: Date(), timeoutWorkItem: timeoutWorkItem)
        queue.asyncAfter(deadline: .now() + request.timeout, execute: timeoutWorkItem)

        guard let payload = "\(request.command)\r".data(using: .ascii) else {
            completeActiveCommand(.failure(error(code: "command-encode-failed", message: "Unable to encode OBD command.")))
            return
        }

        let writeType: CBCharacteristicWriteType = writeCharacteristic.properties.contains(.writeWithoutResponse)
            ? .withoutResponse
            : .withResponse
        peripheral.writeValue(payload, for: writeCharacteristic, type: writeType)
    }

    private func completeActiveCommand(_ result: Result<CommandReply, NativeBridgeError>) {
        guard let activeCommand else { return }

        self.activeCommand = nil
        activeCommand.timeoutWorkItem.cancel()
        let completion = activeCommand.request.completion
        responseBuffer = ""
        completion(result)
        processNextCommandIfPossible()
    }

    private func buildCommandReply(from rawResponse: String) -> CommandReply {
        let latencyMs = max(Int(Date().timeIntervalSince(activeCommand?.startedAt ?? Date()) * 1000), 0)
        return CommandReply(raw: rawResponse, latencyMs: latencyMs)
    }

    private func readPayload(command: String, reply: CommandReply) -> [String: Any] {
        let cleaned = cleanResponse(reply.raw)
        let timestamp = Self.timestampFormatter.string(from: Date())
        var payload: [String: Any] = [
            "command": command,
            "raw_response": cleaned,
            "source_mode": "PHONE-LIVE",
            "source_hint": "iso9141_2",
            "backend_status": "accepted",
            "latency_ms": reply.latencyMs,
            "ts": timestamp
        ]

        if let metadata = Self.pidMetadata[command] {
            payload["pid_key"] = metadata.key
            if let unit = metadata.unit {
                payload["unit"] = unit
            }
        }

        if let value = parsedValue(for: command, cleanedResponse: cleaned) {
            payload["value"] = value
        }

        return payload
    }

    private func parsedValue(for command: String, cleanedResponse: String) -> Any? {
        let bytes = extractResponseBytes(command: command, cleanedResponse: cleanedResponse).compactMap { Int($0, radix: 16) }

        switch command {
        case "010C":
            guard bytes.count >= 2 else { return nil }
            return ((bytes[0] * 256) + bytes[1]) / 4
        case "0105", "010F":
            guard let first = bytes.first else { return nil }
            return first - 40
        case "010D":
            return bytes.first
        case "0111", "012F":
            guard let first = bytes.first else { return nil }
            return Double(round((Double(first) * 1000.0 / 255.0) / 10.0))
        case "0142":
            guard bytes.count >= 2 else { return nil }
            return Double((bytes[0] * 256) + bytes[1]) / 1000.0
        case "0902":
            return decodeVIN(cleanedResponse)
        default:
            return nil
        }
    }

    private func extractResponseBytes(command: String, cleanedResponse: String) -> [String] {
        guard let prefix = Self.pidPrefixes[command] else { return [] }
        let tokens = cleanedResponse
            .uppercased()
            .split(separator: " ")
            .map(String.init)
            .filter { $0.range(of: #"^[0-9A-F]{2}$"#, options: .regularExpression) != nil }

        guard let startIndex = tokens.indices.first(where: { index in
            guard index + prefix.count <= tokens.count else { return false }
            return Array(tokens[index..<(index + prefix.count)]) == prefix
        }) else {
            return []
        }

        return Array(tokens[(startIndex + prefix.count)...])
    }

    private func decodeVIN(_ cleanedResponse: String) -> String? {
        let cleaned = cleanedResponse
            .replacingOccurrences(of: " ", with: "")
            .uppercased()
        guard let range = cleaned.range(of: "490201") else { return nil }
        let hexPart = String(cleaned[range.upperBound...]).filter { "0123456789ABCDEF".contains($0) }
        guard hexPart.count >= 34 else { return nil }

        var vin = ""
        var index = hexPart.startIndex
        while index < hexPart.endIndex && vin.count < 17 {
            let next = hexPart.index(index, offsetBy: 2, limitedBy: hexPart.endIndex) ?? hexPart.endIndex
            let pair = String(hexPart[index..<next])
            guard pair.count == 2, let value = UInt8(pair, radix: 16) else { break }
            if value >= 32 && value <= 126, let scalar = UnicodeScalar(Int(value)) {
                vin.append(Character(scalar))
            }
            index = next
        }

        return vin.count == 17 ? vin : nil
    }

    private func cleanResponse(_ rawResponse: String) -> String {
        rawResponse
            .replacingOccurrences(of: "\u{0}", with: "")
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: ">", with: " ")
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func normalizeCommand(_ command: String) -> String {
        command
            .uppercased()
            .replacingOccurrences(of: " ", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func timeout(for command: String) -> TimeInterval {
        if command == "ATZ" {
            return 5.0
        }
        if command == "0902" {
            return 4.5
        }
        return 3.5
    }

    private func startPollingInternal() {
        stopPollingInternal()

        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(pollingIntervalMs))
        timer.setEventHandler { [weak self] in
            self?.pollIfNeeded()
        }
        pollingTimer = timer
        timer.resume()
    }

    private func stopPollingInternal() {
        pollingTickInFlight = false
        pollingTimer?.cancel()
        pollingTimer = nil
    }

    private func pollIfNeeded() {
        guard connectionStatus == "connected", adapterInitialized, !pollingTickInFlight else {
            return
        }

        pollingTickInFlight = true
        pollNext(index: 0)
    }

    private func pollNext(index: Int) {
        guard index < pollingPids.count else {
            pollingTickInFlight = false
            return
        }

        let pid = pollingPids[index]
        enqueueCommand(command: pid, timeout: timeout(for: pid)) { [weak self] result in
            guard let self else { return }

            switch result {
            case .success(let reply):
                let payload = self.readPayload(command: pid, reply: reply)
                self.eventSink?.nativeBridgeDidDispatchEvent(name: EventName.poll, payload: payload)
            case .failure(let error):
                self.lastError = error.code
            }

            self.pollNext(index: index + 1)
        }
    }

    private func connectionPayload(status: String? = nil, fallbackReason: String? = nil) -> [String: Any] {
        var payload: [String: Any] = [
            "platform": "ios",
            "adapter_name": adapterName,
            "status": status ?? connectionStatus,
            "permission_state": permissionStateString(),
            "source_mode": "PHONE-LIVE",
            "supports_native_bluetooth": true,
            "polling_state": pollingTimer == nil ? "inactive" : "active"
        ]
        if let fallbackReason {
            payload["fallback_reason"] = fallbackReason
        }
        if let lastError {
            payload["last_error"] = lastError
        }
        return payload
    }

    private func permissionStateString() -> String {
        ensureCentralManager()
        switch centralManager.state {
        case .poweredOn:
            return "granted"
        case .unauthorized:
            return "denied"
        case .poweredOff:
            return "powered-off"
        case .unsupported:
            return "unsupported"
        case .resetting:
            return "resetting"
        case .unknown:
            return "prompt"
        @unknown default:
            return "unknown"
        }
    }

    private func error(code: String, message: String) -> NativeBridgeError {
        NativeBridgeError(code: code, message: message, permissionState: permissionStateString())
    }

    private func error(for state: CBManagerState) -> NativeBridgeError {
        switch state {
        case .poweredOff:
            return error(code: "bluetooth-powered-off", message: "Bluetooth is turned off on this iPhone.")
        case .unauthorized:
            return error(code: "bluetooth-permission-denied", message: "Bluetooth permission is denied for this app.")
        case .unsupported:
            return error(code: "bluetooth-unsupported", message: "CoreBluetooth is unavailable on this device.")
        case .resetting:
            return error(code: "bluetooth-resetting", message: "Bluetooth is resetting. Try again in a moment.")
        case .unknown:
            return error(code: "bluetooth-permission-pending", message: "Bluetooth permission is still being determined.")
        case .poweredOn:
            return error(code: "bluetooth-powered-on", message: "Bluetooth is available.")
        @unknown default:
            return error(code: "bluetooth-unknown", message: "Bluetooth is unavailable.")
        }
    }

    private func resolveConnectWaiters(payload: [String: Any]) {
        let waiters = connectWaiters
        connectWaiters.removeAll()
        waiters.forEach { $0(.success(payload)) }
    }

    private func failConnectWaiters(_ bridgeError: NativeBridgeError) {
        cancelScan()
        lastError = bridgeError.code
        connectionStatus = "failed"
        failQueuedCommands(bridgeError)

        if let peripheral, peripheral.state != .disconnected {
            suppressDisconnectEvent = true
            centralManager.cancelPeripheralConnection(peripheral)
        }

        clearTransportState(status: "failed", error: bridgeError.code)

        let waiters = connectWaiters
        connectWaiters.removeAll()
        waiters.forEach { $0(.failure(bridgeError)) }
    }

    private func handleUnexpectedDisconnect(reasonCode: String) {
        let bridgeError = error(code: reasonCode, message: "OBDLink MX+ disconnected.")
        failQueuedCommands(bridgeError)
        clearTransportState(status: "disconnected", error: reasonCode)
        eventSink?.nativeBridgeDidDispatchEvent(
            name: EventName.disconnected,
            payload: connectionPayload(status: "disconnected", fallbackReason: reasonCode)
        )
    }

    private func clearTransportState(status: String, error: String?) {
        connectionStatus = status
        lastError = error
        responseBuffer = ""
        peripheral = nil
        notifyCharacteristic = nil
        writeCharacteristic = nil
        adapterInitialized = false
        adapterInitializationInProgress = false
        stopPollingInternal()
    }

    private func clearPendingCommandsWithoutFailure() {
        if let activeCommand {
            activeCommand.timeoutWorkItem.cancel()
        }
        activeCommand = nil
        queuedCommands.removeAll()
        responseBuffer = ""
    }

    private func failQueuedCommands(_ bridgeError: NativeBridgeError) {
        if let activeCommand {
            activeCommand.timeoutWorkItem.cancel()
            let completion = activeCommand.request.completion
            self.activeCommand = nil
            completion(.failure(bridgeError))
        }

        let pending = queuedCommands
        queuedCommands.removeAll()
        pending.forEach { $0.completion(.failure(bridgeError)) }
        responseBuffer = ""
    }

    private func cancelScan() {
        scanTimeoutWorkItem?.cancel()
        scanTimeoutWorkItem = nil
        centralManager.stopScan()
    }
}

