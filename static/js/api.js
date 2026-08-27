// fetch wrapper: JSON request/response, unified error toast, upload, polling.
import { toast } from './ui.js';

/**
 * Generic request. Returns parsed JSON (or text).
 * On failure, shows a toast and throws an Error (message is backend detail or status code).
 * When options.silent = true, no toast is shown — caller handles errors.
 */
export async function request(path, { method = 'GET', body, raw = false, silent = false } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (!silent) toast('Network error: cannot reach server', 'error');
    throw new Error('Network error: cannot reach server');
  }

  if (!res.ok) {
    // Session expired / not logged in: clear auth state and redirect to login (the login endpoint's own 401 is excluded)
    if (res.status === 401 && !path.startsWith('/api/auth/')) {
      document.body.classList.remove('authed');
      if (location.hash !== '#/login') location.hash = '#/login';
      if (!silent) toast('Please sign in first', 'error');
      throw new Error('Not signed in');
    }
    let msg = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      const detail = data.detail ?? data.message ?? data;
      msg = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map(d => d.msg || JSON.stringify(d)).join('; ')
        : JSON.stringify(detail);
    } catch { /* non-JSON response body */ }
    if (!silent) toast(msg, 'error');
    throw new Error(msg);
  }

  if (raw) return res;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  return res.text();
}

export const get = (path, opts = {}) => request(path, { ...opts, method: 'GET' });
export const post = (path, body = {}, opts = {}) => request(path, { ...opts, method: 'POST', body });
export const put = (path, body = {}, opts = {}) => request(path, { ...opts, method: 'PUT', body });
export const patch = (path, body = {}, opts = {}) => request(path, { ...opts, method: 'PATCH', body });
export const del = (path, opts = {}) => request(path, { ...opts, method: 'DELETE' });

// FormData upload (do not set Content-Type; let the browser add the boundary).
export async function upload(path, formData, opts = {}) {
  let res;
  try {
    res = await fetch(path, { method: 'POST', body: formData });
  } catch {
    if (!opts.silent) toast('Network error: cannot reach server', 'error');
    throw new Error('Network error: cannot reach server');
  }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try { msg = (await res.json()).detail || msg; } catch { /* ignore */ }
    if (!opts.silent) toast(msg, 'error');
    throw new Error(msg);
  }
  return res.json();
}

/**
 * Polling helper. Stops when fn returns a truthy value; the returned stop() can cancel manually.
 * Errors thrown by fn do not stop polling (transient network errors are tolerated);
 * consecutive errors are the caller's responsibility.
 */
export function poll(fn, ms = 1000) {
  let stopped = false;
  let timer = null;
  async function tick() {
    if (stopped) return;
    let stopAfter = false;
    try {
      stopAfter = !!(await fn());
    } catch { /* ignore a single failure */ }
    if (stopped || stopAfter) return;
    timer = setTimeout(tick, ms);
  }
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}
