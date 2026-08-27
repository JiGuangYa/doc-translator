// Sign in / first-run admin password setup.
import { get, post } from '../api.js';
import { toast } from '../ui.js';

function goWorkbench() {
  document.body.classList.add('authed');
  if (location.hash === '#/login' || !location.hash) {
    location.hash = '#/workbench';
  }
}

async function submitPassword(path, payload, btn) {
  btn.disabled = true;
  try {
    await post(path, payload);
    toast('Signed in', 'success');
    goWorkbench();
  } catch (e) {
    // request() already showed the error toast.
  } finally {
    btn.disabled = false;
  }
}

export async function mount(el) {
  let st = { configured: false };
  try { st = await get('/api/auth/status', { silent: true }); } catch { /* still render the form on backend errors */ }

  if (st.authenticated) { goWorkbench(); return; }

  const isSetup = !st.configured;
  el.innerHTML = `
    <div class="login-wrap">
      <form class="login-card" id="login-form">
        <h1 class="page-title">${isSetup ? 'Set up admin password' : 'Sign in'}</h1>
        ${isSetup ? '<p class="hint">First run: choose an admin password (min 8 characters). The hash is stored only on this machine under data/config/auth.json.</p>'
                  : ''}
        <label>Password
          <input type="password" id="pw" autocomplete="${isSetup ? 'new-password' : 'current-password'}"
                 required minlength="8" autofocus>
        </label>
        ${isSetup ? `
        <label>Confirm password
          <input type="password" id="pw2" autocomplete="new-password" required minlength="8">
        </label>` : ''}
        <button type="submit" class="btn primary" id="btn">${isSetup ? 'Set and continue' : 'Sign in'}</button>
      </form>
    </div>`;

  const form = el.querySelector('#login-form');
  form.addEventListener('submit', e => {
    e.preventDefault();
    const pw = el.querySelector('#pw').value;
    if (isSetup) {
      const pw2 = el.querySelector('#pw2').value;
      if (pw !== pw2) { toast('Passwords do not match', 'error'); return; }
      submitPassword('/api/auth/setup', { password: pw }, el.querySelector('#btn'));
    } else {
      submitPassword('/api/auth/login', { password: pw }, el.querySelector('#btn'));
    }
  });
}
