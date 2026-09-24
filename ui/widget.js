/* ===========================================================================
   Sticky Note widget — renderer.

   The card shows a checklist. Ticking an item strikes it through; the host owns the
   file, so this file only draws what it is given and sends clicks back.

   Same shape as the other two widgets: the host pushes a payload, the page renders it,
   and without the Python host the page falls back to ui/state.preview.json so the
   design can be reviewed in a browser.
   =========================================================================== */

'use strict';

const PREVIEW_STATE = 'state.preview.json';
const BRIDGE_WAIT_ATTEMPTS = 30;
const BRIDGE_WAIT_MS = 200;
const BRIDGE_RETRY_MS = 3000;
// Pressing inside this many pixels of the bottom-right corner resizes instead of the
// header drag: a corner region cannot be missed the way a small grip glyph can.
const RESIZE_CORNER_PX = 26;

const PREF_KEYS = {
  on_top: 'sw-on-top',
  autostart: 'sw-autostart',
  stay_on_desktop: 'sw-stay-on-desktop',
  glass: 'sw-glass',
  draggable: 'sw-draggable',
};

const el = (id) => document.getElementById(id);
const hasBridge = () => Boolean(window.pywebview && window.pywebview.api);

let state = null;
let previewMode = false;
let busy = false;

/* ------------------------------------------------------------- bridge --- */

async function call(name, ...args) {
  if (!hasBridge()) return null;
  try {
    return await window.pywebview.api[name](...args);
  } catch (error) {
    console.error('[widget] bridge call failed:', name, error);
    return null;
  }
}

/** Every interaction and boot stage goes to widget.log (the widget has no console). */
function report(stage, detail = '') {
  call('boot_report', stage, String(detail));
}

async function waitForBridge(attempts = BRIDGE_WAIT_ATTEMPTS, delayMs = BRIDGE_WAIT_MS) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (hasBridge()) return true;
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  return false;
}

/* ------------------------------------------------------------ render --- */

function rowFor(item) {
  const row = document.createElement('li');
  row.dataset.id = String(item.id);
  row.dataset.done = String(Boolean(item.done));

  const tick = document.createElement('button');
  tick.type = 'button';
  tick.className = 'tick';
  tick.setAttribute('role', 'checkbox');
  tick.setAttribute('aria-checked', String(Boolean(item.done)));
  tick.title = item.done ? 'Mark as not done' : 'Mark as done';
  tick.textContent = '\u2713';
  tick.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleItem(item.id);
  });

  const text = document.createElement('span');
  text.className = 'text';
  text.textContent = item.text;

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'del';
  remove.title = 'Delete this item';
  remove.setAttribute('aria-label', 'Delete this item');
  remove.textContent = '\u00d7';
  remove.addEventListener('click', (event) => {
    event.stopPropagation();
    removeItem(item.id);
  });

  row.append(tick, text, remove);
  return row;
}

function render(next) {
  if (!next) return;
  state = next;

  const root = el('widget');
  root.dataset.draggable = String((next.prefs || {}).draggable !== false);
  const on = next.glass_enabled !== false;
  root.dataset.glass = on ? (next.glass === 'dense' ? 'dense' : 'clear') : 'off';

  const done = Number(next.done || 0);
  const total = Number(next.total || 0);
  el('badge-label').textContent = total ? `${done}/${total} DONE` : 'EMPTY NOTE';
  el('badge').title = total
    ? `${done} of ${total} ticked off\nClick an item to tick it`
    : 'Type below and press Enter to add the first item';

  const list = el('items');
  const scroll = list.scrollTop;
  list.replaceChildren(...(next.items || []).map(rowFor));
  list.scrollTop = scroll; // re-rendering must not jump the list under the cursor

  el('empty').hidden = total > 0;

  const prefs = next.prefs || {};
  for (const [key, id] of Object.entries(PREF_KEYS)) {
    const node = el(id);
    if (node) node.setAttribute('aria-checked', String(Boolean(prefs[key])));
  }

  if (!previewMode) {
    report(
      'layout',
      `viewport=${window.innerWidth}x${window.innerHeight} ` +
        `card=${el('card').offsetWidth}x${el('card').offsetHeight} items=${total}`
    );
  }
}

/* ---------------------------------------------------------- actions --- */

async function toggleItem(id) {
  if (busy) return;
  busy = true;
  report('tick', String(id));
  const next = await call('toggle_item', id);
  busy = false;
  if (next) render(next);
}

async function removeItem(id) {
  report('delete', String(id));
  const next = await call('remove_item', id);
  if (next) render(next);
}

async function addItem(text) {
  const value = String(text || '').trim();
  if (!value) return;
  report('add', value.slice(0, 40));
  el('add').value = '';
  const next = await call('add_item', value);
  if (next) render(next);
}

async function setPref(key, id) {
  const node = el(id);
  const wanted = node.getAttribute('aria-checked') !== 'true';
  const next = await call('set_pref', key, wanted);
  if (next) render(next);
}

function openSheet(open) {
  el('sheet').toggleAttribute('hidden', !open);
  el('btn-settings').setAttribute('aria-expanded', String(open));
  report('settings panel', open ? 'opened' : 'closed');
}

/* ---------------------------------------------------------- gestures --- */

function bindGestures() {
  // A note has to accept typing and text selection, so the window is dragged by its
  // header strip only -- never by the body.
  const handle = el('drag-handle');
  const card = el('card');
  const locked = () => Boolean(state && state.prefs && state.prefs.draggable === false);

  card.addEventListener('mousedown', (event) => {
    if (event.button !== 0 || locked()) return;
    const box = card.getBoundingClientRect();
    const corner =
      box.right - event.clientX <= RESIZE_CORNER_PX &&
      box.bottom - event.clientY <= RESIZE_CORNER_PX;
    if (!corner) return;
    event.preventDefault();
    report('gesture', 'resize');
    call('begin_resize');
  });

  handle.addEventListener('mousedown', (event) => {
    if (event.button !== 0 || event.target.closest('button')) return;
    if (locked()) return;
    report('gesture', 'move');
    call('begin_move');
  });
}

function bindControls() {
  for (const [key, id] of Object.entries(PREF_KEYS)) {
    el(id).addEventListener('click', () => setPref(key, id));
  }
  el('btn-settings').addEventListener('click', () => {
    openSheet(el('sheet').hasAttribute('hidden'));
  });
  el('sheet-close').addEventListener('click', () => openSheet(false));
  el('btn-hide').addEventListener('click', () => call('hide_window'));
  el('btn-quit').addEventListener('click', () => call('quit_app'));

  const add = el('add');
  add.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    addItem(add.value);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!el('sheet').hasAttribute('hidden')) {
      openSheet(false);
      return;
    }
    if (document.activeElement === add) add.blur();
  });
}

/* ------------------------------------------------------------- boot --- */

async function bootFromBridge() {
  if (!hasBridge()) return false;
  const next = await call('get_state');
  if (!next) return false;
  previewMode = false;
  render(next);
  return true;
}

async function bootFromPreview() {
  try {
    const response = await fetch(PREVIEW_STATE, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    previewMode = true;
    render(await response.json());
    report('preview payload loaded');
  } catch (error) {
    report('no preview payload', String(error));
  }
}

(async function boot() {
  bindGestures();
  bindControls();
  report('page ready');

  if (new URLSearchParams(window.location.search).has('host')) {
    const ready = await waitForBridge();
    report('bridge wait finished', String(ready));
    if (ready && (await bootFromBridge())) {
      report('bridge connected');
      return;
    }
    setInterval(async () => {
      if (!previewMode) return;
      if (await bootFromBridge()) report('bridge connected late');
    }, BRIDGE_RETRY_MS);
  }
  await bootFromPreview();
})();

// The host pushes the note after every change, and on every preference change.
window.__widgetPush = (payload) => render(payload);
