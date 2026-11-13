const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('notesAPI', {
  fetchNotes: () => ipcRenderer.invoke('fetch-notes'),
  readNote: (pk) => ipcRenderer.invoke('read-note', pk),
  openNote: (noteName) => ipcRenderer.invoke('open-note', noteName),
  loadConfig: () => ipcRenderer.invoke('load-config'),
  saveConfig: (config) => ipcRenderer.invoke('save-config', config),
  suggestLinks: (pk) => ipcRenderer.invoke('suggest-links', pk),
  rankSuggestions: (payload) => ipcRenderer.invoke('rank-suggestions', payload),
  addLink: (payload) => ipcRenderer.invoke('add-link', payload),
  deleteNote: (pk) => ipcRenderer.invoke('delete-note', pk),
  semanticSearch: (query) => ipcRenderer.invoke('semantic-search', query),
  nameClusters: (payload) => ipcRenderer.invoke('name-clusters', payload),
  getUiUpdateStatus: () => ipcRenderer.invoke('get-ui-update-status')
});
