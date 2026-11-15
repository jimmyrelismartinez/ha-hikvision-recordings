/* ── INGRESS RULE — EVERY URL IN THIS FILE IS RELATIVE ────────────────────────
 * Home Assistant serves this add-on under a rotating prefix:
 *     /api/hassio_ingress/<token>/
 * A leading slash — fetch("/api/channels") — escapes that prefix, hits HA core
 * and 404s. This is the single most common way HA add-on frontends break.
 * Use "api/channels", never "/api/channels". Same for <script>, <link>, <video>.
 * ──────────────────────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);
const chipsEl = $('camera-chips');
const dateInput = $('date');
const fromInput = $('from');
const toInput = $('to');
const form = $('search-form');
const results = $('results');
const message = $('message');
const statusEl = $('status');
const searchButton = $('search-button');
const bannersEl = $('banners');
const timelineEl = $('timeline');
const showMoreButton = $('show-more');
const playerPanel = $('player-panel');
const playerBody = $('player-body');
const playerCamera = $('player-camera');
const playerTime = $('player-time');
const prevButton = $('prev-clip');
const nextButton = $('next-clip');
const autoplayInput = $('autoplay');
const closeButton = $('player-close');
const viewHome = $('view-home');
const viewCamera = $('view-camera');
const viewAll = $('view-all');
const recentStrip = $('recent-strip');
const recentEmpty = $('recent-empty');
const cameraTiles = $('camera-tiles');
const cameraTitle = $('camera-title');
const liveButton = $('live-button');
const dateChip = $('date-chip');
const dateJump = $('date-jump');
const eventFiltersEl = $('event-filters');
const vtimelineEl = $('vtimeline');
const daySentinel = $('day-sentinel');
const cameraEnd = $('camera-end');
const loadEarlierBtn = $('load-earlier');
const refreshTodayBtn = $('refresh-today');
const stageLive = $('stage-live');
const stageImage = $('stage-image');
const stageLabel = $('stage-label');
const stagePaused = $('stage-paused');
const tabCameras = $('tab-cameras');
const tabAll = $('tab-all');

const pad = (n) => String(n).padStart(2, '0');
const localDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const localTime = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const clockTime = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
// Timeline, Recent and cards: minutes are enough and keep narrow phone columns on one line.
const shortTime = (d) => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(String(s))
  : String(s).replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`);

const PAGE_ROWS = 200;
const EVENT_VOCAB = ['motion', 'person', 'vehicle', 'animal', 'face', 'doorbell', 'scheduled', 'alarm'];
// Every vocabulary type that occurs gets a chip (design §10.8); All is separate.
const FILTER_CHIPS = ['person', 'vehicle', 'animal', 'face', 'doorbell', 'motion', 'scheduled', 'alarm'];
const IDLE_MS = 2 * 60 * 1000;
const SNAP_HOME_MS = 60 * 1000;
const SNAP_CAMERA_MS = 5 * 1000;
const RECENT_STALE_MS = 60 * 1000;
const MAX_THUMB_INFLIGHT = 2;
const GAP_BAND_MIN_S = 120;
const EMPTY_DAY_STOP = 7;
const MAX_DAYS_BACK = 30;

/* ── Persisted preferences ─────────────────────────────────────────────────── */
const STORE = {
  cameras: 'dvr-rec.cameras',
  autoplay: 'dvr-rec.autoplay',
  quality: 'dvr-rec.quality',
  filter: (key) => `dvr-rec.filter.${key}`,
};
function loadPref(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (err) { return fallback; }
}
function savePref(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (err) { /* ignore */ }
}

function showMessage(text, kind = 'info') {
  if (!message) return;
  message.textContent = text;
  message.className = `message ${kind}`;
  message.hidden = !text;
}

function formatSize(bytes) {
  if (!bytes) return '';
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`;
}

function formatDuration(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${pad(s)}s` : `${s}s`;
}

function formatGap(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h) return m ? `${h} h ${m} m` : `${h} h`;
  return `${Math.max(1, m)} m`;
}

function formatAgo(iso) {
  const ms = Date.now() - Date.parse(iso);
  if (!(ms >= 0)) return '—';
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}

function dayLabel(dayStr) {
  const today = localDate(new Date());
  const yest = localDate(new Date(Date.now() - 86400000));
  if (dayStr === today) return 'Today';
  if (dayStr === yest) return 'Yesterday';
  const d = new Date(`${dayStr}T12:00:00`);
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
}

function startOfLocalDay(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}

function endOfLocalDay(d) {
  const x = new Date(d);
  x.setHours(23, 59, 59, 999);
  return x;
}

function addLocalDays(d, n) {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

/* ── Cameras ──────────────────────────────────────────────────────────────── */
let cameras = [];                 // [{key, name, kind, snapshot}]
let selectedCameras = new Set();  // empty = all
let healthDevices = [];           // from api/health
let route = { name: 'home' };     // {name, key?, t?}
let lastInteraction = Date.now();

function noteInteraction() {
  lastInteraction = Date.now();
  if (snapState.paused) resumeSnapshots();
}
['pointerdown', 'keydown', 'touchstart', 'scroll'].forEach((ev) => {
  document.addEventListener(ev, noteInteraction, { passive: true, capture: true });
});

function cameraName(key) {
  const cam = cameras.find((c) => c.key === key);
  return cam ? cam.name : key;
}

function devicePrefixForCamera(key) {
  if (String(key).startsWith('dvr-')) return 'dvr';
  return String(key);
}

function deviceDown(cameraKey) {
  const prefix = devicePrefixForCamera(cameraKey);
  const d = healthDevices.find((x) => x.prefix === prefix);
  return d ? d.state !== 'ok' : false;
}

function renderChips() {
  if (!chipsEl) return;
  chipsEl.innerHTML = '';
  const make = (label, key, pressed) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = label;
    chip.dataset.camera = key;
    chip.setAttribute('aria-pressed', String(pressed));
    chip.addEventListener('click', () => toggleCamera(key));
    chipsEl.appendChild(chip);
  };
  make('All', 'all', selectedCameras.size === 0);
  for (const cam of cameras) make(cam.name, cam.key, selectedCameras.has(cam.key));
}

function toggleCamera(key) {
  if (key === 'all') {
    selectedCameras.clear();
  } else if (selectedCameras.has(key)) {
    selectedCameras.delete(key);
  } else {
    selectedCameras.add(key);
  }
  if (selectedCameras.size === cameras.length) selectedCameras.clear();
  savePref(STORE.cameras, [...selectedCameras]);
  renderChips();
}

function selectedKeys() {
  return selectedCameras.size ? [...selectedCameras] : cameras.map((c) => c.key);
}

async function loadChannels() {
  const response = await fetch('api/channels');
  if (!response.ok) throw new Error('Could not load the camera list.');
  const channels = await response.json();
  cameras = channels.map((c) => ({
    key: String(c.key ?? c.id),
    name: c.name,
    kind: c.kind || 'dvr',
    snapshot: !!c.snapshot,
  }));
  const saved = loadPref(STORE.cameras, []);
  const known = new Set(cameras.map((c) => c.key));
  selectedCameras = new Set(Array.isArray(saved) ? saved.filter((k) => known.has(k)) : []);
  if (selectedCameras.size === cameras.length) selectedCameras.clear();
  renderChips();
}

async function loadHealth() {
  try {
    const health = await (await fetch('api/health')).json();
    healthDevices = Array.isArray(health.devices) ? health.devices : [];
    const devices = healthDevices.length
      ? healthDevices
      : [{ name: 'DVR', state: health.dvr, prefix: 'dvr' }];
    const down = devices.filter((d) => d.state !== 'ok');
    if (!down.length) {
      statusEl.textContent = devices.length === 1 ? `${devices[0].name} online` : 'All devices online';
      statusEl.className = 'status ok';
    } else {
      statusEl.textContent = down.map((d) => (d.state === 'auth_failed'
        ? `${d.name} rejected the credentials`
        : `${d.name} unreachable`)).join(' · ');
      statusEl.className = 'status bad';
    }
    if (typeof health.client_remux_max_mb === 'number' && health.client_remux_max_mb > 0) {
      CLIENT_REMUX_MAX_BYTES = health.client_remux_max_mb * 1024 * 1024;
    }
  } catch (err) {
    statusEl.textContent = '';
  }
}

function applyPreset(preset) {
  const now = new Date();
  if (preset === '1h' || preset === '6h') {
    const hours = preset === '1h' ? 1 : 6;
    const start = new Date(now.getTime() - hours * 3600 * 1000);
    dateInput.value = localDate(start);
    fromInput.value = localTime(start);
    toInput.value = localTime(now);
  } else if (preset === 'today') {
    dateInput.value = localDate(now);
    fromInput.value = '00:00';
    toInput.value = '23:59';
  } else if (preset === 'yesterday') {
    const yesterday = new Date(now.getTime() - 86400 * 1000);
    dateInput.value = localDate(yesterday);
    fromInput.value = '00:00';
    toInput.value = '23:59';
  }
}

/* ── Lazy thumbnails — queue, max 2 in flight, reuse IntersectionObserver ──── */
const thumbQueue = [];            // { img, priority }
let thumbInflight = 0;

function bumpThumbPriority(img) {
  const i = thumbQueue.findIndex((q) => q.img === img);
  if (i >= 0) {
    const item = thumbQueue.splice(i, 1)[0];
    item.priority = Date.now();
    thumbQueue.unshift(item);
  }
}

function enqueueThumb(img) {
  if (img.dataset.queued === '1' || img.dataset.loaded === '1') return;
  img.dataset.queued = '1';
  thumbQueue.push({ img, priority: Date.now() });
  pumpThumbs();
}

function dropThumbIfQueued(img) {
  const i = thumbQueue.findIndex((q) => q.img === img);
  if (i >= 0) {
    thumbQueue.splice(i, 1);
    delete img.dataset.queued;
  }
}

async function fetchThumbInto(img) {
  const url = img.dataset.src;
  if (!url) return;
  try {
    let response = await fetch(url);
    if (response.status === 410 && img.dataset.refreshKey) {
      const refreshed = await refreshClipByKey(img.dataset.refreshKey);
      if (refreshed) {
        img.dataset.src = `api/thumbnail/${refreshed.id}`;
        response = await fetch(img.dataset.src);
      }
    }
    if (!response.ok) throw new Error('thumb failed');
    const blob = await response.blob();
    if (img.dataset.loaded === '1') return;
    const objectUrl = URL.createObjectURL(blob);
    img.onload = () => {
      img.parentElement && img.parentElement.classList.add('loaded');
      URL.revokeObjectURL(objectUrl);
    };
    img.src = objectUrl;
    img.dataset.loaded = '1';
  } catch (err) {
    img.parentElement && img.parentElement.classList.add('failed');
  }
}

function pumpThumbs() {
  while (thumbInflight < MAX_THUMB_INFLIGHT && thumbQueue.length) {
    // Newest-visible first (higher priority = more recent enqueue/bump).
    thumbQueue.sort((a, b) => b.priority - a.priority);
    const { img } = thumbQueue.shift();
    delete img.dataset.queued;
    if (!img.isConnected) continue;
    thumbInflight++;
    fetchThumbInto(img).finally(() => {
      thumbInflight--;
      pumpThumbs();
    });
  }
}

const thumbObserver = ('IntersectionObserver' in window)
  ? new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const img = entry.target;
        if (entry.isIntersecting) {
          bumpThumbPriority(img);
          enqueueThumb(img);
        } else {
          // Left viewport before its request started — drop from queue.
          dropThumbIfQueued(img);
        }
      }
    }, { rootMargin: '200px' })
  : null;

function attachThumb(item, clip, opts = {}) {
  const thumb = document.createElement('div');
  thumb.className = 'clip-thumb';
  const img = document.createElement('img');
  img.alt = '';
  img.dataset.src = `api/thumbnail/${clip.id}`;
  img.dataset.refreshKey = clipKey(clip);
  img.addEventListener('error', () => thumb.classList.add('failed'));
  thumb.appendChild(img);
  if (opts.prepend !== false) item.prepend(thumb);
  else item.appendChild(thumb);
  if (thumbObserver) {
    thumbObserver.observe(img);
  } else {
    enqueueThumb(img);
  }
  return thumb;
}

/* ── PLAYBACK: client-side remux, with the server path as the safety net ────── */
let CLIENT_REMUX_MAX_BYTES = 128 * 1024 * 1024;

const QUALITIES = ['sd', 'hd'];
const DEFAULT_QUALITY = 'sd';

let ffmpegPromise = null;

function ffmpegAvailable() {
  return typeof FFmpegWASM !== 'undefined' && typeof WebAssembly !== 'undefined';
}

async function getFFmpeg() {
  if (!ffmpegPromise) {
    ffmpegPromise = (async () => {
      const { FFmpeg } = FFmpegWASM;
      const ff = new FFmpeg();
      await ff.load({
        coreURL: new URL('vendor/ffmpeg-core.js', document.baseURI).href,
        wasmURL: new URL('vendor/ffmpeg-core.wasm', document.baseURI).href,
      });
      return ff;
    })().catch((err) => {
      ffmpegPromise = null;
      throw err;
    });
  }
  return ffmpegPromise;
}

let remuxChain = Promise.resolve();
function serialise(task) {
  const run = remuxChain.then(task, task);
  remuxChain = run.catch(() => {});
  return run;
}

async function remuxInBrowser(clip, quality, signal) {
  const ff = await getFFmpeg();
  const response = await fetch(`api/stream-raw/${clip.id}?quality=${quality}`, { signal });
  if (!response.ok) {
    let detail = `the add-on returned HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (err) { /* not JSON */ }
    const err = new Error(detail);
    err.status = response.status;
    throw err;
  }
  const raw = new Uint8Array(await response.arrayBuffer());
  if (!raw.length) throw new Error('the DVR returned an empty clip');

  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const input = `in-${stamp}.ps`;
  const output = `out-${stamp}.mp4`;
  try {
    await ff.writeFile(input, raw);
    const code = await ff.exec(['-hide_banner', '-loglevel', 'error',
                                '-i', input, '-c', 'copy', '-f', 'mp4', output]);
    if (code !== 0) throw new Error(`ffmpeg.wasm exited ${code}`);
    const data = await ff.readFile(output);
    if (!data || !data.length) throw new Error('ffmpeg.wasm produced no output');
    return new Blob([data.buffer], { type: 'video/mp4' });
  } finally {
    for (const path of [input, output]) {
      try { await ff.deleteFile(path); } catch (err) { /* never existed */ }
    }
  }
}

function clientRemuxBlocker(clip) {
  if (!ffmpegAvailable()) return 'ffmpeg.wasm not loaded in this browser';
  if (clip.size_bytes > CLIENT_REMUX_MAX_BYTES) {
    return `clip is ${formatSize(clip.size_bytes)}, over the ${formatSize(CLIENT_REMUX_MAX_BYTES)} in-browser limit`;
  }
  return null;
}

function setMode(badge, mode, detail) {
  badge.dataset.mode = mode;
  badge.textContent = {
    working: 'Remuxing in browser…',
    fast: 'Fast · browser remux',
    compat: 'Compatibility · server remux',
    direct: 'Direct · native MP4',
    failed: 'Playback failed',
  }[mode] || mode;
  badge.title = detail || '';
}

/* ── 410 retry by clipKey ─────────────────────────────────────────────────── */
async function refreshClipByKey(key) {
  const [cameraKey, startMs] = String(key).split('|');
  const start = new Date(Number(startMs));
  if (!cameraKey || !Number.isFinite(start.getTime())) return null;
  const day0 = startOfLocalDay(start);
  const day1 = endOfLocalDay(start);
  try {
    const params = new URLSearchParams({
      camera: cameraKey,
      start: day0.toISOString(),
      end: day1.toISOString(),
    });
    const response = await fetch(`api/recordings?${params}`);
    if (!response.ok) return null;
    const body = await response.json();
    const found = (body.clips || []).find((c) => clipKey(c) === key);
    if (found && camState.key === cameraKey) {
      // Update in-memory day lists so Later/Earlier keep working.
      for (const day of camState.days) {
        const i = day.clips.findIndex((c) => clipKey(c) === key);
        if (i >= 0) day.clips[i] = found;
      }
      rebuildCamClips();
    }
    return found || null;
  } catch (err) {
    return null;
  }
}

function buildPlayer(clip, body, opts = {}) {
  let quality = QUALITIES.includes(opts.quality) ? opts.quality : DEFAULT_QUALITY;
  let blobUrl = null;
  let generation = 0;
  let inflight = null;
  let activeClip = clip;
  let retried410 = false;

  const video = opts.video || document.createElement('video');
  video.controls = true;
  video.playsInline = true;
  video.setAttribute('playsinline', '');
  video.preload = 'none';
  video.onended = () => { if (opts.onEnded) opts.onEnded(); };

  const bar = document.createElement('div');
  bar.className = 'clip-controls';

  const picker = document.createElement('div');
  picker.className = 'quality';
  picker.setAttribute('role', 'group');
  picker.setAttribute('aria-label', 'Quality');
  const buttons = new Map();
  for (const q of QUALITIES) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = q.toUpperCase();
    button.title = q === 'sd'
      ? 'Substream — smaller and faster to load'
      : 'Mainstream — full resolution';
    button.addEventListener('click', () => {
      if (quality === q) return;
      quality = q;
      for (const [key, el] of buttons) {
        el.classList.toggle('on', key === quality);
        el.setAttribute('aria-pressed', String(key === quality));
      }
      updateDownload();
      if (opts.onQuality) opts.onQuality(quality);
      play();
    });
    buttons.set(q, button);
    picker.appendChild(button);
  }
  for (const [key, el] of buttons) {
    el.classList.toggle('on', key === quality);
    el.setAttribute('aria-pressed', String(key === quality));
  }

  const badge = document.createElement('span');
  badge.className = 'mode-badge';

  bar.append(picker, badge);

  const hint = document.createElement('p');
  hint.className = 'hint';

  const download = document.createElement('a');
  download.className = 'download';
  download.setAttribute('download', '');
  function updateDownload() {
    download.href = `api/download/${activeClip.id}?quality=${quality}`;
    download.textContent = `Download ${quality.toUpperCase()}`;
  }
  updateDownload();

  // 410 on download click: refresh once by clipKey.
  download.addEventListener('click', async (ev) => {
    // Let the browser navigate for the happy path; only intercept after a prior hint.
  });

  bar.append(download);
  if (video.parentNode === body) {
    body.insertBefore(bar, video);
    video.after(hint);
  } else {
    body.append(bar, video, hint);
  }

  function freeBlob() {
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
  }

  function useServerPath(reason) {
    setMode(badge, 'compat', reason);
    hint.textContent = "Seeking isn't supported on this path — download the clip to scrub.";
    video.src = `api/stream/${activeClip.id}?quality=${quality}`;
    video.play().catch(() => {});
  }

  function cancelInflight() {
    if (inflight) { inflight.abort(); inflight = null; }
  }

  async function handle410() {
    if (retried410) {
      setMode(badge, 'failed', 'clip gone');
      hint.textContent = 'This clip is no longer available — refresh';
      return false;
    }
    retried410 = true;
    const fresh = await refreshClipByKey(clipKey(activeClip));
    if (!fresh) {
      setMode(badge, 'failed', 'clip gone');
      hint.textContent = 'This clip is no longer available — refresh';
      return false;
    }
    activeClip = fresh;
    if (current && clipKey(current.clip) === clipKey(fresh)) current.clip = fresh;
    updateDownload();
    return true;
  }

  async function play() {
    const mine = ++generation;
    const q = quality;
    cancelInflight();
    freeBlob();

    if (activeClip.playback === 'direct') {
      setMode(badge, 'direct', 'camera MP4 proxied as-is — seeking works');
      hint.textContent = '';
      const src = `api/stream/${activeClip.id}?quality=${q}`;
      video.onerror = async () => {
        // Best-effort 410 detection via a HEAD-less re-fetch probe is heavy;
        // stream errors after a registry eviction surface as play failure — retry once.
        video.onerror = null;
        if (await handle410()) play();
      };
      video.src = src;
      video.play().catch(() => {});
      return;
    }

    const blocker = clientRemuxBlocker(activeClip);
    if (blocker) {
      useServerPath(blocker);
      return;
    }

    setMode(badge, 'working', 'fetching raw clip and remuxing with ffmpeg.wasm');
    const controller = new AbortController();
    inflight = controller;
    try {
      const blob = await serialise(() => (mine !== generation
        ? Promise.reject(new Error('stale'))
        : remuxInBrowser(activeClip, q, controller.signal)));
      if (mine !== generation) return;
      inflight = null;
      blobUrl = URL.createObjectURL(blob);
      video.src = blobUrl;
      setMode(badge, 'fast', 'remuxed in-browser by ffmpeg.wasm — seeking works');
      hint.textContent = '';
      video.play().catch(() => {});
    } catch (err) {
      if (mine !== generation) return;
      if (err && err.status === 410) {
        if (await handle410()) {
          play();
          return;
        }
        return;
      }
      console.warn('client remux failed; using the server path', err);
      useServerPath(`client remux failed: ${err && err.message ? err.message : err}`);
    }
  }

  function dispose() {
    generation++;
    cancelInflight();
    video.onended = null;
    video.onerror = null;
    try { video.pause(); } catch (err) { /* not started */ }
    video.removeAttribute('src');
    try { video.load(); } catch (err) { /* aborts the old request */ }
    freeBlob();
  }

  play();
  return { video, dispose, getClip: () => activeClip };
}

/* ── Page state (All recordings + shared player) ──────────────────────────── */
let clipsNewest = [];
let clipsAsc = [];
let ascIndexByKey = new Map();
let rowsShown = 0;
let current = null;               // { clip, player }
const savedQuality = loadPref(STORE.quality, DEFAULT_QUALITY);
let preferredQuality = QUALITIES.includes(savedQuality) ? savedQuality : DEFAULT_QUALITY;
const panelVideo = document.createElement('video');

const clipKey = (c) => `${c.camera_key}|${Date.parse(c.start)}`;
const byTime = (a, b) => (Date.parse(a.start) - Date.parse(b.start))
  || String(a.camera_key).localeCompare(String(b.camera_key));

function setClips(clips) {
  clipsNewest = clips.slice();
  clipsAsc = clips.slice().sort((a, b) => byTime(a, b) || String(a.id).localeCompare(String(b.id)));
  ascIndexByKey = new Map(clipsAsc.map((c, i) => [clipKey(c), i]));
}

function navClipsAsc() {
  // On the camera page, Earlier/Later walk that camera's loaded (filtered) clips.
  if (route.name === 'camera') return camNavClips();
  return clipsAsc;
}

function neighbourIndex(direction) {
  if (!current) return -1;
  const list = navClipsAsc();
  const map = new Map(list.map((c, i) => [clipKey(c), i]));
  const i = map.get(clipKey(current.clip));
  if (i !== undefined) {
    const j = i + direction;
    return j >= 0 && j < list.length ? j : -1;
  }
  if (direction > 0) return list.findIndex((c) => byTime(c, current.clip) > 0);
  for (let j = list.length - 1; j >= 0; j--) if (byTime(list[j], current.clip) < 0) return j;
  return -1;
}

function updateNav() {
  const earlier = neighbourIndex(-1);
  const later = neighbourIndex(1);
  prevButton.disabled = earlier < 0 && !(route.name === 'camera' && camState.canLoadOlder);
  nextButton.disabled = later < 0;
}

function markActive(key) {
  // Only row/segment containers — not thumbnail <img>s (those carry data-refresh-key).
  for (const el of document.querySelectorAll('.clip[data-clip-key], .tl-seg[data-clip-key], .vrow[data-clip-key], .recent-card[data-clip-key]')) {
    const on = !!key && el.dataset.clipKey === key;
    el.classList.toggle('active', on);
    if (on) el.setAttribute('aria-current', 'true'); else el.removeAttribute('aria-current');
  }
}

function showPlayingChrome() {
  if (stageLive) stageLive.hidden = true;
  if (playerBody) playerBody.hidden = false;
  if (liveButton && route.name === 'camera') liveButton.hidden = false;
  if (closeButton) closeButton.hidden = route.name === 'camera';
  const nav = playerPanel && playerPanel.querySelector('.player-nav');
  if (nav) nav.hidden = false;
}

function showStageChrome() {
  if (playerBody) {
    for (const child of [...playerBody.childNodes]) if (child !== panelVideo) child.remove();
    playerBody.hidden = true;
  }
  if (stageLive) stageLive.hidden = false;
  if (liveButton) liveButton.hidden = true;
  if (closeButton) closeButton.hidden = true;
  const nav = playerPanel && playerPanel.querySelector('.player-nav');
  if (nav) nav.hidden = true;
}

function openClip(indexOrClip) {
  let clip = null;
  if (typeof indexOrClip === 'number') {
    const list = navClipsAsc();
    clip = list[indexOrClip];
  } else if (indexOrClip && typeof indexOrClip === 'object') {
    clip = indexOrClip;
  }
  if (!clip) return;
  if (current) current.player.dispose();
  for (const child of [...playerBody.childNodes]) if (child !== panelVideo) child.remove();
  playerPanel.hidden = false;
  showPlayingChrome();
  const start = new Date(clip.start);
  playerCamera.textContent = clip.camera_name || clip.channel_name || cameraName(clip.camera_key);
  playerTime.textContent = `${start.toLocaleDateString()} ${clockTime(start)} · ${formatDuration(clip.duration_s)}`;
  const player = buildPlayer(clip, playerBody, {
    video: panelVideo,
    quality: preferredQuality,
    onQuality: (q) => { preferredQuality = q; savePref(STORE.quality, q); },
    onEnded: () => {
      if (!autoplayInput.checked) return;
      const next = neighbourIndex(1);
      if (next >= 0) openClip(next);
      else if (route.name === 'camera') returnToLive();
    },
  });
  current = { clip, player };
  updateNav();
  markActive(clipKey(clip));
  // Scroll the camera-timeline card into view when Earlier/Later/Autoplay moves.
  if (route.name === 'camera') {
    const el = vtimelineEl && vtimelineEl.querySelector(`[data-clip-key="${cssEsc(clipKey(clip))}"]`);
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function step(direction) {
  const j = neighbourIndex(direction);
  if (j >= 0) {
    openClip(j);
    return;
  }
  if (direction < 0 && route.name === 'camera' && camState.canLoadOlder && !camState.loading) {
    camState.pendingEarlier = true;
    loadNextOlderDay();
  }
}

function closePlayer() {
  if (current) current.player.dispose();
  current = null;
  for (const child of [...playerBody.childNodes]) if (child !== panelVideo) child.remove();
  if (route.name === 'camera') {
    showStageChrome();
    updateStageForCamera();
  } else {
    playerPanel.hidden = true;
    playerBody.hidden = false;
    if (stageLive) stageLive.hidden = true;
  }
  markActive(null);
}

function returnToLive() {
  closePlayer();
}

/* ── List (All recordings) ────────────────────────────────────────────────── */
function eventBadges(clip) {
  const wrap = document.createElement('span');
  wrap.className = 'event-badges';
  for (const ev of (clip.events || [])) {
    const b = document.createElement('span');
    b.className = 'event-badge';
    b.textContent = ev;
    wrap.appendChild(b);
  }
  return wrap;
}

function renderClip(clip) {
  const item = document.createElement('li');
  item.className = 'clip';
  item.dataset.clipId = clip.id;
  item.dataset.clipKey = clipKey(clip);

  const start = new Date(clip.start);
  const header = document.createElement('button');
  header.type = 'button';
  header.className = 'clip-header';

  const timeSpan = document.createElement('span');
  timeSpan.className = 'clip-time';
  timeSpan.textContent = clockTime(start);

  const metaSpan = document.createElement('span');
  metaSpan.className = 'clip-meta';
  metaSpan.textContent = `${formatDuration(clip.duration_s)} · ${formatSize(clip.size_bytes)}`;

  const chipSpan = document.createElement('span');
  chipSpan.className = 'clip-chip';
  chipSpan.textContent = clip.camera_name || clip.channel_name || cameraName(clip.camera_key);

  header.append(timeSpan, metaSpan, eventBadges(clip), chipSpan);
  header.addEventListener('click', () => {
    // Prefer camera-page nav list when on that route; else All-recordings list.
    if (route.name === 'camera') {
      openClip(clip);
    } else {
      const index = ascIndexByKey.get(clipKey(clip));
      if (index !== undefined) openClip(index);
    }
  });

  item.append(header);
  attachThumb(item, clip);
  item.querySelector('.clip-thumb').addEventListener('click', () => header.click());
  return item;
}

function renderMoreRows() {
  const next = clipsNewest.slice(rowsShown, rowsShown + PAGE_ROWS);
  for (const clip of next) results.appendChild(renderClip(clip));
  rowsShown += next.length;
  const left = clipsNewest.length - rowsShown;
  showMoreButton.hidden = left <= 0;
  showMoreButton.textContent = `Show more (${left} left)`;
  if (current) markActive(clipKey(current.clip));
}

const TICK_STEPS_MIN = [5, 10, 15, 30, 60, 120, 180, 360];

function renderTimeline(windowStart, windowEnd, laneKeys) {
  timelineEl.innerHTML = '';
  const t0 = windowStart.getTime();
  const span = windowEnd.getTime() - t0;
  if (!(span > 0)) { timelineEl.hidden = true; return; }
  const pct = (t) => `${Math.min(100, Math.max(0, ((t - t0) / span) * 100))}%`;

  const stepMin = TICK_STEPS_MIN.find((m) => span / (m * 60000) <= 8) || 720;
  const axis = document.createElement('div');
  axis.className = 'tl-row tl-axis';
  axis.innerHTML = '<span class="tl-label"></span>';
  const axisTrack = document.createElement('div');
  axisTrack.className = 'tl-track';
  const tick = new Date(t0);
  tick.setSeconds(0, 0);
  const minutes = tick.getHours() * 60 + tick.getMinutes();
  tick.setHours(0, Math.ceil(minutes / stepMin) * stepMin);
  for (; tick.getTime() <= t0 + span; tick.setMinutes(tick.getMinutes() + stepMin)) {
    const mark = document.createElement('span');
    mark.className = 'tl-tick';
    const at = (tick.getTime() - t0) / span;
    mark.style.left = pct(tick.getTime());
    if (at < 0.04) mark.classList.add('edge-start');
    if (at > 0.96) mark.classList.add('edge-end');
    mark.textContent = localTime(tick);
    axisTrack.appendChild(mark);
  }
  axis.appendChild(axisTrack);
  timelineEl.appendChild(axis);

  const now = Date.now();
  for (const key of laneKeys) {
    const row = document.createElement('div');
    row.className = 'tl-row';
    row.dataset.camera = key;
    const label = document.createElement('span');
    label.className = 'tl-label';
    label.textContent = cameraName(key);
    const track = document.createElement('div');
    track.className = 'tl-track';
    clipsAsc.forEach((clip, index) => {
      if (String(clip.camera_key) !== key) return;
      const s = Math.max(Date.parse(clip.start), t0);
      const e = Math.min(Date.parse(clip.end), t0 + span);
      const seg = document.createElement('button');
      seg.type = 'button';
      seg.className = 'tl-seg';
      seg.dataset.clipId = clip.id;
      seg.dataset.clipKey = clipKey(clip);
      seg.tabIndex = -1;
      seg.style.left = pct(s);
      seg.style.width = `${Math.max(0, ((e - s) / span) * 100)}%`;
      const when = clockTime(new Date(clip.start));
      const ev = (clip.events || []).join(', ');
      seg.title = `${cameraName(key)} ${when} · ${formatDuration(clip.duration_s)}${ev ? ` · ${ev}` : ''}`;
      seg.setAttribute('aria-label', `Play ${seg.title}`);
      seg.addEventListener('click', () => openClip(index));
      track.appendChild(seg);
    });
    if (now >= t0 && now <= t0 + span) {
      const marker = document.createElement('span');
      marker.className = 'tl-now';
      marker.style.left = pct(now);
      marker.title = 'Now';
      track.appendChild(marker);
    }
    row.append(label, track);
    timelineEl.appendChild(row);
  }
  timelineEl.hidden = false;
}

function showSkeletons() {
  results.innerHTML = '';
  for (let i = 0; i < 6; i++) {
    const li = document.createElement('li');
    li.className = 'clip skeleton';
    li.setAttribute('aria-hidden', 'true');
    li.innerHTML = '<div class="clip-thumb"></div><div class="clip-header"><span class="bar"></span></div>';
    results.appendChild(li);
  }
}

function clearResults() {
  results.innerHTML = '';
  setClips([]);
  rowsShown = 0;
  showMoreButton.hidden = true;
  timelineEl.hidden = true;
  if (current) updateNav();
}

function renderBanners(errors) {
  bannersEl.innerHTML = '';
  for (const err of errors || []) {
    const banner = document.createElement('p');
    banner.className = 'banner bad';
    banner.textContent = `${cameraName(err.camera)}: ${err.detail}`;
    bannersEl.appendChild(banner);
  }
}

if (form) {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    searchButton.disabled = true;
    showSkeletons();
    renderBanners([]);
    timelineEl.hidden = true;
    showMoreButton.hidden = true;

    const start = new Date(`${dateInput.value}T${fromInput.value}:00`);
    const end = new Date(`${dateInput.value}T${toInput.value}:59`);
    if (end <= start) {
      end.setDate(end.getDate() + 1);
    }
    showMessage(`Searching ${localDate(start)} ${localTime(start)} → ${localDate(end)} ${localTime(end)}…`);

    const laneKeys = selectedKeys();
    const params = new URLSearchParams();
    if (selectedCameras.size) {
      for (const key of selectedCameras) params.append('camera', key);
    } else {
      params.append('camera', 'all');
    }
    params.append('start', start.toISOString());
    params.append('end', end.toISOString());

    try {
      const response = await fetch(`api/recordings?${params.toString()}`);
      const body = await response.json();
      results.innerHTML = '';
      if (!response.ok) {
        clearResults();
        showMessage(body.detail || 'Search failed.', 'bad');
        return;
      }
      const clips = body.clips || [];
      renderBanners(body.errors);
      setClips(clips);
      rowsShown = 0;
      for (const clip of clips) {
        if (!laneKeys.includes(String(clip.camera_key))) laneKeys.push(String(clip.camera_key));
      }
      if (current) { updateNav(); markActive(clipKey(current.clip)); }
      if (!clips.length) {
        const names = laneKeys.map(cameraName).join(', ');
        showMessage(`No recordings for ${names} between ${fromInput.value} and ${toInput.value}. Try a wider range.`);
        return;
      }
      renderTimeline(start, end, laneKeys);
      renderMoreRows();
      const count = `${clips.length} clip${clips.length === 1 ? '' : 's'}`;
      if (body.truncated) {
        const which = (body.truncated_cameras || []).map(cameraName).join(', ');
        showMessage(`${count} — ${which || 'a camera'} hit the search limit; narrow the time range to see everything.`);
      } else {
        showMessage(`${count}.`);
      }
    } catch (err) {
      clearResults();
      showMessage('Could not reach the add-on. Is it running?', 'bad');
    } finally {
      searchButton.disabled = false;
    }
  });
}

/* ── Keyboard ─────────────────────────────────────────────────────────────── */
function toggleFullscreen(video) {
  const doc = document;
  if (doc.fullscreenElement || doc.webkitFullscreenElement) {
    const exit = doc.exitFullscreen || doc.webkitExitFullscreen;
    if (exit) Promise.resolve(exit.call(doc)).catch(() => {});
    return;
  }
  if (video.requestFullscreen) {
    Promise.resolve(video.requestFullscreen()).catch(() => {});
  } else if (video.webkitRequestFullscreen) {
    video.webkitRequestFullscreen();
  } else if (video.webkitEnterFullscreen) {
    video.webkitEnterFullscreen();
  }
}

function handleKey(event) {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  const inside = (sel) => target && target.closest && target.closest(sel);
  if (inside('input, select, textarea, [contenteditable=""], [contenteditable="true"]')) return;
  if (!current) return;
  const video = current.player.video;
  const onVideo = target === video;
  switch (event.key) {
    case ' ':
    case 'Spacebar':
      if (onVideo || inside('button, a')) return;
      if (video.paused) video.play().catch(() => {}); else video.pause();
      break;
    case 'ArrowLeft':
    case 'ArrowRight': {
      if (onVideo) return;
      const delta = errKeyLeft(event.key);
      let t = (video.currentTime || 0) + delta;
      if (Number.isFinite(video.duration)) t = Math.min(t, video.duration);
      video.currentTime = Math.max(0, t);
      break;
    }
    case 'j':
    case 'J':
      step(-1);
      break;
    case 'k':
    case 'K':
      step(1);
      break;
    case 'f':
    case 'F':
      toggleFullscreen(video);
      break;
    default:
      return;
  }
  event.preventDefault();
}
function errKeyLeft(key) { return key === 'ArrowLeft' ? -5 : 5; }
document.addEventListener('keydown', handleKey);

/* ── Snapshots (scheduled after load/fail; pause when hidden/idle) ────────── */
const snapState = {
  timer: null,
  paused: false,
  fails: new Map(),     // cameraKey -> consecutive failures
  lastUrl: new Map(),   // cameraKey -> object URL or src
};

function snapshotsAllowed() {
  if (document.visibilityState !== 'visible') return false;
  if (Date.now() - lastInteraction > IDLE_MS) return false;
  if (route.name === 'home' || route.name === 'camera') return true;
  return false;
}

function clearSnapTimer() {
  if (snapState.timer) { clearTimeout(snapState.timer); snapState.timer = null; }
}

function pauseSnapshots(reason) {
  clearSnapTimer();
  snapState.paused = true;
  if (stagePaused) {
    stagePaused.hidden = false;
    stagePaused.textContent = 'Paused · tap to resume';
  }
  document.querySelectorAll('.tile-paused').forEach((el) => { el.hidden = false; });
}

function resumeSnapshots() {
  snapState.paused = false;
  if (stagePaused) stagePaused.hidden = true;
  document.querySelectorAll('.tile-paused').forEach((el) => { el.hidden = true; });
  scheduleSnapshots();
}

function scheduleSnapshots() {
  clearSnapTimer();
  if (snapState.paused) return;
  if (!snapshotsAllowed()) {
    if (document.visibilityState !== 'visible' || Date.now() - lastInteraction > IDLE_MS) {
      pauseSnapshots();
    }
    return;
  }
  const delay = route.name === 'camera' ? SNAP_CAMERA_MS : SNAP_HOME_MS;
  snapState.timer = setTimeout(runSnapshotCycle, delay);
}

async function loadSnapshot(cameraKey) {
  const cam = cameras.find((c) => c.key === cameraKey);
  if (!cam || !cam.snapshot) return null;
  try {
    const response = await fetch(`api/snapshot/${cameraKey}?t=${Date.now()}`);
    if (!response.ok) throw new Error(String(response.status));
    const blob = await response.blob();
    snapState.fails.set(cameraKey, 0);
    return URL.createObjectURL(blob);
  } catch (err) {
    const n = (snapState.fails.get(cameraKey) || 0) + 1;
    snapState.fails.set(cameraKey, n);
    return null;
  }
}

async function runSnapshotCycle() {
  snapState.timer = null;
  if (!snapshotsAllowed()) {
    pauseSnapshots();
    return;
  }
  try {
    if (route.name === 'home') {
      const linked = cameras.filter((c) => c.snapshot);
      await Promise.all(linked.map(async (cam) => {
        const url = await loadSnapshot(cam.key);
        if (url) applyTileSnapshot(cam.key, url);
        else if ((snapState.fails.get(cam.key) || 0) >= 2) fallbackTileThumb(cam.key);
      }));
    } else if (route.name === 'camera' && route.key && !current) {
      const cam = cameras.find((c) => c.key === route.key);
      if (cam && cam.snapshot) {
        const url = await loadSnapshot(cam.key);
        if (url) {
          const prev = stageImage.dataset.objectUrl;
          if (prev) URL.revokeObjectURL(prev);
          stageImage.dataset.objectUrl = url;
          stageImage.src = url;
          stageLabel.textContent = 'Live · snapshot';
        }
      }
    }
  } finally {
    scheduleSnapshots();
  }
}

function applyTileSnapshot(key, url) {
  const tile = cameraTiles && cameraTiles.querySelector(`[data-camera="${cssEsc(key)}"]`);
  if (!tile) { URL.revokeObjectURL(url); return; }
  const img = tile.querySelector('.tile-img');
  if (!img) { URL.revokeObjectURL(url); return; }
  const prev = img.dataset.objectUrl;
  if (prev) URL.revokeObjectURL(prev);
  img.dataset.objectUrl = url;
  img.src = url;
  // .clip-thumb images stay at opacity 0 until .loaded — without this the
  // snapshot loads but the tile still shows the placeholder.
  const media = img.closest('.clip-thumb');
  if (media) media.classList.add('loaded', 'has-image');
  tile.classList.add('has-image');
}

function fallbackTileThumb(key) {
  const tile = cameraTiles && cameraTiles.querySelector(`[data-camera="${cssEsc(key)}"]`);
  if (!tile || tile.dataset.thumbFallback === '1') return;
  const clipId = tile.dataset.newestThumb;
  if (!clipId) return;
  tile.dataset.thumbFallback = '1';
  const img = tile.querySelector('.tile-img');
  if (img) img.dataset.src = `api/thumbnail/${clipId}`;
  if (img && thumbObserver) thumbObserver.observe(img);
  else if (img) enqueueThumb(img);
}

if (stageLive) {
  stageLive.addEventListener('click', () => {
    if (snapState.paused) resumeSnapshots();
    else noteInteraction();
  });
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') pauseSnapshots();
  else if (!snapState.paused) scheduleSnapshots();
  else { /* stay paused until tap */ }
});

/* ── Home: Recent + tiles ─────────────────────────────────────────────────── */
let recentFetchedAt = 0;
let recentClips = [];
let recentByCamera = new Map();

async function fetchRecent(force = false) {
  if (!force && Date.now() - recentFetchedAt < RECENT_STALE_MS && recentFetchedAt > 0) {
    renderRecent();
    renderTiles();
    return;
  }
  const end = new Date();
  const start = new Date(end.getTime() - 6 * 3600 * 1000);
  const params = new URLSearchParams({
    camera: 'all',
    start: start.toISOString(),
    end: end.toISOString(),
  });
  try {
    const response = await fetch(`api/recordings?${params}`);
    const body = await response.json();
    recentClips = response.ok ? (body.clips || []) : [];
    recentFetchedAt = Date.now();
    recentByCamera = new Map();
    for (const clip of recentClips) {
      const k = String(clip.camera_key);
      if (!recentByCamera.has(k)) recentByCamera.set(k, clip);
    }
  } catch (err) {
    recentClips = [];
  }
  renderRecent();
  renderTiles();
}

function renderRecent() {
  if (!recentStrip) return;
  recentStrip.innerHTML = '';
  const top = recentClips.slice(0, 12);
  if (recentEmpty) recentEmpty.hidden = top.length > 0;
  for (const clip of top) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'recent-card';
    card.setAttribute('role', 'listitem');
    const thumb = document.createElement('div');
    thumb.className = 'clip-thumb';
    const img = document.createElement('img');
    img.alt = '';
    img.dataset.src = `api/thumbnail/${clip.id}`;
    img.dataset.clipKey = clipKey(clip);
    thumb.appendChild(img);
    if (thumbObserver) thumbObserver.observe(img);
    else enqueueThumb(img);
    const meta = document.createElement('div');
    meta.className = 'recent-meta';
    const name = document.createElement('div');
    name.className = 'recent-name';
    name.textContent = clip.camera_name || cameraName(clip.camera_key);
    const when = document.createElement('div');
    when.className = 'recent-when';
    when.textContent = shortTime(new Date(clip.start));
    meta.append(name, when, eventBadges(clip));
    card.append(thumb, meta);
    card.addEventListener('click', () => {
      // openClip synchronously in the tap handler (iOS gesture), then route.
      openClip(clip);
      const t = encodeURIComponent(clip.start);
      location.hash = `#/camera/${encodeURIComponent(clip.camera_key)}?t=${t}`;
    });
    recentStrip.appendChild(card);
  }
}

function renderTiles() {
  if (!cameraTiles) return;
  cameraTiles.innerHTML = '';
  for (const cam of cameras) {
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'camera-tile';
    tile.dataset.camera = cam.key;
    const media = document.createElement('div');
    media.className = 'tile-media clip-thumb';
    const img = document.createElement('img');
    img.className = 'tile-img';
    img.alt = '';
    media.appendChild(img);
    const paused = document.createElement('span');
    paused.className = 'tile-paused';
    paused.hidden = !snapState.paused;
    paused.textContent = 'Paused · tap to resume';
    media.appendChild(paused);

    const newest = recentByCamera.get(cam.key);
    if (newest) tile.dataset.newestThumb = newest.id;

    if (cam.snapshot) {
      // Snapshot filled by the poller; keep previous on failure.
    } else if (newest) {
      img.dataset.src = `api/thumbnail/${newest.id}`;
      if (thumbObserver) thumbObserver.observe(img);
      else enqueueThumb(img);
    }

    const foot = document.createElement('div');
    foot.className = 'tile-foot';
    const name = document.createElement('span');
    name.className = 'tile-name';
    name.textContent = cam.name;
    const ago = document.createElement('span');
    ago.className = 'tile-ago';
    ago.textContent = newest ? `last clip ${formatAgo(newest.start)}` : '—';
    foot.append(name, ago);
    if (deviceDown(cam.key)) {
      const dot = document.createElement('span');
      dot.className = 'tile-error';
      dot.title = 'Device unreachable';
      foot.appendChild(dot);
    }
    tile.append(media, foot);
    tile.addEventListener('click', () => {
      if (snapState.paused) resumeSnapshots();
      location.hash = `#/camera/${encodeURIComponent(cam.key)}`;
    });
    cameraTiles.appendChild(tile);
  }
  // Kick an immediate snapshot pass for linked cameras.
  if (route.name === 'home' && !snapState.paused) {
    clearSnapTimer();
    runSnapshotCycle();
  }
}

/* ── Camera page: day loading + simplified vertical timeline ──────────────── */
const camState = {
  key: null,
  days: [],            // [{day, clips, truncated, error}] newest day first
  seenKeys: new Set(),
  filter: 'all',
  loading: false,
  pausedAuto: false,
  emptyStreak: 0,
  stopped: false,
  canLoadOlder: true,
  pendingEarlier: false,
  pendingT: null,
  generation: 0,
  scrollObs: null,
};

function camNavClips() {
  const all = [];
  for (const day of camState.days) {
    for (const c of day.clips) {
      if (clipPassesFilter(c)) all.push(c);
    }
  }
  // Newest-at-top days; within a day clips are newest-first from API.
  // Nav walks forward in time → ascending.
  return all.slice().sort((a, b) => byTime(a, b) || String(a.id).localeCompare(String(b.id)));
}

function rebuildCamClips() {
  // no-op placeholder — lists are derived from camState.days
}

function clipPassesFilter(clip) {
  if (camState.filter === 'all') return true;
  return (clip.events || []).includes(camState.filter);
}

function typesInLoadedDays() {
  const set = new Set();
  for (const day of camState.days) {
    for (const c of day.clips) {
      for (const e of (c.events || [])) set.add(e);
    }
  }
  return EVENT_VOCAB.filter((t) => set.has(t));
}

function renderEventFilters() {
  if (!eventFiltersEl) return;
  const types = typesInLoadedDays();
  const showTypes = FILTER_CHIPS.filter((t) => types.includes(t));
  // Hidden when ≤ 1 type occurs in loaded days.
  if (types.length <= 1) {
    eventFiltersEl.hidden = true;
    eventFiltersEl.innerHTML = '';
    if (camState.filter !== 'all') {
      camState.filter = 'all';
      renderVTimeline();
    }
    return;
  }
  // Persisted filter whose type is absent falls back to All.
  if (camState.filter !== 'all' && !types.includes(camState.filter)) {
    camState.filter = 'all';
    if (camState.key) savePref(STORE.filter(camState.key), 'all');
  }
  eventFiltersEl.hidden = false;
  eventFiltersEl.innerHTML = '';
  const make = (label, value) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip filter-chip';
    chip.textContent = label;
    chip.setAttribute('aria-pressed', String(camState.filter === value));
    chip.addEventListener('click', () => {
      camState.filter = value;
      savePref(STORE.filter(camState.key), value);
      renderEventFilters();
      renderVTimeline();
      updateNav();
    });
    eventFiltersEl.appendChild(chip);
  };
  make('All', 'all');
  for (const t of showTypes) make(t[0].toUpperCase() + t.slice(1), t);
}

function resetCameraState(key) {
  camState.generation++;
  camState.key = key;
  camState.days = [];
  camState.seenKeys = new Set();
  camState.loading = false;
  camState.pausedAuto = false;
  camState.emptyStreak = 0;
  camState.stopped = false;
  camState.canLoadOlder = true;
  camState.pendingEarlier = false;
  const saved = loadPref(STORE.filter(key), 'all');
  camState.filter = typeof saved === 'string' ? saved : 'all';
  if (vtimelineEl) vtimelineEl.innerHTML = '';
  if (cameraEnd) cameraEnd.hidden = true;
  if (loadEarlierBtn) loadEarlierBtn.hidden = true;
  if (daySentinel) daySentinel.hidden = false;
}

async function fetchDay(cameraKey, dayDate) {
  const start = startOfLocalDay(dayDate);
  const end = localDate(dayDate) === localDate(new Date())
    ? new Date()
    : endOfLocalDay(dayDate);
  const params = new URLSearchParams({
    camera: cameraKey,
    start: start.toISOString(),
    end: end.toISOString(),
  });
  const response = await fetch(`api/recordings?${params}`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(body.detail || 'Day load failed');
    err.status = response.status;
    throw err;
  }
  return body;
}

function dayStrOf(d) {
  return localDate(d);
}

function oldestLoadedDayDate() {
  if (!camState.days.length) return startOfLocalDay(new Date());
  const last = camState.days[camState.days.length - 1].day;
  return startOfLocalDay(new Date(`${last}T12:00:00`));
}

async function loadDay(dayDate, { prepend = false } = {}) {
  const day = dayStrOf(dayDate);
  if (camState.days.some((d) => d.day === day)) return camState.days.find((d) => d.day === day);
  camState.loading = true;
  try {
    const body = await fetchDay(camState.key, dayDate);
    const clips = [];
    for (const c of (body.clips || [])) {
      const k = clipKey(c);
      // Dedupe across days: show in the day of its start.
      if (camState.seenKeys.has(k)) continue;
      if (localDate(new Date(c.start)) !== day) continue;
      camState.seenKeys.add(k);
      clips.push(c);
    }
    const entry = {
      day,
      clips,
      truncated: !!body.truncated,
      error: null,
      visibleCount: clips.filter(clipPassesFilter).length,
    };
    if (prepend) camState.days.push(entry); // older days go at the end (newest-first sections)
    else camState.days.unshift(entry);
    // Keep days sorted newest-first.
    camState.days.sort((a, b) => (a.day < b.day ? 1 : a.day > b.day ? -1 : 0));
    if (!clips.length) camState.emptyStreak += 1;
    else camState.emptyStreak = 0;
    return entry;
  } catch (err) {
    const entry = { day, clips: [], truncated: false, error: err.message || 'Failed', visibleCount: 0 };
    camState.days.push(entry);
    camState.days.sort((a, b) => (a.day < b.day ? 1 : a.day > b.day ? -1 : 0));
    camState.pausedAuto = true; // failed day pauses auto-loading
    return entry;
  } finally {
    camState.loading = false;
  }
}

async function loadNextOlderDay() {
  if (camState.loading || camState.stopped) return;
  if (camState.pausedAuto) {
    if (loadEarlierBtn) loadEarlierBtn.hidden = false;
    return;
  }
  const oldest = oldestLoadedDayDate();
  const next = addLocalDays(oldest, -1);
  const today = startOfLocalDay(new Date());
  const daysBack = Math.round((today - next) / 86400000);
  if (camState.emptyStreak >= EMPTY_DAY_STOP || daysBack >= MAX_DAYS_BACK) {
    camState.stopped = true;
    camState.canLoadOlder = false;
    if (cameraEnd) {
      cameraEnd.hidden = false;
      cameraEnd.textContent = 'No older recordings';
    }
    if (daySentinel) daySentinel.hidden = true;
    return;
  }
  const entry = await loadDay(next, { prepend: true });
  renderEventFilters();
  renderVTimeline();
  updateStopUi(entry);
  if (camState.pendingEarlier) {
    camState.pendingEarlier = false;
    const j = neighbourIndex(-1);
    if (j >= 0) openClip(j);
  }
  updateNav();
}

function updateStopUi(entry) {
  if (camState.pausedAuto) {
    if (loadEarlierBtn) {
      loadEarlierBtn.hidden = false;
      loadEarlierBtn.textContent = entry && entry.error ? 'Retry failed day' : 'Load earlier day';
    }
    return;
  }
  // Auto-load only while last loaded day added ≥1 visible card OR is empty.
  // When a filter hides everything, show "Load earlier day" instead.
  if (entry && entry.clips.length > 0 && entry.visibleCount === 0 && camState.filter !== 'all') {
    if (loadEarlierBtn) {
      loadEarlierBtn.hidden = false;
      loadEarlierBtn.textContent = 'Load earlier day';
    }
    return;
  }
  if (loadEarlierBtn) loadEarlierBtn.hidden = true;
  if (camState.emptyStreak >= EMPTY_DAY_STOP) {
    camState.stopped = true;
    camState.canLoadOlder = false;
    if (cameraEnd) {
      cameraEnd.hidden = false;
      cameraEnd.textContent = 'No older recordings';
    }
  }
}

function renderVTimeline() {
  if (!vtimelineEl) return;
  vtimelineEl.innerHTML = '';
  for (const day of camState.days) {
    day.visibleCount = day.clips.filter(clipPassesFilter).length;
  }
  const today = localDate(new Date());
  for (const day of camState.days) {
    const section = document.createElement('section');
    section.className = 'vday';
    section.dataset.day = day.day;

    const head = document.createElement('h3');
    head.className = 'vday-head';
    head.textContent = dayLabel(day.day);
    section.appendChild(head);

    if (day.day === today) {
      const nowLine = document.createElement('div');
      nowLine.className = 'vnow';
      nowLine.textContent = 'Now';
      section.appendChild(nowLine);
    }

    if (day.error) {
      const retry = document.createElement('div');
      retry.className = 'vday-error';
      retry.innerHTML = '';
      const msg = document.createElement('span');
      msg.textContent = `Could not load ${dayLabel(day.day)}: ${day.error}`;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip';
      btn.textContent = 'Retry';
      btn.addEventListener('click', async () => {
        camState.days = camState.days.filter((d) => d.day !== day.day);
        camState.pausedAuto = false;
        const d = startOfLocalDay(new Date(`${day.day}T12:00:00`));
        const entry = await loadDay(d);
        renderEventFilters();
        renderVTimeline();
        updateStopUi(entry);
      });
      retry.append(msg, btn);
      section.appendChild(retry);
      vtimelineEl.appendChild(section);
      continue;
    }

    if (day.truncated) {
      const notice = document.createElement('p');
      notice.className = 'message';
      notice.textContent = 'Not every clip of this day could be listed';
      section.appendChild(notice);
    }

    // Newest at the top within the day.
    const ordered = day.clips.slice().sort((a, b) => Date.parse(b.start) - Date.parse(a.start));
    for (let i = 0; i < ordered.length; i++) {
      const clip = ordered[i];
      const next = ordered[i + 1];
      const passes = clipPassesFilter(clip);
      const row = document.createElement('div');
      row.className = 'vrow' + (passes ? '' : ' dimmed');
      row.dataset.clipKey = clipKey(clip);
      row.dataset.clipId = clip.id;

      const time = document.createElement('div');
      time.className = 'vrow-time';
      time.textContent = shortTime(new Date(clip.start));

      const barCol = document.createElement('div');
      barCol.className = 'vrow-bar';
      const seg = document.createElement('div');
      seg.className = 'vrow-seg' + (passes ? '' : ' dim');
      barCol.appendChild(seg);

      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'vrow-card';
      card.hidden = !passes;
      const cardBody = document.createElement('div');
      cardBody.className = 'vrow-card-body';
      const t = document.createElement('div');
      t.className = 'vrow-card-time';
      t.textContent = shortTime(new Date(clip.start));
      const dur = document.createElement('div');
      dur.className = 'vrow-card-duration';
      dur.textContent = formatDuration(clip.duration_s);
      cardBody.append(t, dur, eventBadges(clip));
      attachThumb(card, clip);
      card.appendChild(cardBody);
      card.addEventListener('click', () => openClip(clip));

      row.append(time, barCol, card);
      section.appendChild(row);

      if (next) {
        const chronologicalGap = (Date.parse(clip.start) - Date.parse(next.end)) / 1000;
        if (chronologicalGap >= GAP_BAND_MIN_S) {
          const band = document.createElement('div');
          band.className = 'vgap';
          band.textContent = formatGap(chronologicalGap);
          section.appendChild(band);
        }
      }
    }

    if (!ordered.length && !day.error) {
      const empty = document.createElement('p');
      empty.className = 'message';
      empty.textContent = 'No recordings this day';
      section.appendChild(empty);
    }

    vtimelineEl.appendChild(section);
  }
  if (current) markActive(clipKey(current.clip));
  updateDateChipFromScroll();
}

function updateDateChipFromScroll() {
  if (!dateChip || !vtimelineEl) return;
  const sections = [...vtimelineEl.querySelectorAll('.vday')];
  if (!sections.length) {
    dateChip.textContent = 'Today';
    return;
  }
  let currentDay = sections[0].dataset.day;
  const stickyBottom = (dateChip.getBoundingClientRect().bottom || 0) + 8;
  for (const sec of sections) {
    if (sec.getBoundingClientRect().top <= stickyBottom) currentDay = sec.dataset.day;
  }
  dateChip.textContent = dayLabel(currentDay);
  if (dateJump) dateJump.value = currentDay;
}

function updateStageForCamera() {
  if (!route.key) return;
  playerPanel.hidden = false;
  showStageChrome();
  const cam = cameras.find((c) => c.key === route.key);
  playerCamera.textContent = cam ? cam.name : route.key;
  playerTime.textContent = '';
  if (cam && cam.snapshot) {
    stageLabel.textContent = 'Live · snapshot';
    if (!snapState.paused) {
      clearSnapTimer();
      runSnapshotCycle();
    }
  } else {
    stageLabel.textContent = 'Latest recording';
    const newest = recentByCamera.get(route.key)
      || (camState.days[0] && camState.days[0].clips[0]);
    if (newest) {
      stageImage.removeAttribute('src');
      stageImage.dataset.src = `api/thumbnail/${newest.id}`;
      if (thumbObserver) thumbObserver.observe(stageImage);
      else enqueueThumb(stageImage);
    } else {
      stageImage.removeAttribute('src');
    }
  }
}

async function enterCamera(key, tIso) {
  if (cameraTitle) cameraTitle.textContent = cameraName(key);
  resetCameraState(key);
  camState.pendingT = tIso || null;

  let focusDay = startOfLocalDay(new Date());
  if (tIso) {
    const t = new Date(tIso);
    if (Number.isFinite(t.getTime())) focusDay = startOfLocalDay(t);
  }

  // Always ensure Today is loaded; if t is another day, load that day too.
  const today = startOfLocalDay(new Date());
  await loadDay(today);
  if (dayStrOf(focusDay) !== dayStrOf(today)) {
    await loadDay(focusDay);
  }
  renderEventFilters();
  renderVTimeline();
  updateStopUi(camState.days[camState.days.length - 1]);

  if (!current) updateStageForCamera();
  else showPlayingChrome();

  // Reconcile playing clip by clipKey / scroll to t.
  if (tIso) {
    const targetKey = `${key}|${Date.parse(tIso)}`;
    const all = [];
    for (const d of camState.days) for (const c of d.clips) all.push(c);
    const match = all.find((c) => clipKey(c) === targetKey)
      || all.find((c) => Math.abs(Date.parse(c.start) - Date.parse(tIso)) < 2000);
    if (current) {
      // Already opened from Recent tap — just scroll.
      const el = vtimelineEl && vtimelineEl.querySelector(`[data-clip-key="${cssEsc(clipKey(current.clip))}"]`);
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
    } else if (match) {
      openClip(match);
      const el = vtimelineEl && vtimelineEl.querySelector(`[data-clip-key="${cssEsc(clipKey(match))}"]`);
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
    } else {
      const dayEl = vtimelineEl && vtimelineEl.querySelector(`.vday[data-day="${dayStrOf(focusDay)}"]`);
      if (dayEl && dayEl.scrollIntoView) dayEl.scrollIntoView({ block: 'start' });
    }
  }

  setupDaySentinel();
}

function setupDaySentinel() {
  if (!daySentinel) return;
  if (camState.scrollObs) camState.scrollObs.disconnect();
  camState.scrollObs = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      if (route.name !== 'camera') return;
      if (camState.loading || camState.stopped || camState.pausedAuto) return;
      // Only auto-load when last day had visible cards or was empty.
      const last = camState.days[camState.days.length - 1];
      if (last && last.clips.length > 0 && last.visibleCount === 0 && camState.filter !== 'all') {
        if (loadEarlierBtn) loadEarlierBtn.hidden = false;
        return;
      }
      loadNextOlderDay();
    }
  }, { rootMargin: '100px' });
  daySentinel.hidden = false;
  camState.scrollObs.observe(daySentinel);
}

if (loadEarlierBtn) {
  loadEarlierBtn.addEventListener('click', async () => {
    if (camState.pausedAuto) {
      // Retry the failed day (last with error) or just resume.
      const failed = [...camState.days].reverse().find((d) => d.error);
      if (failed) {
        camState.days = camState.days.filter((d) => d.day !== failed.day);
        camState.pausedAuto = false;
        await loadDay(startOfLocalDay(new Date(`${failed.day}T12:00:00`)));
        renderEventFilters();
        renderVTimeline();
        updateStopUi(camState.days[camState.days.length - 1]);
        return;
      }
      camState.pausedAuto = false;
    }
    await loadNextOlderDay();
  });
}

if (dateChip && dateJump) {
  dateChip.addEventListener('click', () => dateJump.showPicker ? dateJump.showPicker() : dateJump.click());
  dateJump.addEventListener('change', async () => {
    if (!dateJump.value || route.name !== 'camera') return;
    const d = startOfLocalDay(new Date(`${dateJump.value}T12:00:00`));
    await loadDay(d);
    renderEventFilters();
    renderVTimeline();
    const sec = vtimelineEl.querySelector(`.vday[data-day="${dateJump.value}"]`);
    if (sec) sec.scrollIntoView({ block: 'start' });
  });
}

if (refreshTodayBtn) {
  refreshTodayBtn.addEventListener('click', async () => {
    if (route.name !== 'camera') return;
    const today = localDate(new Date());
    // Drop today's clips from seenKeys and reload.
    const old = camState.days.find((d) => d.day === today);
    if (old) {
      for (const c of old.clips) camState.seenKeys.delete(clipKey(c));
      camState.days = camState.days.filter((d) => d.day !== today);
    }
    await loadDay(startOfLocalDay(new Date()));
    renderEventFilters();
    renderVTimeline();
  });
}

if (liveButton) liveButton.addEventListener('click', returnToLive);
if (vtimelineEl) {
  window.addEventListener('scroll', () => {
    if (route.name === 'camera') updateDateChipFromScroll();
  }, { passive: true });
}

/* ── Routing ──────────────────────────────────────────────────────────────── */
function parseHash() {
  const raw = (location.hash || '#/').replace(/^#/, '') || '/';
  const [pathPart, query = ''] = raw.split('?');
  const path = pathPart.replace(/\/+$/, '') || '/';
  const params = new URLSearchParams(query);
  if (path === '/' || path === '') return { name: 'home' };
  if (path === '/all') return { name: 'all' };
  const m = path.match(/^\/camera\/([^/?#]+)$/);
  if (m) {
    return { name: 'camera', key: decodeURIComponent(m[1]), t: params.get('t') };
  }
  return { name: 'home', redirect: true };
}

function disposeRoute() {
  // Leaving a route disposes the playing clip (generation bump, abort).
  if (current) {
    current.player.dispose();
    current = null;
  }
  markActive(null);
  camState.generation++;
  camState.key = null;
  camState.days = [];
  if (vtimelineEl) vtimelineEl.innerHTML = '';
  if (camState.scrollObs) { camState.scrollObs.disconnect(); camState.scrollObs = null; }
  clearSnapTimer();
}

async function applyRoute() {
  const next = parseHash();
  if (next.redirect) {
    location.hash = '#/';
    return;
  }
  const prevName = route.name;
  const prevKey = route.key;
  // Dispose when leaving a route or switching camera.
  const leaving = prevName !== next.name || (next.name === 'camera' && prevKey !== next.key);
  // Special case: Recent tap sets hash after openClip — do not dispose.
  const keepPlaying = next.name === 'camera' && current
    && String(current.clip.camera_key) === String(next.key);

  if (leaving && !keepPlaying) {
    disposeRoute();
    playerPanel.hidden = true;
    if (stageLive) stageLive.hidden = true;
    playerBody.hidden = false;
  } else if (leaving && keepPlaying) {
    // Still bump camera state but keep the player.
    camState.generation++;
    if (camState.scrollObs) { camState.scrollObs.disconnect(); camState.scrollObs = null; }
  }

  route = next;
  document.body.classList.remove('view-home', 'view-camera', 'view-all');
  document.body.classList.add(`view-${next.name}`);

  if (viewHome) viewHome.hidden = next.name !== 'home';
  if (viewCamera) viewCamera.hidden = next.name !== 'camera';
  if (viewAll) viewAll.hidden = next.name !== 'all';

  if (tabCameras) tabCameras.setAttribute('aria-current', next.name === 'home' || next.name === 'camera' ? 'page' : 'false');
  if (tabAll) tabAll.setAttribute('aria-current', next.name === 'all' ? 'page' : 'false');

  if (next.name === 'home') {
    await fetchRecent();
    scheduleSnapshots();
  } else if (next.name === 'camera') {
    await enterCamera(next.key, next.t);
  } else if (next.name === 'all') {
    // Search form ready; player opens on demand.
    if (!current) playerPanel.hidden = true;
  }
}

window.addEventListener('hashchange', () => { applyRoute(); });

/* ── Wiring ───────────────────────────────────────────────────────────────── */
for (const button of document.querySelectorAll('[data-preset]')) {
  button.addEventListener('click', () => applyPreset(button.dataset.preset));
}
if (prevButton) prevButton.addEventListener('click', () => step(-1));
if (nextButton) nextButton.addEventListener('click', () => step(1));
if (closeButton) closeButton.addEventListener('click', closePlayer);
if (showMoreButton) showMoreButton.addEventListener('click', renderMoreRows);
if (autoplayInput) {
  autoplayInput.checked = loadPref(STORE.autoplay, false) === true;
  autoplayInput.addEventListener('change', () => savePref(STORE.autoplay, autoplayInput.checked));
}

(async function init() {
  if (dateInput) applyPreset('1h');
  try {
    await loadChannels();
  } catch (err) {
    showMessage(err.message, 'bad');
  }
  loadHealth();
  await applyRoute();
})();
