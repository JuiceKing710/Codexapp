(function initMobileBridge(global){
  const platform = /iphone|ipad|ipod/.test(navigator.userAgent.toLowerCase())
    ? 'ios'
    : /android/.test(navigator.userAgent.toLowerCase())
      ? 'android'
      : 'browser';

  function unsupported(reason){
    return {
      status: 'failed',
      source_mode: 'PHONE-LIVE',
      supports_native_bluetooth: false,
      fallback_reason: reason || 'native-bridge-missing',
      platform,
    };
  }

  function plugin(){
    return global.Capacitor?.Plugins?.ObdBridge || null;
  }

  const service = {
    async connectToAdapter(){
      const p = plugin();
      if(!p?.connectToAdapter){ return unsupported('Live Bluetooth requires the mobile app'); }
      return p.connectToAdapter();
    },
    async disconnectAdapter(){
      const p = plugin();
      if(!p?.disconnectAdapter){ return { status: 'disconnected' }; }
      return p.disconnectAdapter();
    },
    async getConnectionState(){
      const p = plugin();
      if(!p?.getConnectionState){ return { status: 'disconnected' }; }
      return p.getConnectionState();
    },
    async readPid(pid){
      const p = plugin();
      if(!p?.readPid){ throw new Error('Live Bluetooth requires the mobile app'); }
      return p.readPid({ pid });
    },
    async readVin(){
      const p = plugin();
      if(!p?.readVin){ return { vin: null, raw_response: null }; }
      return p.readVin();
    },
    async reconnectIfNeeded(){
      const p = plugin();
      if(!p?.reconnectIfNeeded){ return { status: 'failed', reconnected: false }; }
      return p.reconnectIfNeeded();
    },
    async startPolling(config){
      const p = plugin();
      if(!p?.startPolling){ return { started: false }; }
      return p.startPolling(config || { intervalMs: 500, pids: [] });
    },
    async stopPolling(){
      const p = plugin();
      if(!p?.stopPolling){ return { stopped: true }; }
      return p.stopPolling();
    },
    async getBridgeDiagnostics(){
      const p = plugin();
      if(!p?.getBridgeDiagnostics){
        return {
          platform,
          native_bridge_available: false,
          connection_method: 'browser-debug',
          bluetooth_link_state: 'unavailable',
          polling_state: 'inactive',
          first_live_read_status: false,
          source_mode: 'BROWSER-DEV',
          fallback_reason: 'Live Bluetooth requires the mobile app',
        };
      }
      return p.getBridgeDiagnostics();
    },
  };

  global.MobileBluetoothService = global.MobileBluetoothService || service;
})(window);
