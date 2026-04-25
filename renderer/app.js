const THEME_COLORS = {
  Red: { accent: '#b30e20', bright: '#ff3048', glow: '#ff5c6d' },
  Blue: { accent: '#1b5cff', bright: '#4d86ff', glow: '#6aa0ff' },
  Green: { accent: '#198a39', bright: '#34d063', glow: '#61e68a' },
  Purple: { accent: '#6f2bce', bright: '#9653f6', glow: '#b483ff' },
  Orange: { accent: '#c75d10', bright: '#ff8b2c', glow: '#ffac63' },
  Cyan: { accent: '#0e91a4', bright: '#25cde4', glow: '#6fe7f4' },
  Gold: { accent: '#af820d', bright: '#d9aa2e', glow: '#ebc867' },
  Pink: { accent: '#be2f73', bright: '#f2559d', glow: '#ff8fbe' }
};

const state = {
  settings: {},
  events: [],
  stats: {},
  search: '',
  sortKey: 'timestampUtc',
  sortReverse: true
};

const $ = (id) => document.getElementById(id);

function setStatus(text) { $('status').textContent = text || ''; }

function applyTheme(name) {
  const theme = THEME_COLORS[name] || THEME_COLORS.Red;
  document.documentElement.style.setProperty('--accent', theme.accent);
  document.documentElement.style.setProperty('--bright', theme.bright);
  document.documentElement.style.setProperty('--glow', theme.glow);
}

function updateFromBackend(payload) {
  if (!payload) return;
  if (payload.error) { setStatus(payload.error); return; }
  state.settings = payload.settings || state.settings;
  state.events = payload.events || state.events;
  state.stats = payload.stats || state.stats;
  applyTheme(state.settings.themeName || 'Red');
  $('themeSelect').value = state.settings.themeName || 'Red';
  $('dedupeInput').value = state.settings.dedupeSeconds ?? 60;
  $('folderPath').textContent = state.settings.gameFolderPath || 'No game folder selected';
  $('folderBtn').textContent = state.settings.gameFolderPath ? 'Change Game Folder' : 'Set Game Folder';
  setStatus(payload.status || `${state.events.length} event(s)`);
  renderEvents();
  renderStats();
}

async function backend(command, payload = {}) {
  const result = await window.crimeScanner.request(command, payload);
  updateFromBackend(result);
  return result;
}

function filteredSortedEvents() {
  const q = state.search.trim().toLowerCase();
  const events = state.events.filter(e => {
    if (!q) return true;
    return [e.player, e.organization || 'Unknown', e.crime, e.displayTime]
      .some(value => String(value || '').toLowerCase().includes(q));
  });
  events.sort((a, b) => {
    const av = String(a[state.sortKey] || '').toLowerCase();
    const bv = String(b[state.sortKey] || '').toLowerCase();
    if (av < bv) return state.sortReverse ? 1 : -1;
    if (av > bv) return state.sortReverse ? -1 : 1;
    return 0;
  });
  return events;
}

function linkOrText(url, text) {
  if (!url) return `<span>${escapeHtml(text)}</span>`;
  return `<a href="#" data-url="${escapeHtml(url)}">${escapeHtml(text)}</a>`;
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function renderEvents() {
  const host = $('eventsList');
  const events = filteredSortedEvents();
  if (!state.events.length) {
    host.className = 'events-list empty';
    host.textContent = 'No logged events yet. Set the game folder and parse.';
    return;
  }
  if (!events.length) {
    host.className = 'events-list empty';
    host.textContent = 'No matching results for the current search.';
    return;
  }
  host.className = 'events-list';
  host.innerHTML = events.map(e => `
    <div class="event-row ${e.targetType === 'self' ? 'self' : ''}">
      <div class="identity">
        <img class="avatar" src="${escapeHtml(e.playerAvatarUrl)}" referrerpolicy="no-referrer" />
        <div class="minmax">
          <div class="cell-title">${linkOrText(e.playerUrl, e.player)}</div>
          <div class="cell-sub">${e.targetType === 'self' ? 'Against you' : 'Against others'}</div>
        </div>
      </div>
      <div class="identity">
        <img class="avatar" src="${escapeHtml(e.organizationLogoUrl)}" referrerpolicy="no-referrer" />
        <div class="minmax">
          <div class="cell-title">${linkOrText(e.organizationUrl, e.organization || 'Unknown')}</div>
          <div class="cell-sub">Organization</div>
        </div>
      </div>
      <div><div class="cell-title">${escapeHtml(e.crime)}</div><div class="cell-sub">Crime</div></div>
      <div><div class="cell-title">${escapeHtml(e.displayTime)}</div><div class="cell-sub">UTC</div></div>
    </div>
  `).join('');
  host.querySelectorAll('a[data-url]').forEach(link => {
    link.addEventListener('click', event => {
      event.preventDefault();
      window.crimeScanner.openExternal(link.dataset.url);
    });
  });
}

function renderStats() {
  const stats = state.stats || {};
  $('summaryCards').innerHTML = [
    ['Total Events', stats.totalEvents || 0],
    ['Against Others', stats.totalOther || 0],
    ['Against You', stats.totalSelf || 0]
  ].map(([label, value]) => `<div class="summary-card"><h3>${label}</h3><strong>${value}</strong></div>`).join('');

  const sections = [
    ['Most Killed', stats.killed || []],
    ['Killed You Most', stats.killedBy || []],
    ['Most Encountered Org', stats.encounteredOrg || []]
  ];
  $('statsSections').innerHTML = sections.map(([title, rows]) => `
    <section class="stats-section">
      <h2>${escapeHtml(title)} (${rows.reduce((sum, row) => sum + Number(row[1] || 0), 0)})</h2>
      ${rows.length ? rows.slice(0, 10).map((row, index) => `
        <div class="stat-row"><span>${index + 1}.</span><span>${escapeHtml(row[0])}</span><span class="badge">${row[1]}</span></div>
      `).join('') : '<p class="cell-sub">No data yet.</p>'}
    </section>
  `).join('');
}

function showView(name) {
  $('homeView').classList.toggle('active', name === 'home');
  $('statsView').classList.toggle('active', name === 'stats');
  $('homeTab').classList.toggle('active', name === 'home');
  $('statsTab').classList.toggle('active', name === 'stats');
}

$('homeTab').addEventListener('click', () => showView('home'));
$('statsTab').addEventListener('click', () => showView('stats'));
$('searchInput').addEventListener('input', (event) => { state.search = event.target.value; renderEvents(); });
$('parseBtn').addEventListener('click', () => backend('parseNow'));
$('enrichBtn').addEventListener('click', () => backend('enrichMetadata'));
$('clearCacheBtn').addEventListener('click', () => backend('clearCache'));
$('folderBtn').addEventListener('click', async () => {
  const folder = await window.crimeScanner.chooseFolder();
  if (folder) await backend('setSettings', { gameFolderPath: folder });
});
$('themeSelect').addEventListener('change', (event) => backend('setSettings', { themeName: event.target.value }));
$('dedupeInput').addEventListener('change', (event) => backend('setSettings', { dedupeSeconds: Number(event.target.value || 60) }));
document.querySelectorAll('[data-sort]').forEach(button => {
  button.addEventListener('click', () => {
    const key = button.dataset.sort;
    if (state.sortKey === key) state.sortReverse = !state.sortReverse;
    else { state.sortKey = key; state.sortReverse = key === 'timestampUtc'; }
    renderEvents();
  });
});

window.crimeScanner.onReady(updateFromBackend);
window.crimeScanner.onLog(text => setStatus(String(text).trim()));
setInterval(() => backend('parseNow').catch(error => setStatus(error.message)), 2000);
