// Tasks list: table + status polling + delete.
import { get, del } from '../api.js';
import { statusBadge, fmtTime, confirmDialog, toast } from '../ui.js';

let stopPoll = null;

export async function mount(el) {
  el.innerHTML = `
    <div class="row" style="margin-bottom:18px">
      <h1 class="page-title" style="margin:0">Tasks</h1>
      <span class="spacer"></span>
      <a class="btn primary" href="#/workbench">+ New translation</a>
    </div>
    <div class="table-wrap">
      <table class="tasks">
        <thead>
          <tr>
            <th>Filename</th><th>Format</th><th>Language pair</th><th>Status</th>
            <th>Progress</th><th>Created</th><th style="text-align:right">Actions</th>
          </tr>
        </thead>
        <tbody id="tbody"><tr><td colspan="7" class="muted">Loading...</td></tr></tbody>
      </table>
    </div>`;

  const tbody = el.querySelector('#tbody');
  await refresh(tbody);

  stopPoll = pollIfTranslating(tbody);
  return () => { if (stopPoll) stopPoll(); };
}

function pollIfTranslating(tbody) {
  let timer = null;
  let stopped = false;
  async function tick() {
    if (stopped) return;
    const anyActive = tbody.dataset.anyActive === '1';
    if (anyActive) {
      try { await refresh(tbody); } catch { /* ignore single failure */ }
    }
    if (!stopped) timer = setTimeout(tick, 2000);
  }
  timer = setTimeout(tick, 2000);
  return () => { stopped = true; clearTimeout(timer); };
}

async function refresh(tbody) {
  let tasks;
  try {
    tasks = await get('/api/tasks');
  } catch { return; }
  renderRows(tbody, tasks);
}

function renderRows(tbody, tasks) {
  tbody.replaceChildren();
  tbody.dataset.anyActive = tasks.some(t => t.status === 'translating') ? '1' : '0';

  if (!tasks.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="7"><div class="empty-state">
      <div class="icon">🗂️</div>
      <p>No tasks yet. <a href="#/workbench">Upload a document</a> to get started.</p>
    </div></td>`;
    tbody.appendChild(tr);
    return;
  }

  for (const t of tasks) {
    const tr = document.createElement('tr');

    // Filename
    const tdName = document.createElement('td');
    tdName.className = 'fname';
    tdName.textContent = t.filename;
    tdName.title = t.filename;

    // Format
    const tdExt = document.createElement('td');
    const extB = document.createElement('span');
    extB.className = 'ext-badge';
    extB.textContent = (t.ext || '').replace('.', '');
    tdExt.appendChild(extB);

    // Language pair
    const tdLang = document.createElement('td');
    const langSpan = document.createElement('span');
    langSpan.className = 'muted mono';
    langSpan.textContent = `${t.source_lang || 'auto'} -> ${t.target_lang || '-'}`;
    tdLang.appendChild(langSpan);

    // Status
    const tdStatus = document.createElement('td');
    tdStatus.appendChild(statusBadge(t.status));
    if (t.status === 'done' && t.untranslated_count > 0) {
      const chip = document.createElement('span');
      chip.className = 'untrans-tag';
      chip.style.marginLeft = '6px';
      chip.textContent = `${t.untranslated_count} untranslated`;
      tdStatus.appendChild(chip);
    }

    // Progress
    const tdProg = document.createElement('td');
    tdProg.appendChild(buildMiniProgress(t));

    // Time
    const tdTime = document.createElement('td');
    tdTime.className = 'muted small';
    tdTime.style.whiteSpace = 'nowrap';
    tdTime.textContent = fmtTime(t.created_at);

    // Actions
    const tdOps = buildOps(t);

    tr.append(tdName, tdExt, tdLang, tdStatus, tdProg, tdTime, tdOps);
    tbody.appendChild(tr);
  }
}

function buildMiniProgress(t) {
  const wrap = document.createElement('div');
  wrap.className = 'mini-progress';
  const track = document.createElement('div');
  track.className = 'progress-track';
  const fill = document.createElement('div');
  fill.className = 'progress-fill' + (t.status === 'translating' && !t.total_segments ? ' indeterminate' : '');
  const done = t.done_segments || 0;
  const total = t.total_segments || 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : (t.status === 'done' ? 100 : 0);
  fill.style.width = `${pct}%`;
  track.appendChild(fill);
  const label = document.createElement('div');
  label.className = 'small muted';
  label.style.marginTop = '3px';
  label.textContent = `${done}/${total} segments`;
  wrap.append(track, label);
  return wrap;
}

function buildOps(t) {
  const td = document.createElement('td');
  td.style.textAlign = 'right';
  td.style.whiteSpace = 'nowrap';

  const cmp = document.createElement('a');
  cmp.className = 'btn sm';
  cmp.href = `#/compare/${t.task_id}`;
  cmp.textContent = 'Compare';

  td.appendChild(cmp);
  if (t.has_translated) {
    const dl = document.createElement('a');
    dl.className = 'btn sm ghost';
    dl.href = `/api/tasks/${t.task_id}/download`;
    dl.textContent = 'Download';
    td.appendChild(dl);
  }

  const rm = document.createElement('button');
  rm.className = 'btn sm danger ghost';
  rm.textContent = 'Delete';
  rm.addEventListener('click', () => removeTask(t.task_id, t.filename));
  td.appendChild(rm);
  return td;
}

async function removeTask(taskId, filename) {
  const ok = await confirmDialog(`Delete task "${filename}"? All files and the translation will be removed. This cannot be undone.`, {
    title: 'Delete task',
    okText: 'Delete',
  });
  if (!ok) return;
  try {
    await del(`/api/tasks/${taskId}`);
    toast('Task deleted', 'success');
  } catch { return; }
  const tbody = document.getElementById('view').querySelector('#tbody');
  if (tbody) refresh(tbody);
}
