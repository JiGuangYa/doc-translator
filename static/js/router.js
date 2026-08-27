// hash router: #/workbench #/tasks #/compare/{taskId} #/settings #/login
import * as workbench from './views/workbench.js';
import * as tasksView from './views/tasks.js';
import * as compare from './views/compare.js';
import * as settings from './views/settings.js';
import * as login from './views/login.js';

const routes = [
  { re: /^#\/workbench\/?$/, view: workbench, tab: 'workbench', public: false },
  { re: /^#\/tasks\/?$/, view: tasksView, tab: 'tasks', public: false },
  { re: /^#\/compare\/([0-9a-f]{32})\/?$/, view: compare, tab: 'tasks', public: false },
  { re: /^#\/settings\/?$/, view: settings, tab: 'settings', public: false },
  { re: /^#\/login\/?$/, view: login, tab: '', public: true },
];

const viewEl = document.getElementById('view');
let cleanup = null;
let renderSeq = 0;
// /api/auth/status is queried once at startup; the 401 interceptor in
// api.js handles session-expiry redirects on later requests.
let authChecked = null;

async function checkAuth() {
  if (!authChecked) {
    authChecked = fetch('/api/auth/status')
      .then(r => r.json())
      .then(st => {
        if (st.authenticated) document.body.classList.add('authed');
        return st;
      })
      .catch(() => ({}));
  }
  return authChecked;
}

async function render() {
  const seq = ++renderSeq;
  if (typeof cleanup === 'function') {
    try { cleanup(); } catch { /* ignore */ }
    cleanup = null;
  }

  let hash = location.hash || '#/workbench';
  if (hash === '#' || hash === '') hash = '#/workbench';

  let matched = null;
  let params = {};
  for (const r of routes) {
    const m = hash.match(r.re);
    if (m) { matched = r; params = m.slice(1); break; }
  }
  if (!matched) { location.hash = '#/workbench'; return; }

  // Unauthenticated access to a protected view -> login page. (The login
  // view itself handles the already-authenticated redirect back.)
  if (!matched.public) {
    const st = await checkAuth();
    if (!st.authenticated && !document.body.classList.contains('authed')) {
      if (hash !== '#/login') { location.hash = '#/login'; return; }
    }
  }

  // Highlight the active top-bar tab.
  document.querySelectorAll('#nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.tab === matched.tab);
  });

  viewEl.classList.remove('wide');
  viewEl.replaceChildren();
  // mount() is async but the route switch clears the slot first; the seq
  // counter prevents a late-arriving old view from clobbering the new one.
  const ret = await matched.view.mount(viewEl, params);
  if (seq !== renderSeq) {
    if (typeof ret === 'function') ret();
    return;
  }
  cleanup = typeof ret === 'function' ? ret : null;
}

window.addEventListener('hashchange', render);
render();
