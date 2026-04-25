const { app, BrowserWindow, ipcMain, dialog, shell, Tray, Menu, Notification, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const { pathToFileURL } = require('url');
const { spawn } = require('child_process');
let autoUpdater = null;
try { ({ autoUpdater } = require('electron-updater')); } catch (_error) { autoUpdater = null; }

let mainWindow;
let backend;
let tray;
let isQuitting = false;
let exitOnClose = false;
let nextId = 1;
const pending = new Map();

function isWindowAlive() {
  return Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents && !mainWindow.webContents.isDestroyed());
}

function safeSend(channel, payload) {
  if (!isWindowAlive()) return;
  mainWindow.webContents.send(channel, payload);
}

function assetPath(name) {
  if (app.isPackaged) return path.join(process.resourcesPath, 'assets', name);
  return path.join(__dirname, '..', 'assets', name);
}

function backendPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend', 'crimescanner_backend.exe');
  }
  return path.join(__dirname, '..', 'backend', 'crimescanner_api.py');
}

function startBackend() {
  const target = backendPath();
  const packagedExe = app.isPackaged || target.endsWith('.exe');
  const command = packagedExe ? target : (process.env.PYTHON || 'python');
  const args = packagedExe ? [] : ['-u', target];

  backend = spawn(command, args, {
    cwd: app.getPath('userData'),
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    shell: false,
    detached: false,
    env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
  });

  let buffer = '';
  backend.stdout.on('data', chunk => {
    buffer += chunk.toString();
    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      try {
        const message = JSON.parse(line);
        if (message.type === 'ready') {
          safeSend('backend-ready', message.payload);
          continue;
        }
        const request = pending.get(message.id);
        if (request) {
          pending.delete(message.id);
          request.resolve(message.payload);
        }
      } catch (error) {
        safeSend('backend-log', line);
      }
    }
  });

  backend.stderr.on('data', chunk => {
    safeSend('backend-log', chunk.toString());
  });

  backend.on('exit', code => {
    for (const request of pending.values()) {
      request.reject(new Error(`Backend exited with code ${code}`));
    }
    pending.clear();
    if (!isQuitting) safeSend('backend-log', `Backend exited with code ${code}`);
  });
}

function requestBackend(command, payload = {}) {
  return new Promise((resolve, reject) => {
    if (!backend || backend.killed) {
      reject(new Error('Backend is not running'));
      return;
    }
    const id = nextId++;
    const timer = setTimeout(() => {
      if (!pending.has(id)) return;
      pending.delete(id);
      reject(new Error(`${command} timed out. The backend may still be parsing a large Game.log.`));
    }, command === 'parseNow' ? 180000 : 15000);

    pending.set(id, {
      resolve: payload => {
        clearTimeout(timer);
        resolve(payload);
      },
      reject: error => {
        clearTimeout(timer);
        reject(error);
      }
    });

    try {
      backend.stdin.write(JSON.stringify({ id, command, payload }) + '\n');
    } catch (error) {
      clearTimeout(timer);
      pending.delete(id);
      reject(error);
    }
  });
}

function createTray() {
  const icon = nativeImage.createFromPath(assetPath('icon.ico'));
  tray = new Tray(icon.isEmpty() ? nativeImage.createFromPath(assetPath('icon.png')) : icon);
  tray.setToolTip('CrimeScanner');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show CrimeScanner', click: () => showMainWindow() },
    { label: 'Parse Now', click: () => requestBackend('parseNow').catch(() => {}) },
    { type: 'separator' },
    { label: 'Quit', click: () => { isQuitting = true; app.quit(); } }
  ]));
  tray.on('double-click', () => showMainWindow());
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.show();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 780,
    minWidth: 1080,
    minHeight: 650,
    backgroundColor: '#09090d',
    icon: assetPath('icon.ico'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  const builtRenderer = path.join(__dirname, '..', 'renderer', 'dist', 'index.html');
  const sourceRenderer = path.join(__dirname, '..', 'renderer', 'index.html');
  mainWindow.setMenuBarVisibility(false);
  mainWindow.loadFile(fs.existsSync(builtRenderer) ? builtRenderer : sourceRenderer);

  mainWindow.on('close', event => {
    if (isQuitting || exitOnClose) {
      isQuitting = true;
      return;
    }
    event.preventDefault();
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.hide();
    if (Notification.isSupported()) {
      new Notification({
        title: 'CrimeScanner is still running',
        body: 'It has been minimized to the system tray.',
        icon: assetPath('icon.png')
      }).show();
    }
  });
}


function setupAutoUpdater() {
  if (!autoUpdater || !app.isPackaged) return;
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on('checking-for-update', () => safeSend('update:status', { status: 'checking' }));
  autoUpdater.on('update-available', info => safeSend('update:status', { status: 'available', info }));
  autoUpdater.on('update-not-available', info => safeSend('update:status', { status: 'none', info }));
  autoUpdater.on('download-progress', progress => safeSend('update:status', { status: 'downloading', progress }));
  autoUpdater.on('update-downloaded', info => {
    safeSend('update:status', { status: 'downloaded', info });
    if (Notification.isSupported()) {
      new Notification({
        title: 'CrimeScanner update ready',
        body: 'Restart CrimeScanner to install the update.',
        icon: assetPath('icon.png')
      }).show();
    }
  });
  autoUpdater.on('error', error => safeSend('update:status', { status: 'error', message: error?.message || String(error) }));
  setTimeout(() => autoUpdater.checkForUpdatesAndNotify().catch(error => safeSend('backend-log', 'Update check failed: ' + error.message)), 2500);
}

function checkForUpdates() {
  if (!autoUpdater || !app.isPackaged) return Promise.resolve({ status: 'disabled' });
  return autoUpdater.checkForUpdatesAndNotify();
}

function installUpdateNow() {
  if (!autoUpdater || !app.isPackaged) return false;
  isQuitting = true;
  autoUpdater.quitAndInstall(false, true);
  return true;
}

app.setAppUserModelId('com.crimescanner.app');

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  createWindow();
  createTray();
  startBackend();
  setupAutoUpdater();
});

app.on('before-quit', () => { isQuitting = true; });

app.on('window-all-closed', () => {
  if (isQuitting || exitOnClose) app.quit();
});

app.on('will-quit', () => {
  if (backend && !backend.killed) backend.kill();
});

ipcMain.handle('backend:request', (_event, command, payload) => requestBackend(command, payload));
ipcMain.handle('dialog:chooseFolder', async () => {
  const browserWindow = isWindowAlive() ? mainWindow : undefined;
  const result = await dialog.showOpenDialog(browserWindow, {
    title: 'Select Star Citizen game folder',
    properties: ['openDirectory']
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});
ipcMain.handle('shell:openExternal', (_event, url) => shell.openExternal(url));
ipcMain.handle('app:notify', (_event, payload = {}) => {
  if (!Notification.isSupported()) return false;
  new Notification({
    title: payload.title || 'CrimeScanner',
    body: payload.body || '',
    icon: assetPath('icon.png')
  }).show();
  return true;
});
ipcMain.handle('app:getAutoLaunch', () => app.getLoginItemSettings().openAtLogin);
ipcMain.handle('app:setAutoLaunch', (_event, enabled) => {
  app.setLoginItemSettings({ openAtLogin: Boolean(enabled) });
  return app.getLoginItemSettings().openAtLogin;
});

ipcMain.handle('app:setExitOnClose', (_event, enabled) => {
  exitOnClose = Boolean(enabled);
  return exitOnClose;
});


ipcMain.handle('app:getAssetPath', (_event, name) => pathToFileURL(assetPath(String(name || ''))).toString());

ipcMain.handle('app:checkForUpdates', () => checkForUpdates());
ipcMain.handle('app:installUpdateNow', () => installUpdateNow());
