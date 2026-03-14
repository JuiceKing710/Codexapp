import { registerPlugin } from '@capacitor/core';

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'failed';

export interface PollingConfig {
  intervalMs: number;
  pids: string[];
}

export interface OBDReadResult {
  command: string;
  raw_response: string;
  value?: number | string | null;
  unit?: string | null;
  latency_ms?: number | null;
  source_mode: 'PHONE-LIVE';
}

export interface BridgeDiagnostics {
  platform: 'ios' | 'android' | 'browser' | 'unknown';
  native_bridge_available: boolean;
  connection_method: string;
  bluetooth_link_state: string;
  polling_state: string;
  first_live_read_status: boolean;
  source_mode: string;
  last_pid_command?: string | null;
  last_pid_response?: string | null;
  ingest_status?: string | null;
  backend_acceptance?: string | null;
  fallback_reason?: string | null;
  last_error?: string | null;
}

export interface ObdBridgePlugin {
  connectToAdapter(): Promise<{ status: ConnectionState; adapter_name?: string }>;
  disconnectAdapter(): Promise<{ status: ConnectionState }>;
  getConnectionState(): Promise<{ status: ConnectionState }>;
  readPid(options: { pid: string }): Promise<OBDReadResult>;
  readVin(): Promise<{ vin: string | null; raw_response?: string | null }>;
  reconnectIfNeeded(): Promise<{ status: ConnectionState; reconnected: boolean }>;
  startPolling(options: PollingConfig): Promise<{ started: boolean }>;
  stopPolling(): Promise<{ stopped: boolean }>;
  getBridgeDiagnostics(): Promise<BridgeDiagnostics>;
}

export const ObdBridge = registerPlugin<ObdBridgePlugin>('ObdBridge');
