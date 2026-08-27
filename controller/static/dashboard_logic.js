// Pure dashboard decisions shared by the classic browser bundle and tests.
(function (root) {
  function ingressPath(path) {
    return path.startsWith('/') ? `.${path}` : path;
  }

  function isIngress() {
    return document.baseURI.includes('/hassio_ingress/');
  }

  function ingressWebSocketUrl(path) {
    const url = new URL(ingressPath(path), document.baseURI);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.toString();
  }

  function wwModelLabel(v) {
    if (!v) return '—';
    if (v.endsWith('.onnx')) v = v.split('/').pop().replace(/\.onnx$/, '');
    return v.replace(/_v[\d.]+$/, '').replace(/_/g, ' ');
  }

  function uptime(s) {
    if (!s) return '—';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }

  function relTime(ts, now = Date.now()) {
    if (!ts) return '—';
    const d = now - ts * 1000;
    if (d < 60000) return `${Math.floor(d / 1000)}s ago`;
    if (d < 3600000) return `${Math.floor(d / 60000)}m ago`;
    if (d < 86400000) return `${Math.floor(d / 3600000)}h ago`;
    return `${Math.floor(d / 86400000)}d ago`;
  }

  function deviceState(d) {
    if (!d.approved) return { key: 'pending', label: 'Pending', color: 'var(--accent-hi)', dot: '#8ab0d0' };
    if (!d.connected) return { key: 'offline', label: 'Offline', color: 'var(--warn)', dot: '#d4703a' };
    if (d.muted) return { key: 'muted', label: 'Muted', color: 'var(--error)', dot: '#c04040' };
    if (d.speaking) return { key: 'speaking', label: 'Speaking', color: 'var(--accent)', dot: '#4080d0' };
    if (d.thinking) return { key: 'thinking', label: 'Thinking', color: 'var(--warn)', dot: '#a08020' };
    if (d.listening) return { key: 'listening', label: 'Listening', color: 'var(--ok)', dot: '#40906a' };
    return { key: 'idle', label: 'Idle', color: 'var(--muted)', dot: '#aaaaaa' };
  }

  function eventAccent(level) {
    return { info: 'var(--ok)', warn: 'var(--warn)', error: 'var(--error)' }[level] || 'var(--muted)';
  }

  function wifiBand(freqMhz) {
    if (!freqMhz) return null;
    return freqMhz >= 4900 ? '5GHz' : '2.4GHz';
  }

  function turnSegments(t) {
    const stt = Math.max(t.stt_latency_ms || 0, 0);
    const ha = Math.max(t.ha_latency_ms || 0, 0);
    const tts = Math.max(t.tts_latency_ms || 0, 0);
    return { stt, ha, tts, shown: stt + ha + tts };
  }

  // Nearest-rank percentile. Keep the input untouched because callers may
  // reuse the turn list for rendering and filtering.
  function percentile(values, p) {
    const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
    if (!sorted.length) return null;
    return sorted[Math.max(0, Math.ceil(sorted.length * p) - 1)];
  }

  function bannerMode(banner) {
    const b = (banner || '').toLowerCase();
    if (b.includes('omni') || b.includes('twrp') || b.includes('recovery')) return 'twrp';
    if (b.includes('csm') || b.includes('biscuit')) return 'android';
    return 'unknown';
  }

  function effectiveConfig(globalConfig, device, keySection = {}, stateKeys = ['startupVolume']) {
    const secs = device.config_sections ?? [];
    const own = device.config || {};
    const out = { ...(globalConfig || {}) };
    Object.keys(own).forEach(k => {
      const sec = keySection[k];
      if ((sec && secs.includes(sec)) || stateKeys.includes(k)) out[k] = own[k];
    });
    return out;
  }

  function onDeviceMode(config) {
    const v = String(config.owwOnDevice ?? 'off').toLowerCase();
    return ['off', 'shadow', 'on'].includes(v) ? v : 'off';
  }

  root.EchoMuseDashboardLogic = {
    ingressPath, isIngress, ingressWebSocketUrl, wwModelLabel, uptime, relTime,
    deviceState, eventAccent, wifiBand, turnSegments, percentile, bannerMode,
    effectiveConfig, onDeviceMode,
  };
})(typeof window !== 'undefined' ? window : globalThis);
