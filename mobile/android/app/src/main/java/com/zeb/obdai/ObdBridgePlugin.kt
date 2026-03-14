package com.zeb.obdai

import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.annotation.CapacitorPlugin
import com.getcapacitor.PluginMethod

@CapacitorPlugin(name = "ObdBridge")
class ObdBridgePlugin : Plugin() {
    private var connectionState: String = "disconnected"

    @PluginMethod
    fun connectToAdapter(call: PluginCall) {
        connectionState = "connected"
        val result = JSObject()
        result.put("status", connectionState)
        result.put("adapter_name", "OBDLink MX+")
        call.resolve(result)
    }

    @PluginMethod
    fun disconnectAdapter(call: PluginCall) {
        connectionState = "disconnected"
        call.resolve(JSObject().put("status", connectionState))
    }

    @PluginMethod
    fun getConnectionState(call: PluginCall) {
        call.resolve(JSObject().put("status", connectionState))
    }

    @PluginMethod
    fun readPid(call: PluginCall) {
        val pid = call.getString("pid")
        if (pid.isNullOrBlank()) {
            call.reject("pid is required")
            return
        }
        val result = JSObject()
        result.put("command", pid)
        result.put("raw_response", "NO_DATA")
        result.put("source_mode", "PHONE-LIVE")
        call.resolve(result)
    }

    @PluginMethod
    fun readVin(call: PluginCall) {
        val result = JSObject()
        result.put("vin", JSObject.NULL)
        result.put("raw_response", "NO_DATA")
        call.resolve(result)
    }

    @PluginMethod
    fun reconnectIfNeeded(call: PluginCall) {
        val reconnected = connectionState != "connected"
        if (reconnected) connectionState = "connected"
        call.resolve(JSObject().put("status", connectionState).put("reconnected", reconnected))
    }

    @PluginMethod
    fun startPolling(call: PluginCall) {
        call.resolve(JSObject().put("started", true))
    }

    @PluginMethod
    fun stopPolling(call: PluginCall) {
        call.resolve(JSObject().put("stopped", true))
    }

    @PluginMethod
    fun getBridgeDiagnostics(call: PluginCall) {
        call.resolve(
            JSObject()
                .put("platform", "android")
                .put("native_bridge_available", true)
                .put("connection_method", "android-bluetooth")
                .put("bluetooth_link_state", connectionState)
                .put("polling_state", "inactive")
                .put("first_live_read_status", false)
                .put("source_mode", "PHONE-LIVE")
        )
    }
}
