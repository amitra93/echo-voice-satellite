/* EchoMuse alarms card.
 *
 * Alarms are controller-owned (Home Assistant has no alarm concept — see
 * docs/design/alarms-design.md), so this card talks to the controller only
 * through the `echo_voice_satellite/alarms/*` WebSocket commands the HACS
 * integration registers (custom_components/echo_voice_satellite/alarm_card.py).
 * It creates no entities and no second store. There is no live countdown to
 * animate — an alarm is a wall-clock time — so it lists on load and after each
 * action, with a slow periodic refresh, rather than subscribing per second.
 */
class EchoVoiceAlarmsCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._alarms = [];
    this._devices = [];
    this._error = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._refresh();
  }

  getCardSize() {
    return 3;
  }

  connectedCallback() {
    this._timer = window.setInterval(() => this._refresh(), 30000);
    this._refresh();
  }

  disconnectedCallback() {
    window.clearInterval(this._timer);
  }

  _call(type, body) {
    return this._hass.connection.sendMessagePromise({
      type: `echo_voice_satellite/alarms/${type}`,
      ...body,
    });
  }

  async _refresh() {
    if (!this._hass?.connection) return;
    try {
      const reply = await this._call("list", {});
      this._alarms = reply.alarms || [];
      this._devices = reply.devices || [];
      this._error = null;
      this._render();
    } catch (error) {
      // An empty list is a real answer. Do not disguise a missing command or
      // controller failure as "No alarms set.".
      this._error = `Couldn't load alarms: ${error?.message || error}`;
      this._render();
    }
  }

  _visibleAlarms() {
    const filter = this._config.device_id;
    const alarms = filter
      ? this._alarms.filter((a) => a.device_id === filter)
      : this._alarms;
    return [...alarms].sort(
      (a, b) => a.hour * 60 + a.minute - (b.hour * 60 + b.minute),
    );
  }

  _timeText(alarm) {
    const ampm = alarm.hour < 12 ? "AM" : "PM";
    const h12 = alarm.hour % 12 || 12;
    return `${h12}:${String(alarm.minute).padStart(2, "0")} ${ampm}`;
  }

  _recurrenceText(alarm) {
    const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    if (alarm.recurrence === "daily") return "Every day";
    if (alarm.recurrence === "once") return alarm.date || "Once";
    if (alarm.recurrence === "weekly") {
      const mask = alarm.weekday_mask || 0;
      if (mask === 0b0011111) return "Weekdays";
      if (mask === 0b1100000) return "Weekends";
      const days = DAYS.filter((_d, i) => (mask >> i) & 1);
      return days.join(", ") || "Weekly";
    }
    return "";
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

  async _act(promise, failure) {
    try {
      await promise;
    } catch (error) {
      this._error = `${failure}: ${error?.message || error}`;
      this._render();
      return;
    }
    this._refresh();
  }

  _rowFor(alarm) {
    const row = document.createElement("div");
    row.className = "row" + (alarm.enabled ? "" : " disabled");

    const info = document.createElement("div");
    info.className = "info";
    const time = document.createElement("div");
    time.className = "time";
    time.textContent = this._timeText(alarm);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent =
      this._recurrenceText(alarm) +
      (alarm.label ? ` · ${alarm.label}` : "") +
      (!alarm.enabled ? " · Completed" : "");
    info.append(time, meta);

    const actions = document.createElement("div");
    actions.className = "actions";
    actions.append(
      this._button(
        "Delete",
        () =>
          this._act(
            this._call("cancel", { device_id: alarm.device_id, alarm_id: alarm.id }),
            "Couldn't delete alarm",
          ),
        { danger: true },
      ),
    );

    row.append(info, actions);
    return row;
  }

  _creationForm() {
    const form = document.createElement("form");
    form.className = "create";

    let deviceSelect = null;
    if (!this._config.device_id) {
      deviceSelect = document.createElement("select");
      for (const device of this._devices) {
        const option = document.createElement("option");
        option.value = device.device_id;
        option.textContent = device.device_name || device.device_id;
        deviceSelect.append(option);
      }
      form.append(deviceSelect);
    }

    const time = document.createElement("input");
    time.type = "time";
    time.required = true;
    form.append(time);

    const recurrence = document.createElement("select");
    for (const [value, label] of [
      ["once", "Once"],
      ["daily", "Every day"],
      ["weekdays", "Weekdays"],
      ["weekends", "Weekends"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      recurrence.append(option);
    }
    form.append(recurrence);

    const label = document.createElement("input");
    label.type = "text";
    label.placeholder = "Label (optional)";
    form.append(label);

    const submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = "Add alarm";
    form.append(submit);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const deviceId = this._config.device_id || deviceSelect?.value;
      if (!deviceId || !time.value) return;
      const [hour, minute] = time.value.split(":").map((n) => parseInt(n, 10));
      const choice = recurrence.value;
      const body = { device_id: deviceId, hour, minute, label: label.value || null };
      if (choice === "weekdays") {
        body.recurrence = "weekly";
        body.weekday_mask = 0b0011111;
      } else if (choice === "weekends") {
        body.recurrence = "weekly";
        body.weekday_mask = 0b1100000;
      } else {
        body.recurrence = choice; // once | daily
      }
      this._act(this._call("create", body), "Couldn't create alarm");
      form.reset();
    });

    return form;
  }

  _render() {
    if (!this.shadowRoot) return;
    const root = this.shadowRoot;
    root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = `
      .card { padding: 16px; font-family: var(--paper-font-body1_-_font-family, sans-serif); }
      h2 { margin: 0 0 12px; font-size: 1.1rem; }
      .row { display: flex; align-items: center; justify-content: space-between;
             padding: 8px 0; border-top: 1px solid var(--divider-color, #eee); }
      .row.disabled .time, .row.disabled .meta { opacity: 0.45; }
      .time { font-size: 1.3rem; font-weight: 600; }
      .meta { font-size: 0.85rem; color: var(--secondary-text-color, #777); }
      .actions { display: flex; gap: 6px; }
      button { cursor: pointer; border-radius: 6px; border: 1px solid var(--divider-color, #ccc);
               background: var(--card-background-color, #fff); padding: 4px 10px; }
      button.danger { color: var(--error-color, #c00); }
      .empty { color: var(--secondary-text-color, #777); padding: 8px 0; }
      .error { color: var(--error-color, #c00); padding: 8px 0; }
      form.create { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px;
                    padding-top: 12px; border-top: 1px solid var(--divider-color, #eee); }
      form.create input, form.create select { padding: 5px; }
    `;

    const card = document.createElement("ha-card");
    const body = document.createElement("div");
    body.className = "card";

    const title = document.createElement("h2");
    title.textContent = this._config.title || "Alarms";
    body.append(title);

    if (this._error) {
      const error = document.createElement("div");
      error.className = "error";
      error.textContent = this._error;
      body.append(error);
    }

    const alarms = this._visibleAlarms();
    if (alarms.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No alarms set.";
      body.append(empty);
    } else {
      for (const alarm of alarms) body.append(this._rowFor(alarm));
    }

    body.append(this._creationForm());
    card.append(body);
    root.append(style, card);
  }
}

customElements.define("echo-voice-alarms-card", EchoVoiceAlarmsCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "echo-voice-alarms-card",
  name: "EchoMuse Alarms Card",
  description: "Create and manage EchoMuse device alarms.",
});
