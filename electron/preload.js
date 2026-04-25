const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('crimeScanner', {
  request: (command, payload) => ipcRenderer.invoke('backend:request', command, payload),
  chooseFolder: () => ipcRenderer.invoke('dialog:chooseFolder'),
  openExternal: (url) => ipcRenderer.invoke('shell:openExternal', url),
  notify: (payload) => ipcRenderer.invoke('app:notify', payload),
  getAutoLaunch: () => ipcRenderer.invoke('app:getAutoLaunch'),
  setAutoLaunch: (enabled) => ipcRenderer.invoke('app:setAutoLaunch', enabled),
  setExitOnClose: (enabled) => ipcRenderer.invoke('app:setExitOnClose', enabled),
  getAssetPath: (name) => ipcRenderer.invoke('app:getAssetPath', name),
  checkForUpdates: () => ipcRenderer.invoke('app:checkForUpdates'),
  installUpdateNow: () => ipcRenderer.invoke('app:installUpdateNow'),
  getVersion: () => ipcRenderer.invoke('app:getVersion'),
  onReady: (callback) => ipcRenderer.on('backend-ready', (_event, payload) => callback(payload)),
  onLog: (callback) => ipcRenderer.on('backend-log', (_event, payload) => callback(payload)),
  onUpdateStatus: (callback) => ipcRenderer.on('update:status', (_event, payload) => callback(payload))
});
