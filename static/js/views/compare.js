// Compare view: segment-by-segment compare (all formats) / PDF page preview / XLSX two-column table preview.
import { get, patch, post, poll } from '../api.js';
import { toast, esc } from '../ui.js';

export async function mount(el, params) {
  const taskId = params[0];

  let info;
  try {
    info = await get(`/api/tasks/${taskId}/info`);
  } catch {
    return () => {};
  }

  el.classList.add('wide');
  const ext = info.ext || '';
  const modes = [['preview', 'Document compare']];
  modes.push(['segments', 'Segment compare']);
  if (ext === '.pdf') modes[0][1] = 'Page compare';
  if (ext === '.xlsx') modes[0][1] = 'Table compare';

  // ---- Top toolbar ----
  const toolbar = document.createElement('div');
  toolbar.className = 'compare-toolbar';
  toolbar.innerHTML = `
    <a class="btn sm ghost" href="#/tasks">← Back to task list</a>
    <b style="word-break:break-all;max-width:260px"></b>
    <span class="spacer"></span>
    <span class="seg-tabs"></span>
    <input type="text" class="search-input" placeholder="Search source / translation..." style="display:none">
    <a class="btn sm primary" href="/api/tasks/${taskId}/download">Download translation</a>`;
  toolbar.querySelector('b').textContent = info.filename;
  if (info.has_translated === false) {
    const dl = toolbar.querySelector('a.btn.primary');
    dl.classList.add('ghost');
    dl.removeAttribute('href');
    dl.style.opacity = '.45';
    dl.title = 'Translation not yet generated';
  }

  const tabsEl = toolbar.querySelector('.seg-tabs');
  const searchInput = toolbar.querySelector('.search-input');

  // ---- Content container ----
  const content = document.createElement('div');
  el.append(toolbar, content);

  let cleanupFn = null;

  // Live search: the segment view registers its own filter callback
  let applySearchCb = null;
  searchInput.addEventListener('input', () => { if (applySearchCb) applySearchCb(searchInput.value); });

  function setMode(key) {
    for (const b of tabsEl.children) {
      b.classList.toggle('active', b.dataset.mode === key);
    }
    searchInput.style.display = key === 'segments' ? '' : 'none';
    if (key !== 'segments') applySearchCb = null;
    if (cleanupFn) { cleanupFn(); cleanupFn = null; }
    content.replaceChildren();
    if (key === 'segments') cleanupFn = renderSegments(content, taskId, cb => { applySearchCb = cb; });
    else if (ext === '.pdf') cleanupFn = renderPdfPreview(content, taskId, info.pages || 0);
    else if (info.render_available === false) {
      // Server has no LibreOffice: fall back to a browser-side renderer
      if (ext === '.xlsx') cleanupFn = renderXlsxPreview(content, taskId);
      else if (ext === '.docx') cleanupFn = renderDocxPreview(content, taskId);
      else cleanupFn = renderUnsupportedPreview(content, ext);
    } else {
      const fallback = ext === '.xlsx' ? renderXlsxPreview
        : ext === '.docx' ? renderDocxPreview : renderUnsupportedPreview;
      cleanupFn = renderOfficePreview(content, taskId, () => fallback(content, taskId));
    }
  }

  for (const [key, label] of modes) {
    const b = document.createElement('button');
    b.textContent = label;
    b.dataset.mode = key;
    b.addEventListener('click', () => setMode(key));
    tabsEl.appendChild(b);
  }
  if (modes.length > 1) tabsEl.firstChild.classList.add('active');
  else tabsEl.style.display = 'none';

  setMode(modes[0][0]);

  return () => { if (cleanupFn) cleanupFn(); };
}

/* ================= Segment compare ================= */

function renderSegments(container, taskId, registerSearch) {
  container.innerHTML = `<p class="muted">Loading segments...</p>`;

  const UNTRANS_MARK = '⟪ untranslated:';
  const isUntrans = t => !!t && String(t).startsWith(UNTRANS_MARK);

  const state = {
    pairs: [],        // [{left, right}]
    segments: [],
    searchTerm: '',
    editingIdx: null,
    rafId: null,
  };

  get(`/api/tasks/${taskId}/segments`).then(data => {
    build(data);
  }).catch(() => {
    container.innerHTML = `<div class="card"><p class="muted">Failed to load segments.</p></div>`;
  });

  function build(data) {
    state.segments = data.segments || [];
    const overflow = data.overflow || [];
    const unCount = state.segments.filter(s => s.translatable !== false && isUntrans(s.translation)).length;

    container.innerHTML = `
      ${overflow.length ? `<div class="small overflow-bar" style="color:var(--warn);margin-bottom:10px"></div>` : ''}
      ${unCount ? `<div class="card untrans-bar" style="margin-bottom:10px;display:flex;align-items:center;gap:12px">
          <span style="color:var(--warn)">⚠ ${unCount} segments could not be auto-translated (model returned empty or batch failed)</span>
          <button type="button" class="btn small">Resume translation</button>
        </div>` : ''}
      <div class="dual">
        <div class="pane left">
          <div class="pane-head"><span>Source</span><span class="sync-toggle"><label class="check small"><input type="checkbox" checked>Scroll sync</label></span></div>
          <div class="pane-list"></div>
        </div>
        <div class="pane right">
          <div class="pane-head">Translation</div>
          <div class="pane-list"></div>
        </div>
      </div>`;
    if (overflow.length) {
      const obar = container.querySelector('.overflow-bar');
      if (obar) obar.textContent =
        `⚠ ${overflow.length} translation(s) exceeded the source text-frame size; layout was preserved as much as possible. Please review manually.`;
    }

    // One-click resume: resubmit with the original parameters (resume only translates missing/placeholder segments)
    const rebtn = container.querySelector('.untrans-bar button');
    if (rebtn) rebtn.addEventListener('click', async () => {
      rebtn.disabled = true;
      rebtn.textContent = 'Submitted, translating...';
      try {
        await post(`/api/tasks/${taskId}/start`, {
          provider_id: info.provider_id,
          source_lang: info.source_lang || 'auto',
          target_lang: info.target_lang || 'zh-CN',
        });
        toast('Resuming untranslated segments', 'success');
        await poll(async () => {
          const p = await get(`/api/tasks/${taskId}/progress`);
          return !['translating', 'pending'].includes(p.status);
        }, 1500);
        toast('Resume complete, refreshing...', 'success');
        setTimeout(() => location.reload(), 800);
      } catch (e) {
        toast(`Resume failed: ${e.message || e}`, 'error');
        rebtn.disabled = false;
        rebtn.textContent = 'Resume translation';
      }
    });

    const paneL = container.querySelector('.pane.left');
    const paneR = container.querySelector('.pane.right');
    const listL = paneL.querySelector('.pane-list');
    const listR = paneR.querySelector('.pane-list');

    state.segments.forEach((s, i) => {
      const leftCard = makeCard(s, i, false);
      const rightCard = s.translatable === false
        ? makeSkippedCard(s, i)
        : makeCard(s, i, true);
      listL.appendChild(leftCard);
      listR.appendChild(rightCard);
      state.pairs.push({ left: leftCard, right: rightCard });
    });

    /* --- Scroll sync (rAF-throttled, aligned by scrollTop ratio) --- */
    let syncOn = true;
    const syncChk = container.querySelector('.pane-head input[type=checkbox]');
    syncChk.addEventListener('change', () => { syncOn = syncChk.checked; });

    let syncing = false;
    function mirror(source, target) {
      if (!syncOn || state.editingIdx !== null || syncing) return;
      syncing = true;
      state.rafId = requestAnimationFrame(() => {
        const denom = source.scrollHeight - source.clientHeight;
        const ratio = denom > 0 ? source.scrollTop / denom : 0;
        target.scrollTop = ratio * Math.max(0, target.scrollHeight - target.clientHeight);
        syncing = false;
      });
    }
    const onScrollL = () => mirror(paneL, paneR);
    const onScrollR = () => mirror(paneR, paneL);
    paneL.addEventListener('scroll', onScrollL, { passive: true });
    paneR.addEventListener('scroll', onScrollR, { passive: true });

    registerSearch(term => {
      state.searchTerm = term.trim();
      filterAndPaint();
    });
  }

  /* --- Card construction --- */

  function makeCard(s, i, isTranslation) {
    const card = document.createElement('div');
    card.className = `seg-card${isTranslation ? ' translated' : ''}`;
    card.dataset.idx = i;
    card.dataset.side = isTranslation ? 'right' : 'left';

    appendMeta(card, s, i);
    const txt = document.createElement('div');
    txt.className = 'txt';
    card.appendChild(txt);

    card.addEventListener('click', () => focusPair(i, isTranslation ? 'right' : 'left'));
    if (isTranslation) card.addEventListener('dblclick', () => startEdit(i));
    return card;
  }

  function makeSkippedCard(s, i) {
    const card = document.createElement('div');
    card.className = 'seg-card skipped';
    card.dataset.idx = i;
    appendMeta(card, s, i, true);
    const txt = document.createElement('div');
    txt.className = 'txt';
    card.appendChild(txt);
    return card;
  }

  function appendMeta(card, s, i, skipped = false) {
    if (s.context) {
      const tag = document.createElement('span');
      tag.className = 'ctx-tag';
      tag.textContent = s.context;
      card.appendChild(tag);
    }
    const idx = document.createElement('span');
    idx.className = 'idx-tag';
    idx.textContent = skipped ? 'Skipped' : `#${i + 1}`;
    card.appendChild(idx);
  }

  function rawOf(i, side) {
    const s = state.segments[i];
    if (!s) return '';
    return side === 'right' ? (s.translation ?? '') : (s.text ?? '');
  }

  /** Repaint the text by searchTerm (wrap matches in <mark>). */
  function paintText(card, side) {
    const txt = card.querySelector('.txt');
    if (!txt) return;
    const raw = rawOf(+card.dataset.idx, side);
    // Placeholder segments (⟪untranslated⟫) skip the raw marker and show an "Untranslated" tag
    if (side === 'right' && isUntrans(raw)) {
      txt.innerHTML = `<span class="untrans-tag">⚠ Untranslated</span>`;
      card.classList.add('untrans');
      return;
    }
    card.classList.remove('untrans');
    if (!state.searchTerm) { txt.textContent = raw; return; }
    const lower = raw.toLowerCase();
    const term = state.searchTerm.toLowerCase();
    let html = '';
    let pos = 0;
    let hit = lower.indexOf(term);
    while (hit !== -1) {
      html += esc(raw.slice(pos, hit)) + `<mark>${esc(raw.slice(hit, hit + term.length))}</mark>`;
      pos = hit + term.length;
      hit = lower.indexOf(term, pos);
    }
    html += esc(raw.slice(pos));
    txt.innerHTML = html;
  }

  function filterAndPaint() {
    const term = state.searchTerm.toLowerCase();
    for (const pair of state.pairs) {
      let matched = true;
      if (state.searchTerm) {
        matched =
          rawOf(+pair.left.dataset.idx, 'left').toLowerCase().includes(term) ||
          rawOf(+pair.right.dataset.idx, 'right').toLowerCase().includes(term);
      }
      pair.left.classList.toggle('match-hidden', !matched);
      pair.right.classList.toggle('match-hidden', !matched);
      paintText(pair.left, 'left');
      paintText(pair.right, 'right');
    }
  }

  /* --- Click-to-highlight pair + scroll opposite side into view --- */

  function focusPair(i, fromSide) {
    const pair = state.pairs[i];
    if (!pair) return;
    for (const p of state.pairs) {
      p.left.classList.remove('highlight-ring');
      p.right.classList.remove('highlight-ring');
    }
    const target = fromSide === 'left' ? pair.right : pair.left;
    target.classList.add('highlight-ring');
    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  /* --- Double-click to edit translation --- */

  async function startEdit(i) {
    const pair = state.pairs[i];
    if (!pair || state.editingIdx !== null) return;
    const card = pair.right;
    if (card.classList.contains('skipped')) return;

    state.editingIdx = i;
    const oldRaw = rawOf(i, 'right');
    card.classList.add('editing');

    const editArea = document.createElement('div');
    editArea.className = 'edit-area';
    const ta = document.createElement('textarea');
    ta.value = oldRaw;
    ta.rows = Math.min(8, Math.max(2, oldRaw.split('\n').length + 1));
    const actions = document.createElement('div');
    actions.className = 'edit-actions';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'btn sm primary';
    saveBtn.textContent = 'Save';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn sm ghost';
    cancelBtn.textContent = 'Cancel';
    const hint = document.createElement('span');
    hint.className = 'small muted';
    hint.textContent = 'Enter to save · Esc to cancel · Shift+Enter for newline';
    actions.append(saveBtn, cancelBtn, hint);
    editArea.append(ta, actions);
    card.replaceChildren(editArea);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);

    let finished = false;
    function finish(saved) {
      if (finished) return;
      finished = true;
      state.editingIdx = null;
      card.classList.remove('editing');
      if (saved) {
        state.segments[i].translation = ta.value;
        toast('Translation saved', 'success');
      }
      rebuildCardContent(card, i);
    }

    async function save() {
      const text = ta.value;
      if (text === oldRaw) { finish(true); return; }
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving...';
      try {
        await patch(`/api/tasks/${taskId}/segments/${state.segments[i].seg_id}`, { text });
        finish(true);
      } catch {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
      }
    }

    saveBtn.addEventListener('click', save);
    cancelBtn.addEventListener('click', () => finish(false));
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save(); }
      else if (e.key === 'Escape') { e.stopPropagation(); finish(false); }
    });
  }

  /** Rebuild a card after edit (keep badge / highlight / search state). */
  function rebuildCardContent(card, i) {
    card.replaceChildren();
    appendMeta(card, state.segments[i], i);
    const txt = document.createElement('div');
    txt.className = 'txt';
    card.appendChild(txt);
    paintText(card, 'right');
  }

  return function teardown() {
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.pairs = [];
  };
}

/* ================= PDF page preview ================= */

function renderPdfPreview(container, taskId, pageCount, baseUrl) {
  baseUrl = baseUrl || `/api/tasks/${taskId}/preview/pdf`;
  if (!pageCount) {
    container.innerHTML = `<div class="card"><p class="muted">Cannot determine page count (PDF parse failed?).</p></div>`;
    return () => {};
  }

  container.innerHTML = `
    <div class="pdf-controls">
      <button class="btn sm ghost" id="pg-prev">◀</button>
      <span class="page-ind" id="pg-ind"></span>
      <button class="btn sm ghost" id="pg-next">▶</button>
      <input type="number" id="pg-jump" min="1" value="1">
      <button class="btn sm" id="pg-go">Jump</button>
      <span class="small muted">Click either side to zoom into a single column</span>
    </div>
    <div class="pdf-grid" id="pdf-grid">
      <div class="pdf-col left"><div class="col-label">Source</div><img class="lazy" alt="Source page image"></div>
      <div class="pdf-col right"><div class="col-label">Translation</div><img class="lazy" alt="Translation page image"></div>
    </div>`;

  let page = 1;
  const grid = container.querySelector('#pdf-grid');
  const ind = container.querySelector('#pg-ind');
  const imgL = grid.querySelector('.left img');
  const imgR = grid.querySelector('.right img');

  function loadPage(n) {
    page = Math.min(Math.max(1, n), pageCount);
    ind.textContent = `${page} / ${pageCount}`;
    const base = `${baseUrl}/${page}`;
    for (const [img, variant] of [[imgL, 'original'], [imgR, 'translated']]) {
      img.classList.add('lazy');
      img.onload = () => img.classList.remove('lazy');
      img.src = `${base}?variant=${variant}`;
    }
  }

  container.querySelector('#pg-prev').addEventListener('click', () => loadPage(page - 1));
  container.querySelector('#pg-next').addEventListener('click', () => loadPage(page + 1));
  const jump = container.querySelector('#pg-jump');
  const go = () => loadPage(parseInt(jump.value, 10) || 1);
  container.querySelector('#pg-go').addEventListener('click', go);
  jump.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });

  // Click a side → single-column zoom; click again to restore
  function toggleFocus(side) {
    const cls = `focus-${side}`;
    if (grid.classList.contains(cls)) {
      grid.classList.remove(cls);
      grid.querySelector(`.pdf-col.${side}`).classList.remove('focused');
    } else {
      grid.classList.remove('focus-left', 'focus-right');
      grid.querySelectorAll('.pdf-col').forEach(c => c.classList.remove('focused'));
      grid.classList.add(cls);
      grid.querySelector(`.pdf-col.${side}`).classList.add('focused');
    }
  }
  grid.querySelector('.pdf-col.left').addEventListener('click', () => toggleFocus('left'));
  grid.querySelector('.pdf-col.right').addEventListener('click', () => toggleFocus('right'));

  loadPage(1);
  return () => {};
}

/* ================= High-fidelity page-image compare (server-side LibreOffice) ================= */

/**
 * High-fidelity compare for Office formats: server-side LibreOffice converts
 * to PDF and renders page images, matching what Office/WPS would show.
 * Polls progress when the render is not ready; falls back when unavailable.
 */
function renderOfficePreview(container, taskId, buildFallback) {
  let disposed = false;
  let cleanupInner = null;
  let stopPoll = null;

  const hintBox = document.createElement('div');
  hintBox.className = 'card';
  hintBox.style.cssText = 'max-width:560px;margin:60px auto;text-align:center;padding:32px';
  hintBox.innerHTML = `
    <div style="font-size:40px">🖨️</div>
    <p class="muted" style="margin-top:14px" id="render-hint">Generating high-fidelity compare pages...</p>`;
  container.appendChild(hintBox);
  const hint = hintBox.querySelector('#render-hint');

  function showPages(pages) {
    if (disposed) return;
    hintBox.remove();
    stopPoll && stopPoll();
    cleanupInner = renderPdfPreview(container, taskId, pages,
      `/api/tasks/${taskId}/preview/render/page`);
  }

  function showFallback(reason) {
    if (disposed) return;
    stopPoll && stopPoll();
    hintBox.remove();
    if (reason) toast(`High-fidelity compare unavailable: ${reason}`, 'error');
    cleanupInner = buildFallback() || null;
  }

  get(`/api/tasks/${taskId}/preview/render`).then(st => {
    if (disposed) return;
    if (st.status === 'ready') return showPages(st.pages);
    if (st.status === 'unavailable' || st.status === 'failed')
      return showFallback(st.error || st.status);
    // none / rendering → trigger render and poll
    post(`/api/tasks/${taskId}/preview/render`, {}, { silent: true }).catch(() => {});
    stopPoll = poll(async () => {
      const s = await get(`/api/tasks/${taskId}/preview/render`);
      if (s.status === 'ready') { showPages(s.pages); return true; }
      if (s.status === 'failed' || s.status === 'unavailable') { showFallback(s.error); return true; }
      return false;
    }, 1500);
  }).catch(() => showFallback());

  return () => {
    disposed = true;
    stopPoll && stopPoll();
    if (cleanupInner) cleanupInner();
  };
}

/* ================= XLSX table preview ================= */

function renderXlsxPreview(container, taskId) {
  container.innerHTML = `
    <div class="xlsx-frame-wrap">
      <iframe src="/api/tasks/${taskId}/preview/xlsx" title="Table compare preview"></iframe>
    </div>`;
  return () => {};
}

/* ================= DOCX visual compare (docx-preview renders real layout) ================= */

const scriptCache = {};
function loadScript(src) {
  if (!scriptCache[src]) {
    scriptCache[src] = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.onload = resolve;
      s.onerror = () => { delete scriptCache[src]; reject(new Error('Failed to load ' + src)); };
      document.head.appendChild(s);
    });
  }
  return scriptCache[src];
}

async function fetchBuffer(taskId, variant) {
  const res = await fetch(`/api/tasks/${taskId}/file?variant=${variant}`);
  if (!res.ok) throw new Error(`File not found (${res.status})`);
  return res.arrayBuffer();
}

function renderDocxPreview(container, taskId) {
  container.innerHTML = `
    <div class="docx-controls">
      <span class="small muted">Zoom</span>
      <select id="zoom-sel">
        <option value="0.6">60%</option>
        <option value="0.8">80%</option>
        <option value="1" selected>100%</option>
        <option value="1.25">125%</option>
        <option value="1.5">150%</option>
      </select>
      <label class="check small"><input type="checkbox" id="sync-chk" checked> Scroll sync</label>
      <span class="hint" id="docx-hint">Loading render engine...</span>
      <span class="spacer"></span>
      <a class="btn sm ghost" href="/api/tasks/${taskId}/download?variant=original">Download source</a>
      <a class="btn sm primary" href="/api/tasks/${taskId}/download">Download translation</a>
    </div>
    <div class="docx-grid">
      <div class="docx-col"><div class="col-label">Source</div><div class="docx-pane paper" id="pane-orig"></div></div>
      <div class="docx-col"><div class="col-label">Translation</div><div class="docx-pane paper" id="pane-trans"></div></div>
    </div>`;

  const hint = container.querySelector('#docx-hint');
  const paneL = container.querySelector('#pane-orig');
  const paneR = container.querySelector('#pane-trans');
  const grid = container.querySelector('.docx-grid');
  let disposed = false;

  (async () => {
    try {
      await loadScript('/static/vendor/jszip.min.js');
      await loadScript('/static/vendor/docx-preview.min.js');
      if (disposed) return;
      hint.textContent = 'Rendering source...';
      const bufO = await fetchBuffer(taskId, 'original');
      await window.docx.renderAsync(bufO, paneL, null, { inWrapper: true, ignoreLastRenderedPageBreak: false });
      if (disposed) return;
      hint.textContent = 'Rendering translation...';
      const bufT = await fetchBuffer(taskId, 'translated');
      await window.docx.renderAsync(bufT, paneR, null, { inWrapper: true, ignoreLastRenderedPageBreak: false });
      if (disposed) return;
      hint.textContent = '';
      applyZoom();
    } catch (e) {
      if (disposed) return;
      hint.textContent = '';
      container.querySelectorAll('.docx-pane').forEach(p => {
        p.innerHTML = `<p class="muted small" style="padding:16px">Render failed: ${esc(e.message || e)}<br>Use "Segment compare" to review the content, or download the file to view it locally.</p>`;
      });
    }
  })();

  function applyZoom() {
    const z = parseFloat(container.querySelector('#zoom-sel').value);
    for (const pane of [paneL, paneR]) {
      const wrap = pane.querySelector('.docx-wrapper');
      if (wrap) {
        wrap.style.transform = `scale(${z})`;
        wrap.style.transformOrigin = 'top center';
      }
    }
  }
  container.querySelector('#zoom-sel').addEventListener('change', applyZoom);

  // Scroll sync
  let syncing = false;
  let syncOn = true;
  container.querySelector('#sync-chk').addEventListener('change', e => { syncOn = e.target.checked; });
  function mirror(src, dst) {
    if (!syncOn || syncing) return;
    syncing = true;
    requestAnimationFrame(() => {
      const denom = src.scrollHeight - src.clientHeight;
      dst.scrollTop = denom > 0 ? (src.scrollTop / denom) * (dst.scrollHeight - dst.clientHeight) : 0;
      syncing = false;
    });
  }
  paneL.parentElement.addEventListener('scroll', () => mirror(paneL.parentElement, paneR.parentElement), { passive: true });
  paneR.parentElement.addEventListener('scroll', () => mirror(paneR.parentElement, paneL.parentElement), { passive: true });

  return () => { disposed = true; };
}

/** PPT and other formats with no in-browser layout renderer: show a clear notice. */
function renderUnsupportedPreview(container, ext) {
  const names = { '.pptx': 'PowerPoint' };
  container.innerHTML = `
    <div class="card" style="max-width:640px;margin:40px auto;text-align:center;padding:36px">
      <div style="font-size:44px">📊</div>
      <h3 style="margin:12px 0 8px">In-browser layout preview is not supported for ${esc(names[ext] || ext)}</h3>
      <p class="muted" style="line-height:1.9">
        Please use "Segment compare" to review the source and translation paragraph by paragraph,<br>
        or download the translation and open it in Office / WPS — the file's layout and styles are fully preserved.
      </p>
      <div class="row" style="justify-content:center;margin-top:18px;display:flex;gap:10px">
        <button class="btn" id="goto-seg">Switch to segment compare</button>
      </div>
    </div>`;
  container.querySelector('#goto-seg').addEventListener('click', () => {
    const tabs = document.querySelector('.seg-tabs');
    if (tabs) tabs.querySelector('[data-mode=segments]').click();
  });
  return () => {};
}
