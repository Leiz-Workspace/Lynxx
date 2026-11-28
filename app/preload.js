// Lynxx desktop: preload bridge (contextIsolation). The renderer never sees API keys or spawns processes itself.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

contextBridge.exposeInMainWorld('lynxx', {
  rpc: (method, params) => invoke('rpc', method, params || {}),
  sidecarStatus: () => invoke('sidecar:getStatus'),
  tags: {
    update: (module, opts) => invoke('tags:update', module, opts), // opts: { full: true } reads the whole list
    scheduleStatus: () => invoke('tags:scheduleStatus'),
    schedule: (options) => invoke('tags:schedule', options), // {enabled, frequency: 'daily' | 'weekly'}: modules 'auto' (main.js)
  },
  search: {
    count: (query) => invoke('search:count', query),
    preview: (query, page) => invoke('search:preview', query, page),
    related: (query) => invoke('search:related', query),
  },
  settings: {
    get: () => invoke('settings:get'),
    set: (patch) => invoke('settings:set', patch),
  },
  secrets: {
    set: (module, value) => invoke('secrets:set', module, value), // empty value: no change
    clear: (module) => invoke('secrets:clear', module),
  },
  jobs: {
    start: (query) => invoke('jobs:start', query),
    cancel: (id) => invoke('jobs:cancel', id),
    list: () => invoke('jobs:list'),
    clear: () => invoke('jobs:clear'),
    rerun: (id, allowDuplicate) => invoke('jobs:rerun', id, allowDuplicate === true),
    // one query on several sites: one job per module, one after another, -skip_known forced (main builds the group)
    startMulti: (query, modules) => invoke('jobs:startMulti', query, modules),
  },
  dialogs: {
    folder: (current) => invoke('dialog:folder', current),
    image: () => invoke('dialog:image'),
    document: () => invoke('dialog:document'),
    zip: () => invoke('dialog:zip'),
  },
  ext: {
    installZip: (file, sha256) => invoke('ext:installZip', file, sha256),
    installCatalog: (id) => invoke('ext:installCatalog', id),
    installRemote: (item) => invoke('ext:installRemote', item),
  },
  gen: {
    // extension generator: creates drafts; install is a separate call that main confirms natively. The API key is write-only
    status: () => invoke('gen:status'),
    start: (opts) => invoke('gen:start', opts),
    cancel: () => invoke('gen:cancel'),
    list: () => invoke('gen:list'),
    get: (session) => invoke('gen:get', session),
    open: (session) => invoke('gen:open', session),
    exportZip: (session) => invoke('gen:export', session),
    install: (session) => invoke('gen:install', session),
    remove: (session) => invoke('gen:remove', session),
    setKey: (value) => invoke('gen:setKey', value),
    clearKey: () => invoke('gen:clearKey'),
  },
  genderModel: {
    status: () => invoke('gmodel:status'),
    load: () => invoke('gmodel:load'), // turn on: load the model on the server now
    unload: () => invoke('gmodel:unload'), // turn off: free its memory on the server
    setToken: (value) => invoke('gmodel:setToken', value),
    clearToken: () => invoke('gmodel:clearToken'),
  },
  site: {
    // "Xác minh trong Lynxx": main opens the module's website in a window where the user passes its bot challenge, then saves the
    // cookie and User-Agent for that module. Only the abbr goes; the answer is {ok, expires} or {ok: false, reason, message}
    verify: (module) => invoke('site:verify', module),
  },
  saved: {
    // saved query schedule: every = hours (0 = off), from 'now' (only posts newer than today) or 'all'
    setAuto: (key, every, from) => invoke('saved:auto', key, every, from),
  },
  reader: {
    open: (file) => invoke('reader:open', file),
    import: (file, title) => invoke('reader:import', file, title),
    epubPages: (file) => invoke('reader:epubPages', file),
    chapter: (chapterId) => invoke('reader:chapter', chapterId),
    progressGet: (chapterId) => invoke('reader:progressGet', chapterId),
    progressSet: (p) => invoke('reader:progressSet', p), // {chapterId, page, scrollOffset, mode, zoom, direction}
  },
  // vBook-format extensions: installed from a folder / plugin.zip / link / repository index (main confirms natively); their
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
  library: {
    // files of the download index: open with the default app (media extensions only) or show in the file manager
    open: (file) => invoke('library:open', file),
    reveal: (file) => invoke('library:reveal', file),
    // delete: paths of index rows only (the core refuses others). trash / restore are reversible; forget (rows of files already
    // gone), purge (files of the trash) and emptyTrash cannot be undone: main asks with a native dialog
    trash: (paths) => invoke('library:trash', paths),
    restore: (paths) => invoke('library:restore', paths),
    forget: (paths) => invoke('library:forget', paths),
    purge: (paths) => invoke('library:purge', paths),
    emptyTrash: () => invoke('library:emptyTrash'),
  },
  index: {
    // download index maintenance. clean: rows whose file is gone (main confirms natively; {dryRun: true} only counts).
    // Library import: the folder comes from pickImportFolder (native dialog), the import runs as a job
    clean: (opts) => invoke('index:clean', opts),
    cleanCancel: () => invoke('index:cleanCancel'),
    pickImportFolder: () => invoke('index:pickFolder'),
    importLibrary: (opts) => invoke('index:import', opts),
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
  // module: the Preview's module, whose per-site headers / cookies go with the request to its own website over https (main
  // upstreamHeaders)
  thumbUrl: (url, referer, module) => `lynxx-thumb://t/?u=${encodeURIComponent(url)}&r=${encodeURIComponent(referer || '')}${module ? `&m=${encodeURIComponent(module)}` : ''}`,
  mediaUrl: (url, referer, module) => `lynxx-thumb://m/?u=${encodeURIComponent(url)}&r=${encodeURIComponent(referer || '')}${module ? `&m=${encodeURIComponent(module)}` : ''}`,
  on: (channel, cb) => {
    const allowed = ['log', 'sidecar:status', 'sidecar:notify', 'jobs:update', 'jobs:log', 'appearance:changed', 'gen:event', 'nav:page', 'book:update'];
    if (!allowed.includes(channel)) throw new Error(`channel not allowed: ${channel}`);
    const handler = (_event, data) => cb(data);
    ipcRenderer.on(channel, handler);
    return () => ipcRenderer.removeListener(channel, handler);
  },
});
