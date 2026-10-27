const { contextBridge, ipcRenderer } = require('electron');

// 渲染进程没有 Node、文件系统、shell 或会话秘密；只获得最小 RPC 接口。
contextBridge.exposeInMainWorld('ilink', Object.freeze({
  request: (path, method = 'GET', body) => ipcRenderer.invoke('gateway:request', { path, method, body }),
  chooseSticker: () => ipcRenderer.invoke('dialog:open-sticker'),
  quit: () => ipcRenderer.invoke('app:quit'),
  openLogFolder: () => ipcRenderer.invoke('app:open-log-folder'),
  openExternal: (url) => ipcRenderer.invoke('app:open-external', url),
  chooseBackend: () => ipcRenderer.invoke('dialog:choose-backend'),
  getAutostart: () => ipcRenderer.invoke('app:autostart-status'),
  setAutostart: (enabled) => ipcRenderer.invoke('app:autostart', enabled),
}));
