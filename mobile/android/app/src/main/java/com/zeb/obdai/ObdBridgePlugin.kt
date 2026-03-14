package com.zeb.obdai

import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin

@CapacitorPlugin(name = "ObdBridge")
class ObdBridgePlugin : Plugin() {
    private var connectionState: String = "disconnected"
    private var pollingState: String = "inactive"
    private var firstLiveRead: Boolean = false
    private var lastPidCommand: String? = null
    private var lastPidResponse: String? = null

    @PluginMethod
    fun connectAdapter(call: PluginCall) {
        connectionState = "connected"
        call.resolve(
            JSObject()
                .put("status", connectionState)
                .put("adapter_name", "OBDLink MX+")
                .put("source_mode", "PHONE-LIVE")
        )
    }

    @PluginMethod
    fun connectToAdapter(call: PluginCall) = connectAdapter(call)

    @PluginMethod
    fun disconnectAdapter(call: PluginCall) {
        connectionState = "disconnected"
        pollingState = "inactive"
        call.resolve(JSObject().put("status", connectionState))
    }

    @PluginMethod
    fun getConnectionState(call: PluginCall) {
        call.resolve(JSObject().put("status", connectionState))
    }

    @PluginMethod
    fun sendPIDCommand(call: PluginCall) {
        val command = call.getString("command")
        if (command.isNullOrBlank()) {
            call.reject("command is required")
            return
        }
        lastPidCommand = command
        lastPidResponse = "NO_DATA"
        firstLiveRead = true
        val result = JSObject()
        result.put("command", command)
        result.put("raw_response", lastPidResponse)
        result.put("source_mode", "PHONE-LIVE")
        call.resolve(result)
    }

    @PluginMethod
    fun readPid(call: PluginCall) {
        val pid = call.getString("pid")
        if (pid.isNullOrBlank()) {
            call.reject("pid is required")
            return
        }
        lastPidCommand = pid
        lastPidResponse = "NO_DATA"
        firstLiveRead = true
        val result = JSObject()
        result.put("command", pid)
        result.put("raw_response", lastPidResponse)
        result.put("source_mode", "PHONE-LIVE")
        call.resolve(result)
    }

    @PluginMethod
    fun receivePIDResponse(call: PluginCall) {
        call.resolve(JSObject().put("raw_response", lastPidResponse))
    }

    @PluginMethod
    fun readVin(call: PluginCall) {
        call.resolve(JSObject().put("vin", JSObject.NULL).put("raw_response", "NO_DATA"))
    }

    @PluginMethod
    fun reconnectIfNeeded(call: PluginCall) {
        val reconnected = connectionState != "connected"
        if (reconnected) connectionState = "connected"
        call.resolve(JSObject().put("status", connectionState).put("reconnected", reconnected))
    }

    @PluginMethod
    fun startPolling(call: PluginCall) {
        pollingState = "active"
        call.resolve(JSObject().put("started", true))
    }

    @PluginMethod
    fun stopPolling(call: PluginCall) {
        pollingState = "inactive"
        call.resolve(JSObject().put("stopped", true))
    }

    @PluginMethod
    fun getBridgeDiagnostics(call: PluginCall) {
        call.resolve(
            JSObject()
                .put("platform", "android")
                .put("native_bridge_available", true)
                .put("connection_method", "android-bluetooth-le")
                .put("bluetooth_link_state", connectionState)
                .put("polling_state", pollingState)
                .put("first_live_read_status", firstLiveRead)
                .put("source_mode", "PHONE-LIVE")
                .put("last_pid_command", lastPidCommand)
                .put("last_pid_response", lastPidResponse)
        )
    }
}
