// Shared UI helpers: esc / toast / modal / confirm dialog / debounce / Esc-to-close.

// HTML escape: any user data going into innerHTML must pass through here.
export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const ICONS = { success: '✓', error: '✕', warn: '⚠', info: 'i' };

export function toast(msg, type = 'info', ms = 3200) {
  const root = document.getElementById('toasts');
  if (!root) return;
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = `${ICONS[type] || ''} ${msg}`.trim();
  const dismiss = () => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 220);
  };
  el.addEventListener('click', dismiss);
  root.appendChild(el);
  setTimeout(dismiss, type === 'error' ? ms + 2200 : ms);
}

/**
 * Open a modal. body is a Node or an HTML string (static templates only).
 * Returns { close, el }; Esc / backdrop click / ✕ all close it.
 */
export function openModal({ title = '', body = '', wide = false, onClose = null } = {}) {
  const root = document.getElementById('modal-root');
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  const box = document.createElement('div');
  box.className = `modal${wide ? ' wide' : ''}`;
  box.innerHTML = `
    <div class="modal-head">
      <h3></h3>
      <button class="modal-close" aria-label="Close">×</button>
    </div>
    <div class="modal-body"></div>`;
  box.querySelector('h3').textContent = title;
  const bodyEl = box.querySelector('.modal-body');
  if (typeof body === 'string') bodyEl.innerHTML = body;
  else bodyEl.appendChild(body);
  mask.appendChild(box);
  root.appendChild(mask);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    mask.remove();
    if (onClose) onClose();
  }
  function onKey(e) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    }
  }
  box.querySelector('.modal-close').addEventListener('click', close);
  mask.addEventListener('mousedown', e => { if (e.target === mask) close(); });
  // Listen in the capture phase so view-level keydown handlers don't preempt it
  document.addEventListener('keydown', onKey, true);

  return { close, el: bodyEl };
}

// Promise-based confirm dialog.
export function confirmDialog(message, { okText = 'Delete', danger = true, title = 'Confirm action' } = {}) {
  return new Promise(resolve => {
    const m = openModal({
      title,
      onClose: () => resolve(false),
      body: `<p style="margin:4px 0 0"></p>`,
    });
    m.el.querySelector('p').textContent = message;
    const foot = document.createElement('div');
    foot.className = 'modal-foot';
    foot.style.padding = '6px 20px 18px';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn ghost';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = () => { resolve(false); m.close(); };
    const okBtn = document.createElement('button');
    okBtn.className = `btn${danger ? ' danger' : ' primary'}`;
    okBtn.textContent = okText;
    okBtn.onclick = () => { resolve(true); m.close(); };
    foot.append(cancelBtn, okBtn);
    // modal-foot must be inside .modal, not .modal-body
    m.el.parentElement.appendChild(foot);
  });
}

export function debounce(fn, ms = 200) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// Timestamp → human-friendly local format.
export function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  if (isNaN(d)) return String(ts);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Task status badge DOM.
export function statusBadge(status) {
  const labels = {
    pending_confirm: 'Pending',
    translating: 'Translating',
    done: 'Done',
    failed: 'Failed',
    cancelled: 'Cancelled',
  };
  const span = document.createElement('span');
  span.className = `badge st-${status || 'pending_confirm'}`;
  span.textContent = labels[status] || status || 'Unknown';
  return span;
}

export const LANGS = [
  ['auto', 'Auto-detect'],
  ['zh-CN', 'Chinese (Simplified)'],
  ['en', 'English'],
  ['th', 'Thai'],
  ['ja', 'Japanese'],
  ['ko', 'Korean'],
  ['fr', 'French'],
  ['de', 'German'],
  ['es', 'Spanish'],
  ['ru', 'Russian'],
  ['vi', 'Vietnamese'],
];

export function langLabel(code) {
  const hit = LANGS.find(([c]) => c === code);
  return hit ? hit[1] : code || '-';
}

// Build a <select> for language codes. skipAuto=true omits 'auto'.
export function langSelect(value, { skipAuto = false, name = '' } = {}) {
  const sel = document.createElement('select');
  if (name) sel.name = name;
  for (const [code, label] of LANGS) {
    if (skipAuto && code === 'auto') continue;
    const opt = document.createElement('option');
    opt.value = code;
    opt.textContent = label;
    if (code === value) opt.selected = true;
    sel.appendChild(opt);
  }
  return sel;
}
