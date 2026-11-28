// Lynxx extension sandbox preload (sandboxed renderer, context isolation): the only bridge extension code can reach.
// Every call is synchronous (sendSync) because vBook extensions call fetch() synchronously; main checks the sender.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lynxxSandbox', {
  call: (op, payload) => ipcRenderer.sendSync('plugin-sandbox:sync', String(op), payload === undefined ? null : payload),
  log: (level, text) => ipcRenderer.send('plugin-sandbox:log', String(level), String(text)),
  onRun: (cb) => { ipcRenderer.on('plugin-sandbox:run', (_e, job) => cb(job)); },
  done: (id, result) => ipcRenderer.send('plugin-sandbox:done', String(id), result),
  ready: () => ipcRenderer.send('plugin-sandbox:ready'),
});
