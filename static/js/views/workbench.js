// Workbench: upload -> parse result -> configure -> progress -> terminal.
import { get, post, upload, poll } from '../api.js';
import { toast, esc, langSelect } from '../ui.js';

const LS_SRC = 'dt_source_lang';
const LS_TGT = 'dt_target_lang';

let state = null; // { task: {...}, phase }
let stopPoll = null;

export async function mount(el) {
  state = { task: null };
  renderUpload(el);
  return () => { if (stopPoll) stopPoll(); };
}

/* ---------- Phase 1: upload ---------- */

function renderUpload(el) {
  el.innerHTML = `
    <h1 class="page-title">Upload document</h1>
    <div class="dropzone" id="dz" role="button" tabindex="0" aria-label="Select or drop a file">
      <div class="dz-icon">📄</div>
      <h2>Drop a file here, or click to choose</h2>
      <p>Supported: .docx / .pptx / .xlsx / .pdf, max 100 MB</p>
    </div>
    <input type="file" id="file-input" accept=".docx,.pptx,.xlsx,.pdf" hidden>`;
  const dz = el.querySelector('#dz');
  const input = el.querySelector('#file-input');

  dz.addEventListener('click', () => input.click());
  dz.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') input.click(); });
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) doUpload(el, input.files[0]);
    input.value = '';
  });
  for (const evt of ['dragenter', 'dragover']) {
    dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.add('dragover'); });
  }
  for (const evt of ['dragleave', 'drop']) {
    dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.remove('dragover'); });
  }
  dz.addEventListener('drop', e => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) doUpload(el, f);
  });
}

async function doUpload(el, file) {
  const fd = new FormData();
  fd.append('file', file);
  // Uploading placeholder.
  el.innerHTML = `
    <div class="card" style="text-align:center;padding:60px 20px">
      <span class="spin" style="font-size:30px">◌</span>
      <p class="muted" style="margin-top:12px">Uploading and parsing <b></b> ...</p>
    </div>`;
  el.querySelector('b').textContent = esc(file.name);

  try {
    const task = await upload('/api/upload', fd);
    state.task = task;
    renderConfigured(el);
  } catch {
    renderUpload(el); // back to the upload page; api.js already toasted the error.
  }
}

/* ---------- Phase 2: parse result + configure ---------- */

async function renderConfigured(el) {
  const t = state.task;
  el.innerHTML = `
    <h1 class="page-title">Confirm translation settings</h1>
    <div class="card result-card">
      <div class="row wrap">
        <span class="ext-badge">${esc(t.ext)}</span>
        <b style="word-break:break-all"></b>
      </div>
      <div class="stat-row">
        <div class="stat"><b>${t.segment_count ?? '-'}</b><span>translatable segments</span></div>
        <div class="stat"><b>${t.total_chars ?? '-'}</b><span>characters</span></div>
        <div class="stat"><b>${t.skipped_count ?? 0}</b><span>segments skipped</span></div>
      </div>
      <ul class="warnings" style="${(t.warnings || []).length ? '' : 'display:none'}"></ul>
    </div>
    <div class="card">
      <div class="config-row">
        <label class="field"><span class="lbl">Translation model</span><select id="sel-provider"></select></label>
        <label class="field"><span class="lbl">Source language</span><select id="sel-src"></select></label>
        <label class="field"><span class="lbl">Target language</span><select id="sel-tgt"></select></label>
        <button class="btn primary lg" id="btn-start">Start translation</button>
      </div>
      <p class="hint" id="provider-hint" style="display:none">
        No translation model configured yet. <a href="#/settings">Add one in Settings &rarr;</a>
      </p>
    </div>`;

  // Filename via textContent (XSS-safe).
  el.querySelector('.result-card b').textContent = t.filename;

  const ul = el.querySelector('.warnings');
  for (const w of t.warnings || []) {
    const li = document.createElement('li');
    li.className = 'warning-item';
    li.textContent = w;
    ul.appendChild(li);
  }

  // Provider dropdown.
  const selProvider = el.querySelector('#sel-provider');
  let providers = [];
  let settings = {};
  try {
    const data = await get('/api/providers');
    providers = data.providers || [];
    settings = data.settings || {};
  } catch { /* toast already shown */ }

  if (!providers.length) {
    selProvider.disabled = true;
    el.querySelector('#provider-hint').style.display = '';
    el.querySelector('#btn-start').disabled = true;
  } else {
    for (const p of providers) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name}@${p.model}`;
      selProvider.appendChild(opt);
    }
    const defId = settings.translation_provider_id;
    if (defId && providers.some(p => p.id === defId)) selProvider.value = defId;
  }

  // Language dropdowns: remember the last choice.
  const savedSrc = localStorage.getItem(LS_SRC) || settings.source_lang || 'auto';
  const savedTgt = localStorage.getItem(LS_TGT) || settings.target_lang || 'en';
  const selSrc = langSelect(savedSrc);
  selSrc.id = 'sel-src';
  const selTgt = langSelect(savedTgt, { skipAuto: true });
  selTgt.id = 'sel-tgt';
  el.querySelector('#sel-src').replaceWith(selSrc);
  el.querySelector('#sel-tgt').replaceWith(selTgt);
  if (selSrc.value === selTgt.value && selTgt.value !== 'en') selSrc.value = 'auto';

  el.querySelector('#btn-start').addEventListener('click', () => startTask(el));
}

async function startTask(el) {
  const btn = el.querySelector('#btn-start');
  btn.disabled = true;
  btn.textContent = 'Starting...';
  const body = {
    provider_id: el.querySelector('#sel-provider').value,
    source_lang: el.querySelector('#sel-src').value,
    target_lang: el.querySelector('#sel-tgt').value,
  };
  localStorage.setItem(LS_SRC, body.source_lang);
  localStorage.setItem(LS_TGT, body.target_lang);
  try {
    await post(`/api/tasks/${state.task.task_id}/start`, body);
  } catch {
    btn.disabled = false;
    btn.textContent = 'Start translation';
    return;
  }
  renderProgress(el);
}

/* ---------- Phase 3: progress panel ---------- */

function renderProgress(el) {
  const tid = state.task.task_id;
  el.innerHTML = `
    <h1 class="page-title">Translating</h1>
    <div class="card progress-panel">
      <div class="row wrap" style="margin-bottom:14px">
        <span class="ext-badge">${esc(state.task.ext)}</span>
        <b style="word-break:break-all"></b>
        <span class="spacer"></span>
        <span class="badge st-translating">Translating</span>
      </div>
      <div style="display:flex;align-items:center;gap:16px">
        <div class="progress-track" style="flex:1"><div class="progress-fill" id="pf"></div></div>
        <span class="pct" id="pct">0%</span>
      </div>
      <div class="row small muted" id="batch-info" style="margin-top:10px"></div>
      <div class="row" style="margin-top:18px;justify-content:flex-end">
        <button class="btn danger" id="btn-cancel">Cancel translation</button>
      </div>
    </div>
    <div id="terminal-slot"></div>`;
  el.querySelector('.progress-panel b').textContent = state.task.filename;

  el.querySelector('#btn-cancel').addEventListener('click', async e => {
    e.target.disabled = true;
    try { await post(`/api/tasks/${tid}/cancel`); toast('Cancellation requested', 'warn'); }
    catch { e.target.disabled = false; }
  });

  if (stopPoll) stopPoll();
  stopPoll = poll(async () => {
    const p = await get(`/api/tasks/${tid}/progress`);
    updateProgress(p);
    if (['done', 'failed', 'cancelled'].includes(p.status)) {
      await refreshTaskSummary();
      renderTerminal(el, p.status);
      return true; // stop polling
    }
    return false;
  }, 800);
}

function updateProgress(p) {
  const pf = document.getElementById('pf');
  const pct = document.getElementById('pct');
  const info = document.getElementById('batch-info');
  if (!pf || !pct) return;
  const done = p.done_segments || 0;
  const total = p.total_segments || 0;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  pf.classList.toggle('indeterminate', !total);
  pf.style.width = `${total ? percent : 30}%`;
  pct.textContent = `${percent}%`;
  if (info) info.textContent = `${done} / ${total} segments done`;
}

async function refreshTaskSummary() {
  try {
    state.task = await get(`/api/tasks/${state.task.task_id}`);
  } catch { /* keep old data */ }
}

/* ---------- Phase 4: terminal ---------- */

function renderTerminal(el, status) {
  if (stopPoll) { stopPoll(); stopPoll = null; }
  const slot = document.getElementById('terminal-slot');
  const panel = el.querySelector('.progress-panel');
  if (!slot || !panel) return; // user navigated away
  panel.style.display = 'none';
  const t = state.task;
  const dlUrl = `/api/tasks/${t.task_id}/download`;

  if (status === 'done') {
    slot.innerHTML = `
      <div class="card done-card">
        <div class="big-icon">🎉</div>
        <h2>Translation complete</h2>
        <p class="muted small" id="done-summary" style="margin-bottom:20px"></p>
        <div class="row" style="justify-content:center">
          <a class="btn primary lg" href="#/compare/${t.task_id}">View comparison</a>
          <a class="btn lg" href="${dlUrl}">Download</a>
          <button class="btn ghost lg" id="btn-new">Translate another</button>
        </div>
      </div>`;
    el.querySelector('#done-summary').textContent =
      `${t.segment_count ?? '-'} segments - ${t.total_chars ?? '-'} characters - target ${t.target_lang}`;
    el.querySelector('#btn-new').addEventListener('click', () => renderUpload(el));
  } else if (status === 'cancelled') {
    slot.innerHTML = `
      <div class="card done-card">
        <div class="big-icon">⏸️</div>
        <h2>Cancelled</h2>
        <p class="muted small">Completed segments are kept. You can resume translation at any time.</p>
        <div class="row" style="justify-content:center;margin-top:18px">
          <button class="btn primary lg" id="btn-resume">Resume translation</button>
          <a class="btn lg" href="#/tasks">Back to tasks</a>
        </div>
      </div>`;
    el.querySelector('#btn-resume').addEventListener('click', () => startResume(el));
  } else {
    slot.innerHTML = `
      <div class="card done-card">
        <div class="big-icon">❌</div>
        <h2>Translation failed</h2>
        <div class="error-box" id="err-box" style="text-align:left"></div>
        <div class="row" style="justify-content:center;margin-top:18px">
          <button class="btn primary lg" id="btn-retry">Retry</button>
          <a class="btn lg" href="#/tasks">Back to tasks</a>
        </div>
      </div>`;
    el.querySelector('#err-box').textContent = t.error || 'Unknown error';
    el.querySelector('#btn-retry').addEventListener('click', () => startResume(el));
  }
}

/** Resume / retry: re-start the task with the previous settings. */
async function startResume(el) {
  const t = state.task;
  try {
    await post(`/api/tasks/${t.task_id}/start`, {
      provider_id: t.provider_id,
      source_lang: t.source_lang || 'auto',
      target_lang: t.target_lang || 'en',
    });
    renderProgress(el);
  } catch { /* toast already shown */ }
}
