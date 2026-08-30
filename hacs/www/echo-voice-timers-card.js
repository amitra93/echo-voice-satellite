/* EchoMuse timer card. Uses Home Assistant's native timer entities and
 * services; it does not create a second timer store. */
class EchoVoiceTimersCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) this._loadTimers();
    this._render();
  }

  getCardSize() {
    return 2;
  }

  connectedCallback() {
    this._tick = window.setInterval(() => this._loadTimers(), 1000);
  }

  disconnectedCallback() {
    window.clearInterval(this._tick);
  }

  async _loadTimers() {
    if (!this._hass?.callWS || this._loading) return;
    this._loading = true;
    try {
      const result = await this._hass.callWS({ type: "echo_voice_satellite/timers" });
      this._timersData = result.timers || [];
      this._loaded = true;
      this._render();
    } catch (_error) {
      this._timersData = [];
    } finally {
      this._loading = false;
    }
  }

  _remaining(timer) {
    if (!timer.is_active) return "Paused";
    const seconds = Math.max(0, timer.seconds_left);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  _service(timer, action) {
    this._hass.callWS({
      type: "echo_voice_satellite/timers",
      action,
      timer_id: timer.id,
    }).then(() => this._loadTimers());
  }

  _button(label, disabled, action) {
    const button = document.createElement("button");
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("click", action);
    return button;
  }

  _render() {
    if (!this.shadowRoot || !this._config || !this._hass) return;
    const timers = this._timersData || [];
    this.shadowRoot.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      :host { display:block; }
      ha-card { padding: 16px; }
      h2 { font-size: 1.1rem; margin: 0 0 12px; }
      .empty { color: var(--secondary-text-color); }
      .timer { border-top: 1px solid var(--divider-color); padding: 12px 0 4px; }
      .timer:first-of-type { border-top: 0; padding-top: 0; }
      .row { align-items:center; display:flex; gap:12px; justify-content:space-between; }
      .name { font-weight: 500; }
      .remaining { color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
      .state { color: var(--primary-color); font-size:.8rem; text-transform:capitalize; }
      .actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
      button { background:var(--ha-card-background,var(--card-background-color)); border:1px solid var(--divider-color); border-radius:6px; color:var(--primary-text-color); cursor:pointer; padding:5px 9px; }
      button:disabled { cursor:default; opacity:.45; }
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
    }
    for (const timer of timers) {
      const section = document.createElement("section");
      section.className = "timer";
      const row = document.createElement("div");
      row.className = "row";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = timer.name || timer.area_name || "Unnamed timer";
      const remaining = document.createElement("span");
      remaining.className = "remaining";
      remaining.textContent = this._remaining(timer);
      row.append(name, remaining);
      section.append(row);
      const state = document.createElement("div");
      state.className = "state";
      state.textContent = timer.is_active ? "active" : "paused";
      section.append(state);
      const actions = document.createElement("div");
      actions.className = "actions";
      const pauseOrResume = timer.is_active ? "pause" : "resume";
      actions.append(
        this._button(timer.is_active ? "Pause" : "Resume", false, () => this._service(timer, pauseOrResume)),
        this._button("Cancel", false, () => this._service(timer, "cancel")),
        this._button("Finish", false, () => this._service(timer, "finish")),
      );
      section.append(actions);
      card.append(section);
    }
    this.shadowRoot.append(card);
  }
}

customElements.define("echo-voice-timers-card", EchoVoiceTimersCard);
