import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import brandLogo from './assets/brand.jpg';

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

const EMPTY_STATS = {
  totalEvents: 0,
  totalOther: 0,
  totalSelf: 0,
  killed: [],
  killedBy: [],
  encounteredOrg: []
};

const SCAN_SECONDS = 60;
const DEFAULT_PLAYER_AVATAR_URL = 'https://cdn.robertsspaceindustries.com/static/images/account/avatar_default_big.jpg';
const DEFAULT_ORG_LOGO_URL = 'https://cdn.robertsspaceindustries.com/static/images/organization/defaults/logo/syndicate.jpg';

function safeImageUrl(url, fallback) {
  const value = String(url || '').trim();
  if (!value) return fallback;
  if (value.startsWith('assets/') || value.startsWith('/assets/') || value.includes('icon.png') || value.includes('icon.ico')) return fallback;
  return value;
}


function normalizeEvent(event) {
  if (!event) return null;
  return {
    player: event.player || '',
    crime: event.crime || '',
    timestampUtc: event.timestampUtc || event.timestamp_utc || '',
    displayTime: event.displayTime || event.display_time || '',
    targetType: event.targetType || event.target_type || 'other',
    playerUrl: event.playerUrl || event.player_url || '',
    playerAvatarUrl: event.playerAvatarUrl || event.player_avatar_url || '',
    organization: event.organization || '',
    organizationUrl: event.organizationUrl || event.organization_url || '',
    organizationLogoUrl: event.organizationLogoUrl || event.organization_logo_url || ''
  };
}

function normalizeStats(stats, events) {
  const safe = stats || {};
  const totalEvents = Number(safe.totalEvents ?? safe.total_events ?? events.length ?? 0);
  const totalOther = Number(safe.totalOther ?? safe.total_other ?? events.filter(e => e.targetType === 'other').length ?? 0);
  const totalSelf = Number(safe.totalSelf ?? safe.total_self ?? events.filter(e => e.targetType === 'self').length ?? 0);
  return {
    totalEvents,
    totalOther,
    totalSelf,
    killed: safe.killed || [],
    killedBy: safe.killedBy || safe.killed_by || [],
    encounteredOrg: safe.encounteredOrg || safe.encountered_org || []
  };
}


function applyTheme(name) {
  const theme = THEME_COLORS[name] || THEME_COLORS.Red;
  document.documentElement.style.setProperty('--accent', theme.accent);
  document.documentElement.style.setProperty('--bright', theme.bright);
  document.documentElement.style.setProperty('--glow', theme.glow);
}

function external(url) {
  if (url) window.crimeScanner?.openExternal(url);
}

function eventTimestampMs(event) {
  const raw = String(event.timestampUtc || event.displayTime || '').trim();
  if (!raw) return 0;
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
  const normalized = hasTimezone ? raw : `${raw}Z`;
  const value = Date.parse(normalized);
  return Number.isFinite(value) ? value : 0;
}

function compactTime(event) {
  const parts = timeParts(event);
  return parts.time && parts.date ? `${parts.time} ${parts.date}` : (event.displayTime || '');
}

function timeParts(event) {
  const ms = eventTimestampMs(event);
  if (!ms) return { time: event.displayTime || '', date: '' };
  const date = new Date(ms);
  const yyyy = String(date.getUTCFullYear());
  const dd = String(date.getUTCDate()).padStart(2, '0');
  const mm = String(date.getUTCMonth() + 1).padStart(2, '0');
  const hh = String(date.getUTCHours()).padStart(2, '0');
  const min = String(date.getUTCMinutes()).padStart(2, '0');
  return { time: `${hh}:${min}`, date: `${dd}/${mm}/${yyyy}` };
}

function countSince(events, hours, predicate = () => true) {
  const cutoff = Date.now() - hours * 60 * 60 * 1000;
  return events.filter(event => predicate(event) && eventTimestampMs(event) >= cutoff).length;
}

function recentEntriesText(count) {
  if (!count) return 'No new entries in 24 hours';
  return `${count} new ${count === 1 ? 'entry' : 'entries'} in 24 hours`;
}

function ThemedAssetIcon({ src, fallback, className = '' }) {
  if (!src) return <span className={`asset-icon-fallback ${className}`}>{fallback}</span>;
  return (
    <span
      className={`asset-icon ${className}`}
      aria-hidden="true"
      style={{ WebkitMaskImage: `url("${src}")`, maskImage: `url("${src}")` }}
    />
  );
}

function StatCard({ label, value, hint, icon, iconSrc, recent }) {
  return (
    <article className="stat-card">
      <div className="stat-icon"><ThemedAssetIcon src={iconSrc} fallback={icon} /></div>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint ? <small>{hint}</small> : null}
      {recent ? <em>{recent}</em> : null}
    </article>
  );
}

function Sidebar({ activeTab, setActiveTab, status, settings, countdown, brandSrc, assetPaths }) {
  const nav = [
    ['home', 'navDashboard', '▦', 'Dashboard'],
    ['events', 'navHistory', '☷', 'History'],
    ['stats', 'navStatistics', '▥', 'Statistics'],
    ['settings', 'navSettings', '⚙', 'Settings']
  ];

  return (
    <aside className="sidebar">
      <div className="brand">
        <img src={brandSrc || brandLogo} alt="CrimeScanner raven icon" />
        <div>
          <h1><span>Crime</span>Scanner</h1>
        </div>
      </div>

      <nav className="side-nav">
        {nav.map(([key, assetKey, fallbackIcon, label]) => (
          <button key={key} className={activeTab === key ? 'active' : ''} onClick={() => setActiveTab(key)}>
            <span><ThemedAssetIcon src={assetPaths?.[assetKey]} fallback={fallbackIcon} /></span>{label}
          </button>
        ))}
      </nav>

      <div className="sidebar-spacer" />

      <section className="monitor-card">
        <strong><span className="live-dot" />Monitoring Active</strong>
        <p>Last scan: {countdown}s</p>
        <p title={status}>{status || 'Watching Game.log'}</p>
      </section>
    </aside>
  );
}

function TopBar({ activeTab, search, setSearch, connection = 'SQLite: Connected' }) {
  const title = {
    home: 'Dashboard',
    events: 'History',
    stats: 'Statistics',
    settings: 'Settings'
  }[activeTab] || 'Dashboard';

  return (
    <header className="topbar">
      <div className="top-title">
        <h2>{title}</h2>
      </div>
      {activeTab !== 'settings' && (
        <div className="search-wrap">
          <span>⌕</span>
          <input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search player, org, crime, or time…" />
        </div>
      )}
      <div className="top-actions">
        <span className="connection-pill">{connection}</span>
      </div>
    </header>
  );
}

function EventRow({ event }) {
  const isSelf = event.targetType === 'self';
  const orgName = event.organization || 'Unknown';
  const playerAvatar = safeImageUrl(event.playerAvatarUrl, DEFAULT_PLAYER_AVATAR_URL);
  const organizationLogo = safeImageUrl(event.organizationLogoUrl, DEFAULT_ORG_LOGO_URL);
  const when = timeParts(event);
  return (
    <article className={`event-row ${isSelf ? 'self' : 'other'}`}>
      <div className="person-cell">
        <img src={playerAvatar} alt="" onError={e => { e.currentTarget.src = DEFAULT_PLAYER_AVATAR_URL; }} />
        <div>
          <button className="link-button" onClick={() => external(event.playerUrl)}>{event.player}</button>
        </div>
      </div>
      <div className="crime-cell">
        <strong>{event.crime}</strong>
      </div>
      <div className="person-cell org-cell">
        <img src={organizationLogo} alt="" onError={e => { e.currentTarget.src = DEFAULT_ORG_LOGO_URL; }} />
        <div>
          <button className="link-button" onClick={() => external(event.organizationUrl)}>{orgName}</button>
        </div>
      </div>
      <div className="time-cell">
        <strong>{when.time}</strong>
        <small>{when.date}</small>
      </div>
    </article>
  );
}

function useVisibleEvents(events, search, sort) {
  return useMemo(() => {
    const query = search.trim().toLowerCase();
    const filtered = events.filter(event => {
      if (!query) return true;
      return [event.player, event.organization || 'Unknown', event.crime, event.displayTime, compactTime(event)]
        .some(value => String(value || '').toLowerCase().includes(query));
    });
    return [...filtered].sort((a, b) => {
      const av = String(a[sort.key] || '').toLowerCase();
      const bv = String(b[sort.key] || '').toLowerCase();
      if (av < bv) return sort.reverse ? 1 : -1;
      if (av > bv) return sort.reverse ? -1 : 1;
      return 0;
    });
  }, [events, search, sort]);
}

function EventsDashboard({ events, stats, search, sort, setSort, compact = false, viewportHeight = window.innerHeight, assetPaths }) {
  const visible = useVisibleEvents(events, search, sort);
  const latestLimit = 10;
  const displayed = compact ? visible : visible.slice(0, latestLimit);
  const recentTotal = countSince(events, 24);
  const recentSelf = countSince(events, 24, event => event.targetType === 'self');
  const recentOther = countSince(events, 24, event => event.targetType === 'other');

  function sortBy(key) {
    setSort(current => current.key === key ? { key, reverse: !current.reverse } : { key, reverse: key === 'timestampUtc' });
  }

  return (
    <section className="dashboard-view">
      {!compact && (
        <div className="summary-grid">
          <StatCard icon="👥" iconSrc={assetPaths?.statTotal} label="Total Events" value={stats.totalEvents || 0} recent={recentEntriesText(recentTotal)} />
          <StatCard icon="🎯" iconSrc={assetPaths?.statAgainstYou} label="Against You" value={stats.totalSelf || 0} recent={recentEntriesText(recentSelf)} />
          <StatCard icon="☠" iconSrc={assetPaths?.statYourCrimes} label="Your Crimes" value={stats.totalOther || 0} recent={recentEntriesText(recentOther)} />
        </div>
      )}

      <section className={`feed-card ${compact ? 'all-events-card' : 'recent-events-card'}`}>
        <div className="feed-title-row">
          <h3>{compact ? 'History' : 'Latest reports'}</h3>
        </div>
        <div className="feed-header">
          <button onClick={() => sortBy('player')}>Player</button>
          <button onClick={() => sortBy('crime')}>Crime</button>
          <button onClick={() => sortBy('organization')}>Organization</button>
          <button onClick={() => sortBy('timestampUtc')}>Time</button>
        </div>
        <div className={`events-list ${compact ? 'scrollable' : 'recent-only'}`}>
          {!events.length && <div className="empty-state">No logged events yet. Set the game folder and parse.</div>}
          {!!events.length && !visible.length && <div className="empty-state">No matching events.</div>}
          {displayed.map((event, index) => <EventRow key={`${event.player}-${event.crime}-${event.timestampUtc}-${index}`} event={event} />)}
        </div>
      </section>
    </section>
  );
}

function RankingSection({ title, rows, onSelect }) {
  const total = rows.reduce((sum, row) => sum + Number(row[1] || 0), 0);
  return (
    <section className="ranking-card">
      <h2>{title} <span>{total}</span></h2>
      {!rows.length && <p className="muted">No data yet.</p>}
      {rows.slice(0, 12).map((row, index) => (
        <button type="button" className="ranking-row" key={`${title}-${row[0]}`} onClick={() => onSelect?.(row[0])} title={`Search ${row[0]} in History`}>
          <span className="rank">{index + 1}</span>
          <strong>{row[0]}</strong>
          <span className="count">{row[1]}</span>
        </button>
      ))}
    </section>
  );
}

function StatsDashboard({ stats, onSearchHistory, assetPaths }) {
  return (
    <section className="stats-view">
      <div className="summary-grid">
        <StatCard icon="▦" iconSrc={assetPaths?.statTotal} label="Total Events" value={stats.totalEvents || 0} />
        <StatCard icon="↗" iconSrc={assetPaths?.statYourCrimes} label="Against Others" value={stats.totalOther || 0} />
        <StatCard icon="↘" iconSrc={assetPaths?.statAgainstYou} label="Against You" value={stats.totalSelf || 0} />
      </div>
      <div className="rankings-grid">
        <RankingSection title="Most Killed" rows={stats.killed || []} onSelect={onSearchHistory} />
        <RankingSection title="Killed You Most" rows={stats.killedBy || []} onSelect={onSearchHistory} />
        <RankingSection title="Most Encountered Org" rows={stats.encounteredOrg || []} onSelect={onSearchHistory} />
      </div>
    </section>
  );
}

function SettingsDashboard({ settings, onChooseFolder, onClear, onThemeChange, exitOnClose, onExitOnCloseChange }) {
  return (
    <section className="settings-view">
      <section className="settings-card wide">
        <div className="settings-card-header">
          <div>
            <h3>Game Path Set</h3>
            <p>Choose the Star Citizen LIVE/PTU folder that contains Game.log.</p>
          </div>
          <button className="primary" onClick={onChooseFolder}>{settings.gameFolderPath ? 'Change Game Folder' : 'Set Game Folder'}</button>
        </div>
        <div className="path-display" title={settings.gameFolderPath || ''}>{settings.gameFolderPath || 'No game folder selected'}</div>
      </section>

      <section className="settings-card">
        <h3>Cache Clear Set</h3>
        <p>Clear cached RSI metadata, image cache, stats cache, and scan state.</p>
        <button className="danger" onClick={onClear}>Clear Cache</button>
      </section>

      <section className="settings-card">
        <h3>Appearance</h3>
        <label>Theme</label>
        <select value={settings.themeName || 'Red'} onChange={event => onThemeChange(event.target.value)}>
          {Object.keys(THEME_COLORS).map(name => <option key={name}>{name}</option>)}
        </select>
      </section>

      <section className="settings-card">
        <h3>Window Behaviour</h3>
        <label className="toggle-row">
          <input type="checkbox" checked={exitOnClose} onChange={event => onExitOnCloseChange(event.target.checked)} />
          Exit program when closed
        </label>
        <p>When unchecked, closing the window hides CrimeScanner to the system tray.</p>
      </section>
    </section>
  );
}

function App() {
  const [settings, setSettings] = useState({ themeName: 'Red', dedupeSeconds: 60, gameFolderPath: '', eventCount: 0 });
  const [events, setEvents] = useState([]);
  const [stats, setStats] = useState(EMPTY_STATS);
  const [status, setStatus] = useState('Starting backend…');
  const [activeTab, setActiveTab] = useState('home');
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState({ key: 'timestampUtc', reverse: true });
  const [exitOnClose, setExitOnClose] = useState(() => localStorage.getItem('crimeScanner.exitOnClose') === 'true');
  const [countdown, setCountdown] = useState(SCAN_SECONDS);
  const [viewportHeight, setViewportHeight] = useState(window.innerHeight);
  const [brandSrc, setBrandSrc] = useState(brandLogo);
  const [assetPaths, setAssetPaths] = useState({});
  const [updateStatus, setUpdateStatus] = useState('');
  const [appVersion, setAppVersion] = useState('');
  const knownEventKeys = useRef(new Set());
  const initialized = useRef(false);
  const lastParseKeys = useRef(new Set());
  const parseStatusReady = useRef(false);

  function eventKey(event) {
    return `${event.player}|${event.crime}|${event.targetType}|${event.timestampUtc}`;
  }

  function gameLogUpdateStatus(nextEvents) {
    const nextKeys = new Set(nextEvents.map(eventKey));
    if (!parseStatusReady.current) {
      parseStatusReady.current = true;
      lastParseKeys.current = nextKeys;
      return nextEvents.length ? `Loaded ${nextEvents.length} entries` : 'No events found';
    }
    const added = nextEvents.filter(event => !lastParseKeys.current.has(eventKey(event))).length;
    lastParseKeys.current = nextKeys;
    if (!added) return 'No new event';
    return `${added} new event${added === 1 ? '' : 's'}`;
  }

  function maybeNotify(nextEvents) {
    const nextKeys = new Set(nextEvents.map(eventKey));
    if (!initialized.current) {
      knownEventKeys.current = nextKeys;
      initialized.current = true;
      return;
    }
    const newEvents = nextEvents.filter(event => !knownEventKeys.current.has(eventKey(event)));
    knownEventKeys.current = nextKeys;
    if (!newEvents.length || document.hasFocus()) return;
    const latest = newEvents[0];
    window.crimeScanner?.notify?.({
      title: latest.targetType === 'self' ? 'Crime committed against you' : 'Crime logged',
      body: `${latest.player}: ${latest.crime}`
    });
  }

  function ingest(payload) {
    if (!payload) return;
    if (payload.error) {
      setStatus(payload.error);
      return;
    }
    const rawEvents = Array.isArray(payload.events) ? payload.events : events;
    const nextEvents = rawEvents.map(normalizeEvent).filter(Boolean);
    const nextSettings = { ...settings, ...(payload.settings || {}), eventCount: nextEvents.length };
    const nextStats = payload.stats ? normalizeStats(payload.stats, nextEvents) : normalizeStats(stats, nextEvents);
    setSettings(nextSettings);
    setEvents(nextEvents);
    setStats(nextStats);
    applyTheme(nextSettings.themeName || 'Red');
    if (payload.status || Array.isArray(payload.events)) setStatus(payload.status || gameLogUpdateStatus(nextEvents));
    setCountdown(SCAN_SECONDS);
    if (Array.isArray(payload.events)) maybeNotify(nextEvents);
  }

  async function request(command, payload = {}) {
    const result = await window.crimeScanner.request(command, payload);
    ingest(result);
    return result;
  }

  async function updateSettings(patch) {
    setSettings(current => ({ ...current, ...patch }));
    await request('setSettings', patch);
  }

  useEffect(() => {
    let parseInFlight = false;

    async function runParse(initial = false) {
      if (parseInFlight) return;
      parseInFlight = true;
      try {
        await request('parseNow', { enrich: true, initial });
        setCountdown(SCAN_SECONDS);
      } catch (error) {
        setStatus(error.message);
      } finally {
        parseInFlight = false;
      }
    }

    applyTheme(settings.themeName || 'Red');
    window.crimeScanner.getAssetPath?.('brand.jpg').then(setBrandSrc).catch(() => setBrandSrc(brandLogo));
    Promise.all([
      ['statTotal', 'stat-total.svg'],
      ['statAgainstYou', 'stat-against-you.svg'],
      ['statYourCrimes', 'stat-your-crimes.svg'],
      ['navDashboard', 'nav-dashboard.svg'],
      ['navHistory', 'nav-history.svg'],
      ['navStatistics', 'nav-statistics.svg'],
      ['navSettings', 'nav-settings.svg']
    ].map(([key, name]) => window.crimeScanner.getAssetPath?.(name).then(url => [key, url]).catch(() => [key, null]))).then(entries => {
      setAssetPaths(Object.fromEntries(entries.filter(([, url]) => Boolean(url))));
    });
    window.crimeScanner.onReady(ingest);
    window.crimeScanner.onLog(text => setStatus(String(text || '').trim()));
    window.crimeScanner.onUpdateStatus?.(payload => {
      const statusText = payload?.status || '';
      setUpdateStatus(statusText);
      if (statusText === 'checking') setStatus('Checking for updates…');
      if (statusText === 'available') setStatus('Update available. Downloading…');
      if (statusText === 'downloading') {
        const pct = Math.round(payload?.progress?.percent || 0);
        setStatus(`Downloading update${pct ? ` ${pct}%` : '…'}`);
      }
      if (statusText === 'downloaded') setStatus('Update ready. Restart to install.');
      if (statusText === 'installing') setStatus('Restarting to install update…');
      if (statusText === 'error') setStatus(payload?.message || 'Update check failed.');
    });
    window.crimeScanner.getVersion?.().then(version => setAppVersion(version ? `v${version}` : '')).catch(() => setAppVersion(''));
    window.crimeScanner.setExitOnClose?.(exitOnClose).catch(() => {});
    request('getState')
      .then(() => runParse(true))
      .catch(error => setStatus(error.message));

    const onResize = () => setViewportHeight(window.innerHeight);
    window.addEventListener('resize', onResize);
    const interval = setInterval(() => runParse(false), SCAN_SECONDS * 1000);
    const countdownInterval = setInterval(() => setCountdown(current => current <= 1 ? SCAN_SECONDS : current - 1), 1000);
    return () => { clearInterval(interval); clearInterval(countdownInterval); window.removeEventListener('resize', onResize); };
  }, []);

  useEffect(() => {
    localStorage.setItem('crimeScanner.exitOnClose', String(exitOnClose));
    window.crimeScanner.setExitOnClose?.(exitOnClose).catch(() => {});
  }, [exitOnClose]);

  function changeTab(nextTab) {
    setActiveTab(currentTab => {
      if (currentTab === 'events' && nextTab !== 'events') {
        setSearch('');
      }
      return nextTab;
    });
  }

  function searchHistory(query) {
    setSearch(String(query || ''));
    setActiveTab('events');
  }

  const chooseFolder = async () => {
    const folder = await window.crimeScanner.chooseFolder();
    if (!folder) return;
    setSettings(current => ({ ...current, gameFolderPath: folder }));
    setStatus('Saving game folder…');
    try {
      await request('setSettings', { gameFolderPath: folder });
      setStatus('Game folder saved. Watching Game.log…');
      await request('parseNow', { enrich: true, initial: true });
    } catch (error) {
      setStatus(error?.message || 'Could not save game folder.');
    }
  };

  const page = activeTab === 'stats'
    ? <StatsDashboard stats={stats} onSearchHistory={searchHistory} assetPaths={assetPaths} />
    : activeTab === 'settings'
      ? <SettingsDashboard
          settings={settings}
          onChooseFolder={chooseFolder}
          onClear={() => request('clearCache')}
          onThemeChange={themeName => updateSettings({ themeName })}
          exitOnClose={exitOnClose}
          onExitOnCloseChange={setExitOnClose}
        />
      : <EventsDashboard events={events} stats={stats} search={search} sort={sort} setSort={setSort} compact={activeTab === 'events'} viewportHeight={viewportHeight} assetPaths={assetPaths} />;

  return (
    <div className="app-shell">
      <Sidebar activeTab={activeTab} setActiveTab={changeTab} status={status} settings={settings} countdown={countdown} brandSrc={brandSrc} assetPaths={assetPaths} />
      <main className="main-panel">
        <TopBar activeTab={activeTab} search={search} setSearch={setSearch} connection="SQLite: Connected" />
        {page}
        {appVersion ? <div className="app-version-badge" title={updateStatus ? `Update status: ${updateStatus}` : 'Current app version'}>{appVersion}</div> : null}
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
