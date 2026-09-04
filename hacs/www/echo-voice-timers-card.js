/* EchoMuse timer card.
 *
 * Home Assistant's TimerManager remains the source of truth (see
 * docs/design/timers-design.md "Data Contract") — this card talks to it,
 * and to the controller's ringing/queued alarm state, only through the
 * `echo_voice_satellite/timers/*` WebSocket commands the HACS integration
 * registers (custom_components/echo_voice_satellite/timer_card.py). It
 * creates no `timer.*` entities, no template sensors, and no second
 * persistence store of its own.
 */
class EchoVoiceTimersCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._timersData = [];
    this._devices = [];
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._subscribe();
    this._render();
  }

  getCardSize() {
    return 2;
  }

  connectedCallback() {
    // Redraws the live countdowns from the already-known `finishes_at`
    // timestamps. This never refetches — see the card's module docstring
    // and docs/design/timers-design.md's "Data Contract": the backend
    // pushes a snapshot on every change via `subscribe`, and the frontend
    // computes the countdown itself in between.
    this._tick = window.setInterval(() => this._render(), 1000);
  }

  disconnectedCallback() {
    window.clearInterval(this._tick);
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
  }

  async _subscribe() {
    if (!this._hass?.connection || this._subscribing) return;
    this._subscribing = true;
    try {
      this._unsubscribe = await this._hass.connection.subscribeMessage(
        (snapshot) => {
          this._timersData = snapshot.timers || [];
          this._devices = snapshot.devices || [];
          this._render();
        },
        { type: "echo_voice_satellite/timers/subscribe" },
      );
    } catch (_error) {
      // Leave the card on its empty state rather than throwing out of a
      // property setter — a stale integration or a mid-reload HA is not
      // this card's failure to report.
    } finally {
      this._subscribing = false;
    }
  }

  _call(type, body) {
    return this._hass.connection.sendMessagePromise({ type: `echo_voice_satellite/timers/${type}`, ...body });
  }

  _visibleTimers() {
    const filterDeviceId = this._config.device_id;
    const timers = filterDeviceId
      ? this._timersData.filter((timer) => timer.device_id === filterDeviceId)
      : this._timersData;
    // Ringing first (needs the user's attention), then queued, then
    // active/paused by soonest-first — a stable ordering that does not
    // reshuffle rows on every 1s countdown redraw.
    const rank = { ringing: 0, queued: 1, active: 2, paused: 2 };
    return [...timers].sort((a, b) => {
      const byRank = (rank[a.state] ?? 3) - (rank[b.state] ?? 3);
      if (byRank !== 0) return byRank;
      return (a.remaining_seconds ?? 0) - (b.remaining_seconds ?? 0);
    });
  }

  _remainingText(timer) {
    if (timer.state === "ringing") return "Ringing";
    if (timer.state === "queued") return "Queued";
    if (timer.state === "paused") return this._durationText(timer.remaining_seconds);
    if (!timer.finishes_at) return this._durationText(timer.remaining_seconds);
    const seconds = Math.max(0, Math.round((Date.parse(timer.finishes_at) - Date.now()) / 1000));
    return this._durationText(seconds);
  }

  _durationText(totalSeconds) {
    const seconds = Math.max(0, Math.round(totalSeconds || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    const parts = hours ? [hours, minutes, secs] : [minutes, secs];
    return parts.map((part, i) => (i === 0 ? String(part) : String(part).padStart(2, "0"))).join(":");
  }

  _button(label, action, { danger = false } = {}) {
    const button = document.createElement("button");
    button.textContent = label;
    if (danger) button.classList.add("danger");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      action();
    });
    return button;
  }

  _actionsFor(timer) {
    const actions = document.createElement("div");
    actions.className = "actions";
    if (timer.state === "ringing") {
      actions.append(this._button("Dismiss", () => this._call("dismiss", { timer_id: timer.id }), { danger: true }));
      return actions;
    }
    if (timer.state === "queued") {
      return actions; // nothing to do yet — it starts ringing on its own
    }
    actions.append(
      this._button(timer.state === "active" ? "Pause" : "Resume", () =>
        this._call(timer.state === "active" ? "pause" : "resume", { timer_id: timer.id })),
      this._button("+1m", () => this._call("change", { timer_id: timer.id, seconds: 60 })),
      this._button("-1m", () => this._call("change", { timer_id: timer.id, seconds: -60 })),
      this._button("Cancel", () => this._call("cancel", { timer_id: timer.id }), { danger: true }),
    );
    return actions;
  }

  _renderCreationForm() {
    const form = document.createElement("form");
    form.className = "create";

    const deviceId = this._config.device_id;
    let deviceSelect = null;
    if (!deviceId) {
      deviceSelect = document.createElement("select");
      for (const device of this._devices) {
        const option = document.createElement("option");
        option.value = device.device_id;
        option.textContent = device.device_name || device.device_id;
        deviceSelect.append(option);
      }
      form.append(deviceSelect);
    }

    const minutes = document.createElement("input");
    minutes.type = "number";
    minutes.min = "0";
    minutes.placeholder = "Minutes";
    minutes.required = true;
    form.append(minutes);

    const name = document.createElement("input");
    name.type = "text";
    name.placeholder = "Name (optional)";
    form.append(name);

    const submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = "Start timer";
    form.append(submit);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const targetDevice = deviceId || deviceSelect?.value;
      if (!targetDevice || !minutes.value) return;
      this._call("start", {
        device_id: targetDevice,
        minutes: Number(minutes.value),
        name: name.value || null,
      });
      minutes.value = "";
      name.value = "";
    });

    return form;
  }

  _render() {
    if (!this.shadowRoot || !this._config || !this._hass) return;
    const timers = this._visibleTimers();
    this.shadowRoot.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      :host { display:block; }
      ha-card { padding: 16px; }
      h2 { font-size: 1.1rem; margin: 0 0 12px; }
      .empty { color: var(--secondary-text-color); margin-bottom: 12px; }
      .timer { border-top: 1px solid var(--divider-color); padding: 12px 0 4px; }
      .timer:first-of-type { border-top: 0; padding-top: 0; }
      .timer.ringing { animation: em-pulse 1.4s ease-in-out infinite; }
      @keyframes em-pulse { 0%,100% { opacity:1; } 50% { opacity:.55; } }
      .row { align-items:center; display:flex; gap:12px; justify-content:space-between; }
      .name { font-weight: 500; }
      .device { color: var(--secondary-text-color); font-size:.75rem; }
      .remaining { color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
      .timer.ringing .remaining { color: var(--error-color, #db4437); font-weight: 600; }
      .state { color: var(--primary-color); font-size:.8rem; text-transform:capitalize; }
      .actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
      button { background:var(--ha-card-background,var(--card-background-color)); border:1px solid var(--divider-color); border-radius:6px; color:var(--primary-text-color); cursor:pointer; padding:5px 9px; }
      button:disabled { cursor:default; opacity:.45; }
      button.danger { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); }
      .create { align-items:center; display:flex; flex-wrap:wrap; gap:8px; }
      .create input, .create select { background:var(--ha-card-background,var(--card-background-color)); border:1px solid var(--divider-color); border-radius:6px; color:var(--primary-text-color); padding:5px 9px; }
      .create input[type="number"] { width: 5em; }
    `;
    this.shadowRoot.append(style);
    const card = document.createElement("ha-card");
    const title = document.createElement("h2");
    title.textContent = this._config.title || "Timers";
    card.append(title);

    if (!timers.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No timers";
      card.append(empty);
      card.append(this._renderCreationForm());
      this.shadowRoot.append(card);
      return;
    }

    for (const timer of timers) {
      const section = document.createElement("section");
      section.className = `timer ${timer.state}`;
      const row = document.createElement("div");
      row.className = "row";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = timer.name || this._durationText(timer.duration_seconds) + " timer";
      const remaining = document.createElement("span");
      remaining.className = "remaining";
      remaining.textContent = this._remainingText(timer);
      row.append(name, remaining);
      section.append(row);
      if (!this._config.device_id && timer.device_name) {
        const device = document.createElement("div");
        device.className = "device";
        device.textContent = timer.device_name;
        section.append(device);
      }
      const state = document.createElement("div");
      state.className = "state";
      state.textContent = timer.state;
      section.append(state);
      section.append(this._actionsFor(timer));
      card.append(section);
    }
    this.shadowRoot.append(card);
  }
}

customElements.define("echo-voice-timers-card", EchoVoiceTimersCard);
