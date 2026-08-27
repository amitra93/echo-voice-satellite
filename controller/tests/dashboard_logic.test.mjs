import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/dashboard_logic.js', import.meta.url), 'utf8');
const document = { baseURI: 'https://example.test/api/hassio_ingress/token/' };
const window = {};
vm.runInNewContext(source, { window, document, URL, globalThis: window });
const logic = window.EchoMuseDashboardLogic;

test('ingress paths and websocket URLs', () => {
  assert.equal(logic.ingressPath('/api/devices'), './api/devices');
  assert.equal(logic.ingressPath('relative'), 'relative');
  assert.equal(logic.isIngress.call({}), true);
  assert.equal(logic.ingressWebSocketUrl('/api/events'), 'wss://example.test/api/hassio_ingress/token/api/events');
});

test('labels, durations, relative times, and Wi-Fi bands', () => {
  assert.equal(logic.wwModelLabel('/models/hey_jarvis_v0.1.onnx'), 'hey jarvis');
  assert.equal(logic.wwModelLabel(''), '—');
  assert.equal(logic.uptime(90061), '1d 1h');
  assert.equal(logic.uptime(3600), '1h 0m');
  assert.equal(logic.uptime(60), '1m');
  assert.equal(logic.relTime(0), '—');
  assert.equal(logic.relTime(100, 100500), '0s ago');
  assert.equal(logic.relTime(100, 161000), '1m ago');
  assert.equal(logic.relTime(100, 3700000), '1h ago');
  assert.equal(logic.relTime(100, 90000000), '1d ago');
  assert.equal(logic.wifiBand(null), null);
  assert.equal(logic.wifiBand(2412), '2.4GHz');
  assert.equal(logic.wifiBand(5180), '5GHz');
});

test('device state priority and event accents', () => {
  assert.equal(logic.deviceState({ approved: false }).key, 'pending');
  assert.equal(logic.deviceState({ approved: true, connected: false }).key, 'offline');
  assert.equal(logic.deviceState({ approved: true, connected: true, muted: true }).key, 'muted');
  assert.equal(logic.deviceState({ approved: true, connected: true, speaking: true, thinking: true }).key, 'speaking');
  assert.equal(logic.deviceState({ approved: true, connected: true, thinking: true }).key, 'thinking');
  assert.equal(logic.deviceState({ approved: true, connected: true, listening: true }).key, 'listening');
  assert.equal(logic.deviceState({ approved: true, connected: true }).key, 'idle');
  assert.equal(logic.eventAccent('info'), 'var(--ok)');
  assert.equal(logic.eventAccent('unknown'), 'var(--muted)');
});

test('turn, banner, configuration, and wake-mode decisions', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(logic.turnSegments({ stt_latency_ms: -1, ha_latency_ms: 2, tts_latency_ms: 3 }))), { stt: 0, ha: 2, tts: 3, shown: 5 });
  const values = [900, 100, 500, 300, 700];
  assert.equal(logic.percentile(values, 0.50), 500);
  assert.equal(logic.percentile(values, 0.90), 900);
  assert.equal(logic.percentile(values, 0.99), 900);
  assert.deepEqual(values, [900, 100, 500, 300, 700]);
  assert.equal(logic.percentile([], 0.50), null);
  assert.equal(logic.bannerMode('omni_biscuit'), 'twrp');
  assert.equal(logic.bannerMode('CSM biscuit'), 'android');
  assert.equal(logic.bannerMode('unknown'), 'unknown');
  const config = logic.effectiveConfig(
    { wake: 'fleet', ring: 'fleet', startupVolume: 50 },
    { config_sections: ['wakeword'], config: { wake: 'device', ring: 'stale', startupVolume: 80 } },
    { wake: 'wakeword', ring: 'ring' },
    ['startupVolume'],
  );
  assert.deepEqual(JSON.parse(JSON.stringify(config)), { wake: 'device', ring: 'fleet', startupVolume: 80 });
  assert.equal(logic.onDeviceMode({ owwOnDevice: 'SHADOW' }), 'shadow');
  assert.equal(logic.onDeviceMode({ owwOnDevice: 'bad' }), 'off');
});
