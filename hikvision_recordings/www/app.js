/* ── INGRESS RULE — EVERY URL IN THIS FILE IS RELATIVE ────────────────────────
 * Home Assistant serves this add-on under a rotating prefix:
 *     /api/hassio_ingress/<token>/
 * A leading slash — fetch("/api/channels") — escapes that prefix, hits HA core
 * and 404s. This is the single most common way HA add-on frontends break.
 * Use "api/channels", never "/api/channels". Same for <script>, <link>, <video>.
 * ──────────────────────────────────────────────────────────────────────────── */

const channelSelect = document.getElementById('channel');
const dateInput = document.getElementById('date');
const fromInput = document.getElementById('from');
const toInput = document.getElementById('to');
const form = document.getElementById('search-form');
const results = document.getElementById('results');
const message = document.getElementById('message');
const statusEl = document.getElementById('status');
const searchButton = document.getElementById('search-button');

const pad = (n) => String(n).padStart(2, '0');
const localDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const localTime = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

function showMessage(text, kind = 'info') {
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

async function loadChannels() {
  const response = await fetch('api/channels');
  if (!response.ok) throw new Error('Could not load the camera list.');
  const channels = await response.json();
  channelSelect.innerHTML = '';
  for (const channel of channels) {
    const option = document.createElement('option');
    option.value = channel.id;
    option.textContent = channel.name;
    channelSelect.appendChild(option);
  }
}

async function loadHealth() {
  try {
    const health = await (await fetch('api/health')).json();
    if (health.dvr === 'ok') {
      statusEl.textContent = 'DVR online';
      statusEl.className = 'status ok';
    } else if (health.dvr === 'auth_failed') {
      statusEl.textContent = 'DVR rejected the credentials';
      statusEl.className = 'status bad';
    } else {
      statusEl.textContent = 'DVR unreachable';
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

/* ── Lazy thumbnails ─────────────────────────────────────────────────────────
 * Thumbnails share the DVR's max_concurrent_downloads budget with playback and
 * downloads, so firing 40 requests the moment a search returns would swamp the
 * device and make the list slow. Each row's image is fetched only once it
 * actually scrolls into view, and only once (unobserved immediately after).
 * A failed thumbnail leaves that one row on its placeholder — it must never
 * break the rest of the list.
 * ─────────────────────────────────────────────────────────────────────────── */
const thumbObserver = ('IntersectionObserver' in window)
  ? new IntersectionObserver((entries, obs) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        obs.unobserve(img);
        img.src = img.dataset.src;   // relative URL — see the ingress rule above
      }
    }, { rootMargin: '200px' })      // start a little before the row is visible
  : null;

function attachThumb(item, clip) {
  const thumb = document.createElement('div');
  thumb.className = 'clip-thumb';
  const img = document.createElement('img');
  img.alt = '';
  img.loading = 'lazy';
  img.dataset.src = `api/thumbnail/${clip.id}`;   // relative — no leading slash
  img.addEventListener('load', () => thumb.classList.add('loaded'));
  img.addEventListener('error', () => thumb.classList.add('failed'));
  thumb.appendChild(img);
  item.prepend(thumb);
  if (thumbObserver) {
    thumbObserver.observe(img);
  } else {
    img.src = img.dataset.src;       // no IntersectionObserver: just load it
  }
}

/* ── PLAYBACK: client-side remux, with the server path as the safety net ──────
 * FAST PATH  — fetch api/stream-raw (the DVR's untouched MPEG-PS), remux it to MP4
 *              in this browser with ffmpeg.wasm, play it from a blob URL. No ffmpeg
 *              runs on the Pi, nothing is staged, and because the whole MP4 is in
 *              memory the result is genuinely SEEKABLE — which the server path
 *              cannot offer.
 * SAFE PATH  — api/stream, i.e. the tmpfs-staged server-side remux from v0.1.1/
 *              v0.1.3, untouched. Used automatically whenever the fast path is
 *              unavailable or fails for ANY reason.
 *
 * NO SharedArrayBuffer, NO COOP/COEP. That requirement belonged to ffmpeg.wasm's
 * multi-threaded core. Cross-origin isolation is inherited from the top-level
 * document, and HA's frontend sends neither header, so inside the Ingress iframe
 * crossOriginIsolated is always false and nothing this add-on serves can change
 * that. The single-threaded core needs none of it — see the Dockerfile note.
 * ─────────────────────────────────────────────────────────────────────────── */

// Above this, the fast path is skipped: the raw clip plus its remuxed copy both
// live in the wasm heap, and on a phone (HA Companion's WKWebView is the real
// target) a large clip will exhaust memory rather than throw something catchable.
// Compared against the MAINSTREAM size from the search result, which is the
// conservative direction — an SD fetch is roughly half of it.
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
      // NOTE: classWorkerURL is deliberately NOT passed. In the UMD build that
      // option forces `new Worker(..., {type:"module"})`, where importScripts()
      // does not exist, so loading the UMD core falls into a dead code path and
      // throws "Cannot find module". Omitting it yields a classic worker, and the
      // worker file is then resolved relative to vendor/ffmpeg.js — which is why
      // 814.ffmpeg.js must sit beside it.
      await ff.load({
        coreURL: new URL('vendor/ffmpeg-core.js', document.baseURI).href,
        wasmURL: new URL('vendor/ffmpeg-core.wasm', document.baseURI).href,
      });
      return ff;
    })().catch((err) => {
      ffmpegPromise = null;      // let a later clip retry rather than poisoning them all
      throw err;
    });
  }
  return ffmpegPromise;
}

// One wasm instance with one filesystem: two clips remuxing at once would collide.
// Serialise them instead of instantiating a second 32 MB core.
let remuxChain = Promise.resolve();
function serialise(task) {
  const run = remuxChain.then(task, task);
  remuxChain = run.catch(() => {});
  return run;
}

async function remuxInBrowser(clip, quality) {
  const ff = await getFFmpeg();
  const response = await fetch(`api/stream-raw/${clip.id}?quality=${quality}`);
  if (!response.ok) {
    let detail = `the add-on returned HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (err) { /* not JSON */ }
    throw new Error(detail);
  }
  const raw = new Uint8Array(await response.arrayBuffer());
  if (!raw.length) throw new Error('the DVR returned an empty clip');

  // Unique names: MEMFS is shared across every clip this session.
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const input = `in-${stamp}.ps`;
  const output = `out-${stamp}.mp4`;
  try {
    await ff.writeFile(input, raw);
    // The same stream copy the server does — no re-encode. Plain MP4 rather than
    // the server's fragmented one: the whole file is already here, so a normal
    // moov gives the browser full seeking.
    const code = await ff.exec(['-hide_banner', '-loglevel', 'error',
                                '-i', input, '-c', 'copy', '-f', 'mp4', output]);
    if (code !== 0) throw new Error(`ffmpeg.wasm exited ${code}`);
    const data = await ff.readFile(output);
    if (!data || !data.length) throw new Error('ffmpeg.wasm produced no output');
    return new Blob([data.buffer], { type: 'video/mp4' });
  } finally {
    // Always reclaim the wasm heap, even on failure — otherwise a few large clips
    // exhaust it and every later remux fails for an unrelated reason.
    for (const path of [input, output]) {
      try { await ff.deleteFile(path); } catch (err) { /* never existed */ }
    }
  }
}

/* Why the fast path was skipped, in words Ramon can read off a screenshot. */
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
    failed: 'Playback failed',
  }[mode] || mode;
  badge.title = detail || '';
}

function renderClip(clip) {
  const item = document.createElement('li');
  item.className = 'clip';

  const start = new Date(clip.start);
  const header = document.createElement('button');
  header.type = 'button';
  header.className = 'clip-header';

  const timeSpan = document.createElement('span');
  timeSpan.className = 'clip-time';
  timeSpan.textContent = start.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const metaSpan = document.createElement('span');
  metaSpan.className = 'clip-meta';
  metaSpan.textContent = `${formatDuration(clip.duration_s)} · ${formatSize(clip.size_bytes)}`;

  const chipSpan = document.createElement('span');
  chipSpan.className = 'clip-chip';
  chipSpan.textContent = clip.channel_name;

  header.append(timeSpan, metaSpan, chipSpan);

  const body = document.createElement('div');
  body.className = 'clip-body';
  body.hidden = true;

  header.addEventListener('click', () => {
    const opening = body.hidden;
    body.hidden = !body.hidden;
    if (opening && !body.dataset.loaded) {
      body.dataset.loaded = '1';
      buildPlayer(clip, body);
    }
  });

  item.append(header, body);
  attachThumb(item, clip);
  return item;
}

function buildPlayer(clip, body) {
  let quality = DEFAULT_QUALITY;
  let blobUrl = null;

  const video = document.createElement('video');
  video.controls = true;
  video.playsInline = true;
  video.preload = 'none';

  const bar = document.createElement('div');
  bar.className = 'clip-controls';

  const picker = document.createElement('div');
  picker.className = 'quality';
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
      for (const [key, el] of buttons) el.classList.toggle('on', key === quality);
      play();
    });
    buttons.set(q, button);
    picker.appendChild(button);
  }
  buttons.get(quality).classList.add('on');

  const badge = document.createElement('span');
  badge.className = 'mode-badge';

  bar.append(picker, badge);

  const hint = document.createElement('p');
  hint.className = 'hint';

  const download = document.createElement('a');
  download.className = 'download';
  // Download is always full-quality mainstream — deliberately NOT quality-aware.
  download.href = `api/download/${clip.id}`;      // relative — see rule at top
  download.textContent = 'Download';
  download.setAttribute('download', '');

  body.append(bar, video, hint, download);

  function useServerPath(reason) {
    setMode(badge, 'compat', reason);
    hint.textContent = "Seeking isn't supported on this path — download the clip to scrub.";
    video.src = `api/stream/${clip.id}?quality=${quality}`;   // relative — see rule at top
    video.play().catch(() => { /* iOS wants a second tap; the controls handle it */ });
  }

  async function play() {
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }

    const blocker = clientRemuxBlocker(clip);
    if (blocker) {
      // Never going to work here — don't load a 32 MB core to find that out, and
      // don't show an error for something that was never attempted.
      useServerPath(blocker);
      return;
    }

    setMode(badge, 'working', 'fetching raw clip and remuxing with ffmpeg.wasm');
    try {
      const blob = await serialise(() => remuxInBrowser(clip, quality));
      blobUrl = URL.createObjectURL(blob);
      video.src = blobUrl;
      setMode(badge, 'fast', 'remuxed in-browser by ffmpeg.wasm — seeking works');
      hint.textContent = '';
      video.play().catch(() => { /* iOS wants a second tap */ });
    } catch (err) {
      // ANY client-side failure falls through to the path that already works.
      console.warn('client remux failed; using the server path', err);
      useServerPath(`client remux failed: ${err && err.message ? err.message : err}`);
    }
  }

  play();
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  results.innerHTML = '';
  searchButton.disabled = true;

  const start = new Date(`${dateInput.value}T${fromInput.value}:00`);
  const end = new Date(`${dateInput.value}T${toInput.value}:59`);
  // "From" can be later in the clock than "To" (e.g. a "Last 6 h" preset run at 00:30,
  // or someone typing 23:00 → 01:00 by hand) — both mean the range crosses midnight into
  // the next day. Roll `end` forward a day rather than rejecting or silently mangling it.
  if (end <= start) {
    end.setDate(end.getDate() + 1);
  }
  showMessage(`Searching ${localDate(start)} ${localTime(start)} → ${localDate(end)} ${localTime(end)}…`);

  const params = new URLSearchParams({
    channel: channelSelect.value,
    start: start.toISOString(),
    end: end.toISOString(),
  });

  try {
    const response = await fetch(`api/recordings?${params.toString()}`);
    const body = await response.json();
    if (!response.ok) {
      showMessage(body.detail || 'Search failed.', 'bad');
      return;
    }
    if (!body.clips.length) {
      const name = channelSelect.options[channelSelect.selectedIndex].text;
      showMessage(`No recordings for ${name} between ${fromInput.value} and ${toInput.value}. Try a wider range.`);
      return;
    }
    showMessage(body.truncated
      ? `Showing the first ${body.clips.length} clips — narrow the time range to see more.`
      : `${body.clips.length} clip${body.clips.length === 1 ? '' : 's'}.`);
    for (const clip of body.clips) results.appendChild(renderClip(clip));
  } catch (err) {
    showMessage('Could not reach the add-on. Is it running?', 'bad');
  } finally {
    searchButton.disabled = false;
  }
});

for (const button of document.querySelectorAll('[data-preset]')) {
  button.addEventListener('click', () => applyPreset(button.dataset.preset));
}

(async function init() {
  applyPreset('1h');
  try {
    await loadChannels();
  } catch (err) {
    showMessage(err.message, 'bad');
  }
  loadHealth();
})();
