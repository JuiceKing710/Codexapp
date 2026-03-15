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

  async function callBridge(primaryName, fallbackName, ...args){
    const p = plugin();
    const fn = p?.[primaryName] || (fallbackName ? p?.[fallbackName] : null);
    if(!fn){ return null; }
    return fn.apply(p, args);
  }

  const service = {
    async connectAdapter(){
      const res = await callBridge('connectAdapter', 'connectToAdapter');
      if(!res){ return unsupported('Live Bluetooth requires the mobile app'); }
      return res;
    },
    async connectToAdapter(){ return this.connectAdapter(); },
    async disconnectAdapter(){
      const res = await callBridge('disconnectAdapter');
      return res || { status: 'disconnected' };
    },
    async getConnectionState(){
      const res = await callBridge('getConnectionState');
      return res || { status: 'disconnected' };
    },
    async sendPIDCommand(command){
      const p = plugin();
      const fn = p?.sendPIDCommand || (p?.readPid ? (cmd) => p.readPid({ pid: cmd }) : null);
      if(!fn){ throw new Error('Live Bluetooth requires the mobile app'); }
      return fn.call(p, command);
    },
    async readPid(pid){ return this.sendPIDCommand(pid); },
    async receivePIDResponse(){
      const res = await callBridge('receivePIDResponse');
      return res || { raw_response: null };
    },
    async readVin(){
      const res = await callBridge('readVin');
      return res || { vin: null, raw_response: null };
    },
    async reconnectIfNeeded(){
      const res = await callBridge('reconnectIfNeeded');
      return res || { status: 'failed', reconnected: false };
    },
    async startPolling(config){
      const res = await callBridge('startPolling', null, config || { intervalMs: 500, pids: [] });
      return res || { started: false };
    },
    async stopPolling(){
      const res = await callBridge('stopPolling');
      return res || { stopped: true };
    },
    async getBridgeDiagnostics(){
      const res = await callBridge('getBridgeDiagnostics');
      if(!res){
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
      return res;
    },
  };

  global.MobileBluetoothService = global.MobileBluetoothService || service;
})(window);
