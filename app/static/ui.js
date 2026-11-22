/* Shared UI kit: icons, plain-language labels, small SVG/HTML charts with
   hover tooltips, toasts and confirm dialogs. No external libraries, so the
   demo works offline. Charts follow one rule set: thin marks, rounded data
   ends, 2px gaps, hairline grid, a legend for 2+ series, a tooltip on hover. */
(function () {
  const P = {
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
    alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.4v.1"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.6-4 4.6-6 8-6s6.4 2 8 6"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c1.2-3.4 3.6-5 6.5-5s5.3 1.6 6.5 5"/><path d="M15.5 4.6a3.5 3.5 0 0 1 0 6.8M18 15.3c1.6.7 2.8 2.2 3.5 4.7"/>',
    file: '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4M9 13h6M9 17h4"/>',
    bank: '<path d="M3 10l9-6 9 6"/><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 21h18"/>',
    shield: '<path d="M12 3l8 3v6c0 4.8-3.4 8-8 9-4.6-1-8-4.2-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-4.5"/>',
    spark: '<path d="M12 3.5l1.8 5.2 5.2 1.8-5.2 1.8L12 17.5l-1.8-5.2L5 10.5l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
    bolt: '<path d="M13 2.5L4.5 14H11l-1 7.5L19.5 10H13z"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5l8.5 6.5 8.5-6.5"/>',
    home: '<path d="M3.5 11L12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/>',
    list: '<path d="M9 6h12M9 12h12M9 18h12"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    chart: '<path d="M4 20V11M10 20V5M16 20v-6M21 20H3"/>',
    sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    upload: '<path d="M12 16V5M7.5 9.5L12 5l4.5 4.5"/><path d="M4 16v3h16v-3"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    refresh: '<path d="M20 11.5A8 8 0 1 1 17.6 6"/><path d="M20 4.5v4.5h-4.5"/>',
    store: '<path d="M4 9.5L5.2 4h13.6L20 9.5"/><path d="M4 9.5h16v.5a2.7 2.7 0 0 1-5.3 0 2.7 2.7 0 0 1-5.4 0A2.7 2.7 0 0 1 4 10z"/><path d="M5.5 12.5V20h13v-7.5M10 20v-5h4v5"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>',
    msg: '<path d="M4 5h16v11H9l-5 4z"/>',
    hourglass: '<path d="M7 3h10M7 21h10"/><path d="M8 3c0 5 8 5 8 9s-8 4-8 9M16 3c0 5-8 5-8 9"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M20.5 20.5l-4.8-4.8"/>',
    rupee: '<path d="M7 4h10M7 9h10M10 4c3.6 0 5.5 2 5.5 5S13 14 9 14h-1l7 6.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>', minus: '<path d="M5 12h14"/>',
    flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>', doc2: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    gauge: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-5"/>',
    party: '<path d="M4 20l5-13 8 8z"/><path d="M14 4l1 2M19 6l-2 1M20 11h-2M9 7l1.5 1.5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.1"/>',
  };
  const icon = (n, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[n] || ""}</svg>`;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const dur = s => {
    if (s == null || isNaN(s)) return "–";
    s = Math.abs(s);
    if (s < 1) return "<1s";
    s = Math.round(s);
    if (s < 60) return `${s}s`;
    if (s < 3600) { const m = Math.floor(s / 60), r = s % 60; return r ? `${m}m ${r}s` : `${m}m`; }
    return `${(s / 3600).toFixed(1)}h`;
  };
  const ago = t => {
    const s = Date.now() / 1000 - t;
    if (s < 10) return "just now";
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(t * 1000).toLocaleDateString();
  };
  const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  const pct = x => `${Math.round((x || 0) * 100)}%`;
  const plain = s => String(s || "").replace(/_/g, " ").toLowerCase().replace(/^\w/, c => c.toUpperCase());
  const initials = n => String(n || "?").split(/[.\s]/).filter(Boolean).pop().slice(0, 2).toUpperCase();
  const avatar = n => n ? `<span class="av ${n.includes("vikram") ? "b" : n.includes("ops") ? "c" : ""}" title="${esc(n)}">${esc(initials(n))}</span>` : "";

  // ---------------------------------------------------------------- plain language
  const STATE = {
    SUBMITTED: {label: "Just submitted", kind: "moving", icon: "hourglass"},
    VERIFYING: {label: "Being verified", kind: "moving", icon: "hourglass"},
    NEEDS_INFO: {label: "Waiting on merchant", kind: "merchant", icon: "mail"},
    MANUAL_REVIEW: {label: "Needs a reviewer", kind: "human", icon: "user"},
    APPROVED: {label: "Approved", kind: "setup", icon: "check"},
    PROVISIONING: {label: "Setting up account", kind: "setup", icon: "hourglass"},
    PROVISIONING_FAILED: {label: "Account setup failed", kind: "human", icon: "alert"},
    ACTIVE: {label: "Live", kind: "live", icon: "check"},
    REJECTED: {label: "Rejected", kind: "stop", icon: "x"},
  };
  const ISSUE = {
    GSTIN_CANCELLED: "GST registration is cancelled", GSTIN_NOT_FOUND: "GSTIN not found", GSTIN_PAN_MISMATCH: "GSTIN belongs to a different PAN",
    PAN_NOT_FOUND: "PAN not found", PAN_NAME_MISMATCH: "Name on PAN doesn't match", BANK_NAME_MISMATCH: "Bank account is in another name",
    BANK_ACCOUNT_INVALID: "Bank account didn't accept the test deposit", RISK_HIGH: "High-risk business", RISK_MEDIUM: "Medium-risk business",
    DOC_FORM_CONFLICT: "Document doesn't match the form", DUPLICATE_APPLICATION: "Duplicate of another application",
    POSSIBLE_DUPLICATE: "Might be a duplicate", DOCUMENT_REUSED: "Same file used by another applicant",
    DOCUMENT_TAMPER_SUSPECTED: "Document may be tampered with", FIELD_VERIFICATION_REQUIRED: "Confirm what the document says",
    MANUAL_EXTRACTION_REQUIRED: "Type in the document details", CATEGORY_MISMATCH: "Business isn't what they declared",
    MID_CREATION_FAILED: "Couldn't create the merchant ID", UNRESOLVED_CHECKS: "Some checks didn't pass",
    PAN_CHECK_EXHAUSTED: "PAN service didn't respond", GST_CHECK_EXHAUSTED: "GST service didn't respond",
    BANK_CHECK_EXHAUSTED: "Bank service didn't respond", RISK_CHECK_EXHAUSTED: "Risk service didn't respond",
    MISSING_DOCUMENT: "Document missing", DOCUMENT_UNREADABLE: "Blurry document", WRONG_DOCUMENT_TYPE: "Wrong document uploaded",
    INVALID_FORMAT: "Detail in the wrong format", MISSING_FIELD: "Detail missing", INCONSISTENT: "Details don't add up",
    FIELD_NOT_FOUND: "Detail not on the document", REVIEWER_REQUEST: "Question from a reviewer",
  };
  const issue = c => ISSUE[c] || plain(c);
  const CHECK = {
    PASSED: ["Verified", "live", "check"], FAILED: ["Failed", "stop", "x"], DEAD: ["No response", "stop", "alert"],
    WAIVED: ["Waived", "human", "flag"], IN_FLIGHT: ["Checking", "moving", "hourglass"], PENDING: ["Starting", "moving", "hourglass"],
    DISPATCHING: ["Starting", "moving", "hourglass"], RETRY_WAIT: ["Retrying soon", "human", "refresh"], BLOCKED: ["Waiting for input", "merchant", "clock"],
    NOT_REQUIRED: ["Not needed", "idle", "minus"], ADJUDICATING: ["AI comparing names", "ai", "spark"],
  };
  const pill = (text, kind, ic) => `<span class="pill p-${kind}">${ic ? icon(ic) : ""}${esc(text)}</span>`;
  const statePill = s => { const m = STATE[s] || {label: plain(s), kind: "idle"}; return pill(m.label, m.kind, m.icon); };
  const checkPill = s => { const m = CHECK[s] || [plain(s), "idle"]; return pill(m[0], m[1], m[2]); };

  // ---------------------------------------------------------------- tooltip
  const tip = document.createElement("div");
  tip.className = "tip hidden"; tip.setAttribute("role", "tooltip");
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(tip));
  function showTip(html, x, y) {
    tip.innerHTML = html; tip.classList.remove("hidden");
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - r.width - 8, x + 14) + "px";
    tip.style.top = Math.max(8, y - r.height - 12) + "px";
  }
  const hideTip = () => tip.classList.add("hidden");
  document.addEventListener("mousemove", e => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t) showTip(t.dataset.tip, e.clientX, e.clientY);
    else if (!e.target.closest || !e.target.closest(".chart-line")) hideTip();
  });
  document.addEventListener("focusin", e => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t) { const r = t.getBoundingClientRect(); showTip(t.dataset.tip, r.left, r.top); }
  });
  document.addEventListener("focusout", hideTip);

  // ---------------------------------------------------------------- charts
  const KIND_COLOR = {setup: "var(--setup)", ai: "var(--ai)", idle: "var(--idle)", moving: "var(--moving)", merchant: "var(--merchant)", human: "var(--human)", live: "var(--live)", stop: "var(--stop)"};
  const DARK_TEXT = new Set(["var(--human)", "var(--merchant)", "var(--ord-1)"]);

  /** Tall segmented bar: one whole split into parts, share printed inside, legend below. */
  function seg(parts, opts = {}) {
    const total = parts.reduce((a, p) => a + (p.value || 0), 0);
    const fmt = opts.fmt || (v => v);
    if (!total) return `<div class="emptychart" style="height:${opts.thin ? 40 : 70}px">${esc(opts.empty || "Nothing yet")}</div>`;
    const blocks = parts.filter(p => p.value > 0).map(p => {
      const share = p.value / total;
      const label = opts.thin || share < 0.08 ? "" : (opts.showValue ? fmt(p.value) : pct(share));
      return `<div style="flex:${p.value};background:${p.color}" class="${DARK_TEXT.has(p.color) ? "dark" : ""}" tabindex="0"
        data-tip="<b>${esc(p.label)}</b><br>${esc(fmt(p.value))} · ${pct(share)}">${label}</div>`;
    }).join("");
    const legend = opts.legend === false ? "" : `<div class="legend">${parts.map(p =>
      `<span><i style="background:${p.color}"></i>${p.icon ? icon(p.icon, "s") : ""}${esc(p.label)} <b class="num">${esc(fmt(p.value))}</b></span>`).join("")}</div>`;
    return `<div class="seg ${opts.thin ? "thin" : ""}" role="img" aria-label="${esc(parts.map(p => `${p.label} ${p.value}`).join(", "))}">${blocks}</div>${legend}`;
  }

  /** Horizontal bars, one value each. colors: single color or per-row array (ordinal). */
  function hbars(rows, opts = {}) {
    if (!rows.length) return `<div class="emptychart">${esc(opts.empty || "No data yet")}</div>`;
    const max = Math.max(...rows.map(r => r.value || 0), 1);
    const fmt = opts.fmt || (v => v);
    return `<div class="hbars" role="list">${rows.map((r, i) => {
      const color = Array.isArray(opts.colors) ? opts.colors[Math.min(i, opts.colors.length - 1)] : (r.color || opts.color || "var(--series-1)");
      return `<div class="hbar" role="listitem" data-tip="<b>${esc(r.label)}</b><br>${esc(fmt(r.value))}${r.note ? "<br>" + esc(r.note) : ""}">
        <span class="lbl" title="${esc(r.label)}">${r.icon ? icon(r.icon, "s") + " " : ""}${esc(r.label)}</span>
        <span class="trk"><span class="fill" style="display:block;width:${Math.max(0.5, (r.value || 0) / max * 100)}%;background:${color}"></span></span>
        <span class="val num">${esc(fmt(r.value))}${r.sub ? `<span class="muted small" style="font-weight:400"> ${esc(r.sub)}</span>` : ""}</span></div>`;
    }).join("")}</div>`;
  }

  /** Line chart with a crosshair tooltip. series: [{name, color, points:[{x, y}]}]; x is a unix time. */
  function line(el, series, opts = {}) {
    const W = Math.max(280, el.clientWidth || 600), H = opts.height || 190, L = 34, R = 14, T = 12, B = 26;
    const xs = series[0] ? series[0].points.map(p => p.x) : [];
    if (!xs.length || series.every(s => s.points.every(p => !p.y))) {
      el.innerHTML = `<div class="emptychart" style="height:${H}px">${esc(opts.empty || "Nothing to show yet")}</div>`; return;
    }
    const ymax = Math.max(1, ...series.flatMap(s => s.points.map(p => p.y)));
    const nice = ymax <= 4 ? ymax : Math.ceil(ymax / 4) * 4;
    const X = i => L + (W - L - R) * (xs.length === 1 ? 0.5 : i / (xs.length - 1));
    const Y = v => T + (H - T - B) * (1 - v / nice);
    const ticks = [0, nice / 2, nice].map(v => Math.round(v));
    const xl = [0, Math.floor((xs.length - 1) / 2), xs.length - 1].filter((v, i, a) => a.indexOf(v) === i);
    const paths = series.map(s => {
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
      const area = series.length === 1 ? `<path d="${d}L${X(xs.length - 1)},${Y(0)}L${X(0)},${Y(0)}Z" fill="${s.color}" opacity=".08"/>` : "";
      const last = s.points[s.points.length - 1];
      return `${area}<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${X(xs.length - 1)}" cy="${Y(last.y)}" r="4" fill="${s.color}" stroke="var(--card)" stroke-width="2"/>`;
    }).join("");
    el.innerHTML = `<div class="chart chart-line"><svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(opts.label || "")}">
      <g class="grid">${ticks.map(t => `<line x1="${L}" x2="${W - R}" y1="${Y(t)}" y2="${Y(t)}"/>`).join("")}</g>
      <g class="axis">${ticks.map(t => `<text x="${L - 8}" y="${Y(t) + 4}" text-anchor="end">${t}</text>`).join("")}
        ${xl.map(i => `<text x="${X(i)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === xs.length - 1 ? "end" : "middle"}">${esc((opts.xfmt || clock)(xs[i]))}</text>`).join("")}</g>
      ${paths}<line class="xh" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--ink-3)" stroke-width="1" opacity="0"/>
      <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" class="hit"/></svg>
      ${series.length > 1 ? `<div class="legend">${series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : ""}</div>`;
    const svg = el.querySelector("svg"), xh = el.querySelector(".xh"), hit = el.querySelector(".hit");
    hit.addEventListener("mousemove", e => {
      const r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * (W / r.width);
      const i = Math.max(0, Math.min(xs.length - 1, Math.round((px - L) / ((W - L - R) / Math.max(1, xs.length - 1)))));
      xh.setAttribute("x1", X(i)); xh.setAttribute("x2", X(i)); xh.setAttribute("opacity", ".5");
      showTip(`<b>${esc((opts.tipfmt || opts.xfmt || clock)(xs[i]))}</b>` + series.map(s => `<br><span style="color:${s.color}">●</span> ${esc(s.name)}: ${s.points[i].y}`).join(""), e.clientX, e.clientY);
    });
    hit.addEventListener("mouseleave", () => { xh.setAttribute("opacity", "0"); hideTip(); });
  }

  /** Bucket timestamps into a series; picks a bucket size so there are at most ~24 points. */
  function buckets(times, now, minSpan = 600) {
    const start = Math.min(now - minSpan, ...(times.length ? times : [now]));
    const span = now - start;
    const size = [10, 30, 60, 300, 900, 3600, 21600, 86400].find(s => span / s <= 24) || 86400;
    const first = Math.floor(start / size) * size, n = Math.floor((now - first) / size) + 1;
    return {size, xs: Array.from({length: n}, (_, i) => first + i * size),
            count: ts => { const c = new Array(n).fill(0); ts.forEach(t => { const i = Math.floor((t - first) / size); if (i >= 0 && i < n) c[i]++; }); return c; }};
  }

  // ---------------------------------------------------------------- toast & dialog
  function toast(msg, kind = "ok") {
    let box = document.querySelector(".toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; box.setAttribute("aria-live", "polite"); document.body.appendChild(box); }
    const t = document.createElement("div");
    t.className = "toast " + (kind === "bad" ? "bad" : "");
    t.innerHTML = icon(kind === "bad" ? "alert" : "check") + `<span>${esc(msg)}</span>`;
    box.appendChild(t); setTimeout(() => t.remove(), 4200);
  }
  function confirmBox({title, body, ok = "Confirm", danger = false}) {
    return new Promise(res => {
      const d = document.createElement("dialog"); d.className = "dlg";
      d.innerHTML = `<form method="dialog" class="in"><h3>${esc(title)}</h3><p>${esc(body)}</p>
        <div class="row"><button class="btn ghost" value="no">Cancel</button><button class="btn ${danger ? "bad" : ""}" value="yes">${esc(ok)}</button></div></form>`;
      document.body.appendChild(d);
      d.addEventListener("close", () => { res(d.returnValue === "yes"); d.remove(); });
      d.showModal();
    });
  }

  window.UI = {icon, esc, dur, ago, clock, pct, plain, avatar, STATE, issue, CHECK, pill, statePill, checkPill,
               seg, hbars, line, buckets, toast, confirm: confirmBox, KIND_COLOR, showTip, hideTip};
})();
