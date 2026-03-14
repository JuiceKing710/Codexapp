import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.zeb.obdai',
  appName: 'Zeb OBD AI',
  webDir: '../src/sienna_diag/api/static',
  bundledWebRuntime: false,
  server: {
    androidScheme: 'https',
    cleartext: true,
    url: process.env.ZEB_BACKEND_URL || 'http://10.0.2.2:8000/dashboard',
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 300,
    },
  },
};

export default config;
