// Settings: provider CRUD (with connectivity test) + global translation settings.
import { get, post, put, del } from '../api.js';
import { toast, confirmDialog, openModal, langSelect } from '../ui.js';

export async function mount(el) {
  el.innerHTML = `
    <h1 class="page-title">Settings</h1>

    <div class="card">
      <div class="row" style="margin-bottom:14px">
        <b>Translation providers</b>
        <span class="spacer"></span>
        <button class="btn primary sm" id="btn-add">+ Add provider</button>
      </div>
      <div class="provider-grid" id="provider-grid"><p class="muted small">Loading...</p></div>
    </div>

    <div class="card">
      <b>Global settings</b>
      <form class="settings-form" id="settings-form" style="margin-top:14px"></form>
    </div>`;

  const grid = el.querySelector('#provider-grid');
  el.querySelector('#btn-add').addEventListener('click', () => providerModal(null, () => reloadProviders(grid)));

  await Promise.all([reloadProviders(grid), loadSettingsForm(el.querySelector('#settings-form'))]);
  return () => {};
}

/* ================= Providers ================= */

async function reloadProviders(grid) {
  let data;
  try {
    data = await get('/api/providers');
  } catch {
    grid.innerHTML = `<p class="muted small">Failed to load</p>`;
    return;
  }
  renderProviders(grid, data.providers || []);
}

function renderProviders(grid, providers) {
  grid.replaceChildren();
  if (!providers.length) {
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'No providers configured. Add an OpenAI-compatible model service to start translating.';
    grid.appendChild(p);
    return;
  }
  for (const prov of providers) {
    grid.appendChild(providerCard(prov));
  }
}

function providerCard(prov) {
  const card = document.createElement('div');
  card.className = 'card hoverable provider-card';

  const head = document.createElement('div');
  head.className = 'p-head';
  const name = document.createElement('span');
  name.className = 'p-name';
  name.textContent = prov.name;
  const pill = document.createElement('span');
  pill.className = `key-pill ${prov.has_api_key ? 'ok' : 'none'}`;
  pill.textContent = prov.has_api_key ? 'API key set' : 'No API key';
  head.append(name, pill);

  const meta = document.createElement('div');
  meta.className = 'p-meta';
  const urlLine = document.createElement('div');
  urlLine.textContent = prov.base_url;
  const modelLine = document.createElement('div');
  modelLine.textContent = `Model: ${prov.model}`;
  meta.append(urlLine, modelLine);

  const ops = document.createElement('div');
  ops.className = 'row';
  ops.style.marginTop = '12px';
  ops.style.flexWrap = 'wrap';

  const testBtn = document.createElement('button');
  testBtn.className = 'btn sm';
  testBtn.textContent = 'Test connection';
  const testResult = document.createElement('div');
  testResult.className = 'test-result';
  testBtn.addEventListener('click', () => runTest(prov.id, testBtn, testResult));

  const editBtn = document.createElement('button');
  editBtn.className = 'btn sm ghost';
  editBtn.textContent = 'Edit';
  editBtn.addEventListener('click', () => providerModal(prov));

  const rmBtn = document.createElement('button');
  rmBtn.className = 'btn sm danger ghost';
  rmBtn.textContent = 'Delete';
  rmBtn.addEventListener('click', async () => {
    const ok = await confirmDialog(`Delete provider "${prov.name}"?`, { title: 'Delete provider' });
    if (!ok) return;
    try { await del(`/api/providers/${prov.id}`); toast('Deleted', 'success'); }
    catch { return; }
    const gridEl = document.getElementById('view').querySelector('#provider-grid');
    if (gridEl) reloadProviders(gridEl);
    refreshSettingsForm();
  });

  ops.append(testBtn, editBtn, rmBtn);

  const resultWrap = document.createElement('div');
  resultWrap.appendChild(testResult);

  card.append(head, meta, ops, resultWrap);
  return card;
}

async function runTest(providerId, btn, resultEl) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin">◌</span> Testing';
  try {
    const r = await post(`/api/providers/${providerId}/test`);
    if (r.ok) {
      resultEl.className = 'test-result ok';
      resultEl.textContent = `OK - latency ${r.latency_ms} ms`;
    } else {
      resultEl.className = 'test-result err';
      resultEl.textContent = `${r.error || 'Connection failed'}`;
    }
  } catch {
    resultEl.className = 'test-result err';
    resultEl.textContent = 'Test request failed';
  }
  btn.disabled = false;
  btn.textContent = 'Test connection';
}

/** Add / edit provider modal form. */
function providerModal(prov, onSaved = null) {
  const isEdit = !!prov;
  const body = document.createElement('div');
  body.innerHTML = `
    <form class="form-grid">
      <label class="field"><span class="lbl">Name *</span><input type="text" name="name" placeholder="e.g. DeepSeek"></label>
      <label class="field"><span class="lbl">Model *</span><input type="text" name="model" placeholder="e.g. deepseek-chat"></label>
      <label class="field full"><span class="lbl">Base URL *</span><input type="text" name="base_url" placeholder="https://api.deepseek.com/v1"></label>
      <label class="field full"><span class="lbl">${isEdit ? 'API Key (leave blank to keep)' : 'API Key'}</span><input type="password" name="api_key" placeholder="sk-..." autocomplete="new-password"></label>
    </form>`;

  const m = openModal({ title: isEdit ? `Edit provider: ${prov.name}` : 'Add provider', body });
  const form = body.querySelector('.form-grid');
  // Enter inside an input should trigger save, not a page-level form submit.
  form.addEventListener('submit', e => { e.preventDefault(); doSave(); });

  if (isEdit) {
    form.elements.name.value = prov.name || '';
    form.elements.model.value = prov.model || '';
    form.elements.base_url.value = prov.base_url || '';
  }

  const foot = document.createElement('div');
  foot.className = 'modal-foot';
  foot.style.padding = '0 20px 18px';
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn ghost';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', m.close);
  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn primary';
  saveBtn.textContent = 'Save';
  saveBtn.addEventListener('click', doSave);
  foot.append(cancelBtn, saveBtn);
  m.el.parentElement.appendChild(foot);

  async function doSave() {
    const payload = {
      name: form.elements.name.value.trim(),
      base_url: form.elements.base_url.value.trim(),
      model: form.elements.model.value.trim(),
      api_key: form.elements.api_key.value,
    };
    if (!payload.name || !payload.base_url || !payload.model) {
      toast('Name / Base URL / Model are required', 'warn');
      return;
    }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    try {
      if (isEdit) await put(`/api/providers/${prov.id}`, payload);
      else await post('/api/providers', payload);
    } catch {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
      return;
    }
    m.close();
    toast(isEdit ? 'Provider updated' : 'Provider added', 'success');
    const gridEl = document.getElementById('view').querySelector('#provider-grid');
    if (gridEl) reloadProviders(gridEl);
    refreshSettingsForm();
    if (onSaved) onSaved();
  }
}

/* ================= Global settings ================= */

let settingsCache = null;

async function loadSettingsForm(formEl) {
  let data;
  try {
    data = await get('/api/providers');
  } catch { return; }
  settingsCache = data.settings || {};
  buildSettingsForm(formEl, data.providers || [], settingsCache);
}

/** Re-build the settings form when the provider list changes (keep current values). */
async function refreshSettingsForm() {
  const formEl = document.getElementById('view').querySelector('#settings-form');
  if (!formEl) return;
  const keep = collectSettings(formEl);
  let data;
  try { data = await get('/api/providers'); } catch { return; }
  buildSettingsForm(formEl, data.providers || [], data.settings || {}, keep);
}

function providerOptions(sel, providers, selectedId, emptyLabel) {
  sel.replaceChildren();
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = emptyLabel;
  sel.appendChild(empty);
  for (const p of providers) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = `${p.name}@${p.model}`;
    if (p.id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  }
}

function buildSettingsForm(formEl, providers, settings, keep = null) {
  const cur = keep || {
    translation_provider_id: settings.translation_provider_id || '',
    assistant_provider_id: settings.assistant_provider_id || '',
    source_lang: settings.source_lang || 'auto',
    target_lang: settings.target_lang || 'en',
    batch_max_chars: settings.batch_max_chars,
    batch_max_segments: settings.batch_max_segments,
    concurrency_batches: settings.concurrency_batches,
    translate_notes: settings.translate_notes !== false,
  };

  formEl.innerHTML = `
    <div class="form-grid">
      <label class="field"><span class="lbl">Translation model</span><select name="translation_provider_id"></select></label>
      <label class="field"><span class="lbl">Assistant model (reserved)</span><select name="assistant_provider_id"></select></label>
      <label class="field"><span class="lbl">Default source language</span></label>
      <label class="field"><span class="lbl">Default target language</span></label>
      <label class="field"><span class="lbl">Max chars per batch</span><input type="number" name="batch_max_chars" min="200" max="20000" step="100"></label>
      <label class="field"><span class="lbl">Max segments per batch</span><input type="number" name="batch_max_segments" min="1" max="100" step="1"></label>
      <label class="field"><span class="lbl">Concurrent batches</span><input type="number" name="concurrency_batches" min="1" max="10" step="1"></label>
      <div class="field"><span class="lbl">&nbsp;</span><label class="check"><input type="checkbox" name="translate_notes"> Translate PPT speaker notes</label></div>
    </div>
    <div class="row" style="margin-top:18px">
      <button type="submit" class="btn primary">Save settings</button>
      <span class="hint" id="save-hint"></span>
    </div>
    <p class="hint" style="margin-top:6px">Recommended defaults: 4000 chars / 20 segments per batch. Larger batches risk model truncation and silent loss.</p>`;

  providerOptions(
    formEl.elements.translation_provider_id, providers,
    cur.translation_provider_id, providers.length ? '(unspecified)' : 'Add a provider above first');
  providerOptions(
    formEl.elements.assistant_provider_id, providers,
    cur.assistant_provider_id, '(unspecified)');

  const srcSel = langSelect(cur.source_lang, { name: 'source_lang' });
  const tgtSel = langSelect(cur.target_lang, { skipAuto: true, name: 'target_lang' });
  // Drop the selects into the 3rd and 4th label.field.
  formEl.querySelectorAll('label.field')[2].appendChild(srcSel);
  formEl.querySelectorAll('label.field')[3].appendChild(tgtSel);

  formEl.elements.batch_max_chars.value = cur.batch_max_chars ?? 4000;
  formEl.elements.batch_max_segments.value = cur.batch_max_segments ?? 20;
  formEl.elements.concurrency_batches.value = cur.concurrency_batches ?? 3;
  formEl.elements.translate_notes.checked = !!cur.translate_notes;

  formEl.addEventListener('submit', e => {
    e.preventDefault();
    saveSettings(formEl);
  });
}

function collectSettings(formEl) {
  return {
    translation_provider_id: formEl.elements.translation_provider_id.value,
    assistant_provider_id: formEl.elements.assistant_provider_id.value,
    source_lang: formEl.elements.source_lang.value,
    target_lang: formEl.elements.target_lang.value,
    batch_max_chars: parseInt(formEl.elements.batch_max_chars.value, 10) || 4000,
    batch_max_segments: parseInt(formEl.elements.batch_max_segments.value, 10) || 20,
    concurrency_batches: parseInt(formEl.elements.concurrency_batches.value, 10) || 3,
    translate_notes: formEl.elements.translate_notes.checked,
  };
}

async function saveSettings(formEl) {
  const v = collectSettings(formEl);
  const body = {
    translation_provider_id: v.translation_provider_id || null,
    assistant_provider_id: v.assistant_provider_id || null,
    source_lang: v.source_lang,
    target_lang: v.target_lang,
    batch_max_chars: v.batch_max_chars,
    batch_max_segments: v.batch_max_segments,
    concurrency_batches: v.concurrency_batches,
    translate_notes: v.translate_notes,
  };
  const hint = formEl.querySelector('#save-hint');
  hint.textContent = 'Saving...';
  try {
    await put('/api/settings', body);
  } catch {
    hint.textContent = '';
    return;
  }
  hint.textContent = 'Saved';
  toast('Settings saved', 'success');
  setTimeout(() => { hint.textContent = ''; }, 2500);
}
