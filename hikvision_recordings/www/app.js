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
      const video = document.createElement('video');
      video.controls = true;
      video.playsInline = true;
      video.preload = 'none';
      video.src = `api/stream/${clip.id}`;          // relative — see rule at top
      const hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = "Seeking isn't supported for live DVR playback — download the clip to scrub.";
      const download = document.createElement('a');
      download.className = 'download';
      download.href = `api/download/${clip.id}`;    // relative — see rule at top
      download.textContent = 'Download';
      download.setAttribute('download', '');
      body.append(video, hint, download);
      video.play().catch(() => { /* iOS wants a second tap; the controls handle it */ });
    }
  });

  item.append(header, body);
  return item;
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
  applyPreset('today');
  try {
    await loadChannels();
  } catch (err) {
    showMessage(err.message, 'bad');
  }
  loadHealth();
})();
