/**
 * Catchable departures card.
 *
 * A small, self-contained Lovelace card for the Catchable integration. It reads
 * a departure sensor's `departures` attribute and renders one row per service:
 * line + direction on the left, minutes right-aligned on the right. Status is
 * colour coded (on time = green, delayed = yellow, cancelled = red) and
 * localized in English (default) and German.
 *
 * The integration registers this file automatically, so no dashboard resource
 * is required.
 *
 * Usage:
 *   type: custom:catchable-departures-card
 *   entity: sensor.your_stop_departures
 *   title: optional override (defaults to the entity's friendly name)
 *   line_colors: true   # official Berlin U-/S-Bahn line colours (default: true)
 *   ring_symbols: true  # ⟳ / ⟲ for the S41 / S42 Ringbahn (default: true)
 */

const STRINGS = {
  en: {
    none: "No departures within reach right now.",
    cancelled: "cancelled",
    min: "min",
    late: (n) => `(${n} min late)`,
    cached: "Cached data — live feed temporarily unavailable.",
    unavailable: "Sensor unavailable.",
    pickEntity: "Pick a Catchable departure sensor.",
  },
  de: {
    none: "Derzeit keine erreichbaren Abfahrten.",
    cancelled: "ausgefallen",
    min: "Min.",
    late: (n) => `(${n} Min. verspätet)`,
    cached: "Zwischengespeicherte Daten — Echtzeit-Feed derzeit nicht verfügbar.",
    unavailable: "Sensor nicht verfügbar.",
    pickEntity: "Bitte einen Catchable-Abfahrtssensor wählen.",
  },
};

// Official Berlin U-Bahn and S-Bahn line colours. Keys are normalized line
// tokens (mode letter + number, e.g. "U1", "S41"). Sources: BVG / S-Bahn
// Berlin corporate line colours.
const LINE_COLORS = {
  U1: "#7DAD4C", U2: "#DA421E", U3: "#16683D", U4: "#F0D722", U5: "#7E5330",
  U6: "#8C6DAB", U7: "#528DBA", U8: "#224F86", U9: "#F3791D",
  S1: "#DE4DA4", S2: "#005F27", S25: "#005F27", S26: "#005F27", S3: "#0066AD",
  S41: "#A23C1C", S42: "#CB6418", S45: "#CD9C2E", S46: "#CD9C2E", S47: "#CD9C2E",
  S5: "#EB7405", S7: "#816DAA", S75: "#816DAA", S8: "#5CB531", S85: "#5CB531",
  S9: "#8D2233",
};

// Fallback colours per transport category when a line has no specific colour.
const CATEGORY_COLORS = {
  subway: "#224F86",
  suburban: "#007A33",
  regional: "#EC0016",
  tram: "#D71C24",
  bus: "#95276E",
  ferry: "#0098D4",
};

class CatchableDeparturesCard extends HTMLElement {
  setConfig(config) {
    // Accept an empty entity (e.g. the card picker's stub config) without
    // throwing — a hard throw here surfaces as "Configuration error" in the
    // add-card dialog. We render a friendly hint instead when none is set.
    this._config = config || {};
    // Display toggles default to on.
    this._lineColors = this._config.line_colors !== false;
    this._ringSymbols = this._config.ring_symbols !== false;
    this._lastKey = null;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _t() {
    const lang = (this._hass && this._hass.language ? this._hass.language : "en")
      .split("-")[0]
      .toLowerCase();
    return STRINGS[lang] || STRINGS.en;
  }

  // Normalized U-/S-Bahn token ("U 1" -> "U1", "S 41" -> "S41"), else null.
  _lineKey(line) {
    const s = String(line || "").trim();
    let m = s.match(/^U\s*0*(\d+)/i);
    if (m) return "U" + m[1];
    m = s.match(/^S\s*0*(\d+)/i);
    if (m) return "S" + m[1];
    return null;
  }

  _category(line) {
    const s = String(line || "").trim();
    if (/^U\s*\d/i.test(s)) return "subway";
    if (/^S\s*\d/i.test(s)) return "suburban";
    if (/^Tram/i.test(s)) return "tram";
    if (/^Bus/i.test(s)) return "bus";
    if (/^(Ferry|F\d)/i.test(s)) return "ferry";
    return "regional";
  }

  _lineColor(line) {
    const key = this._lineKey(line);
    if (key && LINE_COLORS[key]) return LINE_COLORS[key];
    return CATEGORY_COLORS[this._category(line)] || null;
  }

  // Ring symbol for the Berlin Ringbahn: S41 runs clockwise, S42 anti-clockwise.
  _ringSymbol(line) {
    const key = this._lineKey(line);
    if (key === "S41") return "\u27F3"; // ⟳
    if (key === "S42") return "\u27F2"; // ⟲
    return "";
  }

  // Pick a readable text colour for a given background.
  _contrast(hex) {
    const c = String(hex).replace("#", "");
    if (c.length < 6) return "#fff";
    const r = parseInt(c.substr(0, 2), 16);
    const g = parseInt(c.substr(2, 2), 16);
    const b = parseInt(c.substr(4, 2), 16);
    const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return luminance > 0.6 ? "#111" : "#fff";
  }

  _render() {
    if (!this._hass || !this._config) return;
    const t = this._t();

    if (!this._config.entity) {
      this._paint(
        this._config.title || "Catchable",
        `<div class="empty">${t.pickEntity}</div>`
      );
      return;
    }

    const stateObj = this._hass.states[this._config.entity];

    if (!stateObj) {
      this._paint(
        this._config.title || this._config.entity,
        `<div class="empty">${t.unavailable}</div>`
      );
      return;
    }

    const attrs = stateObj.attributes || {};
    const title =
      this._config.title || attrs.friendly_name || this._config.entity;
    const departures = Array.isArray(attrs.departures) ? attrs.departures : [];

    // Cheap change detection so we don't rebuild the DOM every state poll.
    const key = JSON.stringify([
      title,
      attrs.stale,
      departures,
      this._lineColors,
      this._ringSymbols,
    ]);
    if (key === this._lastKey) return;
    this._lastKey = key;

    let body = "";
    if (attrs.stale) {
      body += `<div class="stale">${t.cached}</div>`;
    }

    if (departures.length === 0) {
      body += `<div class="empty">${t.none}</div>`;
    } else {
      body += '<div class="rows">';
      for (const d of departures) {
        const cancelled = !!d.cancelled;
        const delay = Number(d.delay_min || 0);
        const status = cancelled ? "cancelled" : delay >= 1 ? "delayed" : "ontime";
        const arrow = (d.kind || "departure") === "arrival" ? "←" : "→";
        const rawLine = d.line || "—";
        const place = this._esc(d.direction || "—");

        // Line badge: optional official colour + optional Ringbahn symbol.
        const ring = this._ringSymbols ? this._ringSymbol(rawLine) : "";
        const lineText = this._esc(rawLine) + (ring ? `\u00a0${ring}` : "");
        const color = this._lineColors ? this._lineColor(rawLine) : null;
        const lineCls = color ? "line badge" : "line";
        const lineStyle = color
          ? ` style="background:${color};color:${this._contrast(color)}"`
          : "";

        let right;
        if (cancelled) {
          right = `<span class="x">${t.cancelled}</span>`;
        } else {
          const mins = d.departure_in_min;
          const minTxt =
            mins === null || mins === undefined ? "—" : `${mins}\u00a0${t.min}`;
          const note = delay >= 1 ? `<span class="note">${t.late(delay)}</span>` : "";
          right = `<span class="mins">${minTxt}</span>${note}`;
        }

        body +=
          `<div class="row ${status}">` +
          `<span class="left"><span class="${lineCls}"${lineStyle}>${lineText}</span>` +
          `<span class="arrow">${arrow}</span>` +
          `<span class="place">${place}</span></span>` +
          `<span class="right">${right}</span>` +
          `</div>`;
      }
      body += "</div>";
    }

    this._paint(this._esc(title), body);
  }

  _paint(title, body) {
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this._style = document.createElement("style");
      this._style.textContent = this._css();
      this._content = document.createElement("div");
      this._content.className = "catchable-content";
      this._card.appendChild(this._style);
      this._card.appendChild(this._content);
      this.appendChild(this._card);
    }
    this._card.setAttribute("header", title);
    this._content.innerHTML = body;
  }

  _css() {
    return `
      .catchable-content { padding: 4px 16px 12px; }
      .rows { display: flex; flex-direction: column; }
      .row {
        display: flex; align-items: center; justify-content: space-between;
        gap: 12px; padding: 8px 10px; margin: 4px 0; border-radius: 10px;
        border-left: 4px solid var(--divider-color);
        background: var(--ha-card-background, var(--card-background-color));
      }
      .row.ontime { border-left-color: var(--success-color, #2e7d32);
        background: color-mix(in srgb, var(--success-color, #2e7d32) 10%, transparent); }
      .row.delayed { border-left-color: var(--warning-color, #f9a825);
        background: color-mix(in srgb, var(--warning-color, #f9a825) 14%, transparent); }
      .row.cancelled { border-left-color: var(--error-color, #c62828);
        background: color-mix(in srgb, var(--error-color, #c62828) 12%, transparent); }
      .left { display: flex; align-items: center; gap: 8px; min-width: 0; }
      .line { font-weight: 600; white-space: nowrap; }
      .line.badge {
        display: inline-block; padding: 1px 8px; border-radius: 6px;
        font-weight: 700; min-width: 1.6em; text-align: center;
      }
      .arrow { opacity: 0.6; }
      .place { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .right {
        display: flex; flex-direction: column; align-items: flex-end;
        text-align: right; white-space: nowrap; flex: 0 0 auto;
      }
      .mins { font-variant-numeric: tabular-nums; font-weight: 600; }
      .note { font-size: 0.8em; opacity: 0.8; }
      .x { color: var(--error-color, #c62828); font-weight: 600; }
      .stale {
        font-size: 0.85em; opacity: 0.75; font-style: italic;
        padding: 4px 10px 8px;
      }
      .empty { opacity: 0.7; padding: 12px 10px; }
    `;
  }

  _esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  getCardSize() {
    const stateObj =
      this._hass && this._config
        ? this._hass.states[this._config.entity]
        : null;
    const n =
      stateObj && Array.isArray(stateObj.attributes.departures)
        ? stateObj.attributes.departures.length
        : 3;
    return 1 + Math.min(n, 8);
  }

  static getStubConfig(hass) {
    let entity = "";
    if (hass && hass.states) {
      const match = Object.keys(hass.states).find(
        (id) =>
          id.startsWith("sensor.") &&
          Array.isArray(hass.states[id].attributes.departures)
      );
      if (match) entity = match;
    }
    return { entity };
  }
}

customElements.define("catchable-departures-card", CatchableDeparturesCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "catchable-departures-card",
  name: "Catchable Departures",
  description: "Departure board for the Catchable GTFS-RT integration.",
});

console.info("%c CATCHABLE-DEPARTURES-CARD ", "color: white; background: #1565c0;");
