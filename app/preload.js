// Lynxx desktop: preload bridge (contextIsolation). The renderer never spawns processes, reads files or runs extension code.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

contextBridge.exposeInMainWorld('lynxx', {
  rpc: (method, params) => invoke('rpc', method, params || {}), // app.info, formats.supported only (main.js RENDERER_RPC)
  sidecarStatus: () => invoke('sidecar:getStatus'),
  settings: {
    get: () => invoke('settings:get'),
    set: (patch) => invoke('settings:set', patch),
  },
  dialogs: {
    folder: (current) => invoke('dialog:folder', current),
    image: () => invoke('dialog:image'),
    document: () => invoke('dialog:document'),
  },
  reader: {
    import: (file, title) => invoke('reader:import', file, title),
    chapter: (chapterId) => invoke('reader:chapter', chapterId),
    progressGet: (chapterId) => invoke('reader:progressGet', chapterId),
    progressSet: (p) => invoke('reader:progressSet', p), // {chapterId, page, scrollOffset, mode, zoom, direction}
  },
  // extensions: installed from a folder / plugin.zip / link / repository index (main confirms natively); their
  // scripts run in main's sandbox, never in this page
  plugins: {
    list: () => invoke('plugins:list'),
    repositories: () => invoke('plugins:repositories'),
    installFolder: () => invoke('plugins:installFolder'),
    installZip: () => invoke('plugins:installZip'),
    installUrl: (url, repository) => invoke('plugins:installUrl', url, repository),
    repoFetch: (url) => invoke('plugins:repoFetch', url),
    repoAdd: (url) => invoke('plugins:repoAdd', url),
    repoRemove: (url) => invoke('plugins:repoRemove', url),
    uninstall: (id) => invoke('plugins:uninstall', id),
    setEnabled: (id, enabled) => invoke('plugins:setEnabled', id, enabled),
    setConfig: (id, values) => invoke('plugins:setConfig', id, values),
    openSite: (id) => invoke('plugins:openSite', id),
    match: (url) => invoke('plugins:match', url),
    home: (id) => invoke('plugins:home', id),
    genre: (id) => invoke('plugins:genre', id),
    listItems: (id, script, input, page) => invoke('plugins:listItems', id, script, input, page),
    search: (id, query, page) => invoke('plugins:search', id, query, page),
    detail: (id, url) => invoke('plugins:detail', id, url),
    toc: (id, url) => invoke('plugins:toc', id, url),
  },
  books: {
    download: (params) => invoke('book:download', params), // {pluginId, url, scope: 'all' | 'missing' | 'selected', chapterUrls}
    jobs: () => invoke('book:jobs'),
    cancel: (id) => invoke('book:cancel', id),
    clear: () => invoke('book:clear'),
  },
  shelf: {
    collections: () => invoke('library:collections'),
    collection: (id) => invoke('library:collection', id),
    update: (id) => invoke('library:update', id),
    fillRemote: (id) => invoke('library:fillRemote', id), // chapters known only by image links (imported exports)
    remove: (id) => invoke('library:delete', id),
    importFile: () => invoke('library:importFile'),
    export: (params) => invoke('library:export', params), // {collectionId, format, options, downloadMissing}: main asks where to save
    exportRefresh: (collectionId, exportId) => invoke('library:exportRefresh', collectionId, exportId),
    exportForget: (exportId) => invoke('library:exportForget', exportId),
    revealExport: (collectionId, exportId) => invoke('library:revealExport', collectionId, exportId),
  },
  appearance: {
    setBackgroundImage: (file) => invoke('appearance:setBackgroundImage', file),
  },
  app: {
    info: () => invoke('app:info'),
    relaunch: () => invoke('app:relaunch'),
    installLauncher: () => invoke('launcher:install'),
    openPath: (p) => invoke('shell:open', p),
    openExternal: (url) => invoke('shell:external', url),
  },
  // covers of the sites, fetched and cached by main (public hosts only)
  thumbUrl: (url, referer) => `lynxx-thumb://t/?u=${encodeURIComponent(url)}&r=${encodeURIComponent(referer || '')}`,
  on: (channel, cb) => {
    const allowed = ['log', 'sidecar:status', 'sidecar:notify', 'appearance:changed', 'nav:page', 'book:update', 'plugins:changed'];
    if (!allowed.includes(channel)) throw new Error(`channel not allowed: ${channel}`);
    const handler = (_event, data) => cb(data);
    ipcRenderer.on(channel, handler);
    return () => ipcRenderer.removeListener(channel, handler);
  },
});
