'use strict';

const token = document.querySelector('meta[name="controller-token"]').content;
const $ = id => document.getElementById(id);
const keys = new Set();
const movementKeys = ['w', 's', 'a', 'd', 'space', 'shift', 'q', 'e'];
let state = { connected: false, enabled: false };
let sequence = Date.now();
let connectedBefore = false;
let busyInput = false;
let stateBusy = false;
let stopped = false;
let streamSignature = '';
let lastControlReason = '';

function url(path) {
  return path + '?token=' + encodeURIComponent(token);
}

async function post(path, data) {
  const response = await fetch(url(path), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data), keepalive: true,
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'Request failed');
  return result;
}

function message(text) { $('message').textContent = text; }

async function input(force = false) {
  if (stopped || !state.connected || (!force && busyInput)) return;
  busyInput = true;
  sequence = Math.max(sequence + 1, Date.now());
  const data = {
    sequence, keys: Array.from(keys), speed: Number($('speed').value),
    yaw_speed: Number($('yaw-speed').value),
    headless: $('headless').checked,
  };
  try { await post('/api/input', data); }
  catch (error) { message(error.message); }
  finally { busyInput = false; }
}

async function action(name) {
  keys.clear();
  await input(true);
  try { message((await post('/api/action', { name })).message); await poll(); }
  catch (error) { message(error.message); }
}

function rotationAngle() {
  const selected = $('rotate').value;
  if (selected !== 'Auto') return Number(selected);
  return state.dimensions && state.dimensions[1] > state.dimensions[0] ? 90 : 0;
}

function fitStream() {
  const image = $('stream');
  const dimensions = state.dimensions || [image.naturalWidth, image.naturalHeight];
  if (!dimensions[0] || !dimensions[1] || image.hidden) return;
  const angle = rotationAngle();
  const sideways = angle === 90 || angle === 270;
  const availableWidth = $('viewer').clientWidth;
  const availableHeight = $('viewer').clientHeight;
  const rotatedWidth = sideways ? dimensions[1] : dimensions[0];
  const rotatedHeight = sideways ? dimensions[0] : dimensions[1];
  const scale = Math.min(availableWidth / rotatedWidth, availableHeight / rotatedHeight);
  image.style.width = dimensions[0] * scale + 'px';
  image.style.height = dimensions[1] * scale + 'px';
  image.style.transform = `translate(-50%, -50%) rotate(${angle}deg)`;
}

function renderState() {
  const available = state.profile === 83 && state.fresh && !state.emergency;
  $('host').disabled = state.connected;
  if (!state.connected) $('host').value = state.host;
  $('connect').textContent = state.connected ? 'Disconnect' : 'Connect';
  $('connect').classList.toggle('accent', !state.connected);
  $('status').textContent = state.calibrating
    ? `Calibrating · ${state.calibration_remaining.toFixed(1)}s`
    : state.profile !== null && state.profile !== 83 ? 'Unsupported profile'
    : state.connected ? state.fresh ? 'Connected' : 'Waiting for drone' : 'Disconnected';
  if (state.reason !== lastControlReason && /lost|stopped|supports profile/i.test(state.reason)) {
    message(state.reason);
  }
  lastControlReason = state.reason;
  $('status-dot').classList.toggle('connected', state.connected && state.fresh);
  $('status-dot').classList.toggle('warning', state.connected && !state.fresh);
  $('demo').hidden = !state.demo;
  $('profile-label').textContent = state.profile === null ? 'LOCAL SESSION' : `PROFILE ${state.profile}`;
  $('control-state').textContent = state.emergency ? 'Stop requested' : state.enabled ? 'Controls on' : 'Controls off';
  $('control-state').classList.toggle('active', state.enabled);
  $('enable').disabled = !available;
  $('enable').textContent = state.enabled ? 'Disable controls' : 'Enable controls';
  $('enable').classList.toggle('enabled', state.enabled);
  for (const id of ['takeoff', 'land', 'calibrate']) {
    $(id).disabled = !(available && state.enabled) || (state.calibrating && id !== 'land');
  }
  for (const name of ['roll', 'pitch', 'yaw']) {
    $('trim-' + name).textContent = (state.trims[name] > 0 ? '+' : '') + state.trims[name];
  }
  for (const button of document.querySelectorAll('[data-trim], #trim-reset')) {
    button.disabled = state.calibrating;
  }
  $('emergency').disabled = state.profile !== 83;
  $('camera').disabled = !available;
  $('snapshot').disabled = !state.dimensions;
  $('calibration-note').textContent = state.calibrating
    ? `Calibration command running. ${state.calibration_remaining.toFixed(1)}s remaining.`
    : 'Calibrate while stationary on a level surface.';
  $('video').textContent = state.dimensions
    ? `${state.dimensions[0]} × ${state.dimensions[1]} source · ${state.fps} fps`
    : state.connected ? state.video : 'Waiting for video';
  const live = Boolean(state.dimensions && !state.video_stale && state.connected);
  $('live-indicator').classList.toggle('live', live);
  $('camera-state').textContent = state.video_stale ? 'Paused' : live ? 'Live' : 'Standby';
  $('stale').hidden = !state.video_stale;
  for (let index = 0; index < 4; index++) {
    $(['roll', 'pitch', 'throttle', 'yaw'][index]).textContent = state.axes[index];
  }
  $('log').textContent = state.log_directory;

  const signature = state.dimensions ? state.host + ':' + state.dimensions.join('x') : '';
  if (state.connected && (!connectedBefore || (signature && signature !== streamSignature))) {
    $('stream').src = url('/stream') + '&n=' + Date.now();
    streamSignature = signature;
  }
  $('stream').hidden = !state.connected || !state.dimensions;
  $('empty').hidden = Boolean(state.connected && state.dimensions);
  if (!state.connected) {
    $('stream').removeAttribute('src');
    streamSignature = '';
    keys.clear();
  }
  connectedBefore = state.connected;
  fitStream();
}

async function poll() {
  if (stateBusy || stopped) return;
  stateBusy = true;
  try {
    const response = await fetch(url('/api/state'));
    if (!response.ok) throw Error('Controller unavailable');
    state = await response.json();
    renderState();
  } catch (error) {
    keys.clear();
    message('Controller unavailable. Flight input has stopped.');
    $('status').textContent = 'Controller unavailable';
    $('status-dot').classList.remove('connected');
  } finally { stateBusy = false; }
}

$('connect').onclick = async () => {
  keys.clear();
  await input(true);
  try {
    const result = await post(state.connected ? '/api/disconnect' : '/api/connect',
      state.connected ? {} : { host: $('host').value });
    message(result.message);
    await poll();
  } catch (error) { message(error.message); }
};
$('enable').onclick = () => action(state.enabled ? 'disable' : 'enable');
for (const name of ['takeoff', 'land', 'calibrate', 'emergency', 'camera', 'snapshot']) {
  $(name).onclick = () => action(name);
}

async function trim(axis, delta) {
  keys.clear();
  await input(true);
  try { message((await post('/api/trim', { axis, delta })).message); await poll(); }
  catch (error) { message(error.message); }
}
for (const button of document.querySelectorAll('[data-trim]')) {
  button.onclick = () => trim(button.dataset.trim, Number(button.dataset.delta));
}
$('trim-reset').onclick = () => trim('reset', 0);
$('quit').onclick = async () => {
  keys.clear();
  await input(true);
  try {
    await post('/api/quit', {});
    stopped = true;
    message('Controller closed. You can close this tab.');
  } catch (error) { message(error.message); }
};
$('speed').oninput = () => { $('speed-value').textContent = $('speed').value; };
$('yaw-speed').oninput = () => { $('yaw-speed-value').textContent = $('yaw-speed').value; };
$('rotate').onchange = () => { fitStream(); $('rotate').blur(); };
$('stream').onload = fitStream;
$('focus-view').onclick = () => {
  const active = document.body.classList.toggle('focus-view');
  $('focus-view').setAttribute('aria-pressed', String(active));
  $('focus-view').textContent = active ? 'Show controls' : 'Focus view';
  fitStream();
};
new ResizeObserver(fitStream).observe($('viewer'));

function neutral() { keys.clear(); input(true); }
window.addEventListener('blur', neutral);
window.addEventListener('pagehide', neutral);
document.addEventListener('visibilitychange', () => { if (document.hidden) neutral(); });
document.addEventListener('focusin', event => {
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) neutral();
});
document.addEventListener('keydown', event => {
  const key = event.key === ' ' ? 'space' : event.key.toLowerCase();
  if (key === 'escape') {
    event.preventDefault();
    if (!event.repeat) action('emergency');
    return;
  }
  const target = event.target;
  const editingText = target.isContentEditable || target.tagName === 'TEXTAREA'
    || (target.tagName === 'INPUT' && !['range', 'checkbox', 'radio', 'button'].includes(target.type));
  if (editingText || target.tagName === 'SELECT') return;
  if (key === 'space' && ['checkbox', 'radio'].includes(target.type)) return;
  if (movementKeys.includes(key)) {
    event.preventDefault();
    if (state.enabled) keys.add(key);
  } else if (key === 'f') {
    event.preventDefault(); neutral();
  } else if ((key === 't' || key === 'l') && !event.repeat) {
    event.preventDefault(); action(key === 't' ? 'takeoff' : 'land');
  }
});
document.addEventListener('keyup', event => {
  keys.delete(event.key === ' ' ? 'space' : event.key.toLowerCase());
});
setInterval(() => input(), 100);
setInterval(poll, 250);
poll();
