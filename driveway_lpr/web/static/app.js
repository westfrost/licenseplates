/* Indkørslen - dashboard. Vanilla JS + Chart.js, no build step. */
(() => {
  "use strict";

  const DIRS = [
    { key: "in", label: "Ind", css: "--dir-in" },
    { key: "out", label: "Ud", css: "--dir-out" },
    { key: "through", label: "Forbi", css: "--dir-through" },
    { key: "unknown", label: "Ukendt", css: "--dir-unknown" },
  ];
  const DIR_LABEL = Object.fromEntries(DIRS.map((d) => [d.key, d.label]));
  const CATEGORY = { beboer: "Beboer", familie: "Familie", gaest: "Gæst", haandvaerker: "Håndværker",
    levering: "Levering", kendt: "Kendt", blokeret: "Uønsket" };
  const WEEKDAYS = ["Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn"];
  const PAGE = 50;

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const nf = new Intl.NumberFormat("da-DK");

  let TZ = "Europe/Copenhagen";
  const fmt = (ts, opts) => new Intl.DateTimeFormat("da-DK", { timeZone: TZ, ...opts }).format(new Date(ts * 1000));
  const fmtTime = (ts) => fmt(ts, { hour: "2-digit", minute: "2-digit" });
  const fmtDateTime = (ts) => fmt(ts, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  function fmtWhen(ts) {
    const today = fmt(Date.now() / 1000, { year: "numeric", month: "2-digit", day: "2-digit" });
    const day = fmt(ts, { year: "numeric", month: "2-digit", day: "2-digit" });
    return day === today ? `i dag ${fmtTime(ts)}` : fmtDateTime(ts);
  }
  function fmtDuration(s) {
    if (s == null) return "–";
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 86400) { const h = Math.floor(s / 3600); const m = Math.round((s % 3600) / 60); return m ? `${h} t ${m} m` : `${h} t`; }
    return `${(s / 86400).toFixed(1).replace(".", ",")} døgn`;
  }
  function ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "lige nu";
    return `${fmtDuration(s)} siden`;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    return res.json();
  }

  const isDanish = (p) => /^[A-Z]{2}\d{5}$/.test(p || "");
  const plateHtml = (ev) => `<span class="plate${ev.valid_format === false ? " invalid" : ""}${isDanish(ev.plate) ? "" : " foreign"}" data-plate="${esc(ev.plate)}" title="Vis historik"><span>${esc(ev.plate_display || ev.plate)}</span></span>`;
  const dirHtml = (d, parked) => `<span class="dir ${esc(d)}">${esc(DIR_LABEL[d] || d)}</span>${parked ? '<span class="badge">parkeret</span>' : ""}`;
  const vehDesc = (ev) => (ev.register && ev.register.description) || "";
  const whoHtml = (ev) => ev.vehicle_label ? esc(ev.vehicle_label)
    : vehDesc(ev) ? `<span class="veh">${esc(vehDesc(ev))}</span>`
    : (ev.known || ev.vehicle_category ? "" : '<span class="muted">Ukendt</span>');
  const regBadge = (ev) => ev.register_status === "not_found"
    ? '<span class="badge warn" title="Pladen findes ikke i Motorregistret – måske en fejllæsning eller en udenlandsk plade">⚠ ikke i registret</span>'
    : ev.register_status === "corrected"
      ? `<span class="badge" title="Kameraet læste ${esc(ev.original_plate)}, som ikke findes. Rettet via Motorregistret.">✓ rettet fra ${esc(ev.original_plate)}</span>`
      : "";
  let LOOKUP = { provider: "none", link: "" };
  const extLink = (plate) => (LOOKUP.link ? LOOKUP.link.replaceAll("{plate}", encodeURIComponent(plate)) : "https://www.nummerplade.net/");
  function extButton(plate) {
    return `<a class="btn ghost" href="${esc(extLink(plate))}" target="_blank" rel="noopener noreferrer" data-copy="${esc(plate)}"
      title="${LOOKUP.link ? "Åbn opslag" : "Kopierer pladen, så du kan indsætte den i søgefeltet"}">nummerplade.net ↗</a>`;
  }
  const REG_FIELDS = [["Mærke / model", (r) => [r.make, r.model, r.variant].filter(Boolean).join(" ")], ["Farve", (r) => r.color],
    ["Modelår", (r) => r.model_year], ["Drivmiddel", (r) => r.fuel], ["Art", (r) => r.kind], ["Anvendelse", (r) => r.use],
    ["Status", (r) => r.status], ["1. registrering", (r) => r.first_registration]];
  const regRows = (r) => REG_FIELDS.map(([k, f]) => [k, f(r)]).filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  const snapUrl = (rel) => (rel ? `/snapshots/${rel.split("/").map(encodeURIComponent).join("/")}` : "");

  // ------------------------------------------------------------- tooltip
  const tip = document.createElement("div");
  tip.className = "tooltip"; tip.hidden = true; document.body.appendChild(tip);
  function showTip(e, html) { tip.innerHTML = html; tip.hidden = false; tip.style.left = `${e.clientX}px`; tip.style.top = `${e.clientY - 6}px`; }
  function hideTip() { tip.hidden = true; }

  // ---------------------------------------------------------------- tabs
  let current = "overview";
  function showTab(name) {
    current = name;
    $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    $$(".tab-panel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
    history.replaceState(null, "", `#${name}`);
    if (name === "live") startLive(); else stopLive();
    refresh();
  }
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.goto)));

  // ---------------------------------------------------------------- theme
  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (_) { /* ignore */ }
    redrawCharts();
  });
  try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (_) { /* ignore */ }
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawCharts);

  // --------------------------------------------------------------- charts
  const charts = {};
  const chartData = {};
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.animation = { duration: 250 };

  function legendHtml(keys) {
    return DIRS.filter((d) => keys.includes(d.key)).map((d) => `<span><i style="background:var(${d.css})"></i>${d.label}</span>`).join("");
  }

  function barChart(id, labels, rows, keys, opts = {}) {
    const surface = css("--surface");
    const datasets = DIRS.filter((d) => keys.includes(d.key)).map((d, i, arr) => ({
      label: d.label,
      data: rows.map((r) => r[d.key]),
      backgroundColor: css(d.css),
      hoverBackgroundColor: css(d.css),
      borderColor: surface,
      borderWidth: { top: 2, bottom: 0, left: 0, right: 0 },
      borderSkipped: false,
      // rounded data-end only on the top segment of each stack
      borderRadius: i === arr.length - 1 ? { topLeft: 4, topRight: 4 } : 0,
      maxBarThickness: 24,
      categoryPercentage: 0.8,
      barPercentage: 0.9,
    }));
    const config = {
      type: "bar",
      data: { labels, datasets },
      options: {
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { stacked: true, grid: { display: false }, border: { color: css("--axis") },
               ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 12 } },
          y: { stacked: true, beginAtZero: true, grid: { color: css("--grid") }, border: { display: false },
               ticks: { color: css("--muted"), precision: 0, maxTicksLimit: 5 } },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: css("--ink"), titleColor: css("--page"), bodyColor: css("--page"),
            padding: 10, boxPadding: 4, usePointStyle: true,
            filter: (item) => item.raw > 0,
            callbacks: { title: (items) => (opts.titleFn ? opts.titleFn(items[0].dataIndex) : items[0].label) },
          },
        },
      },
    };
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(document.getElementById(id), config);
  }

  function drawDaily() {
    const rows = chartData.daily; if (!rows) return;
    const labels = rows.map((r) => { const [y, m, d] = r.date.split("-"); return `${+d}/${+m}`; });
    barChart("daily-chart", labels, rows, ["in", "out", "through", "unknown"], {
      titleFn: (i) => new Intl.DateTimeFormat("da-DK", { weekday: "long", day: "numeric", month: "long" }).format(new Date(rows[i].date + "T12:00:00")),
    });
    $("#daily-legend").innerHTML = legendHtml(["in", "out", "through", "unknown"]);
  }
  function drawHourly() {
    const rows = chartData.hourly; if (!rows) return;
    barChart("hourly-chart", rows.map((r) => String(r.hour).padStart(2, "0")), rows, ["in", "out", "through", "unknown"], {
      titleFn: (i) => `kl. ${String(i).padStart(2, "0")}:00–${String(i).padStart(2, "0")}:59`,
    });
    $("#hourly-legend").innerHTML = legendHtml(["in", "out", "through", "unknown"]);
  }
  function drawHeatmap() {
    const grid = chartData.heatmap; if (!grid) return;
    const max = Math.max(1, ...grid.flat());
    const step = (v) => (v === 0 ? 0 : Math.min(7, 1 + Math.floor((v / max) * 6.999)));
    let html = '<div></div>' + Array.from({ length: 24 }, (_, h) => `<div class="cl">${h}</div>`).join("");
    grid.forEach((row, d) => {
      html += `<div class="rl">${WEEKDAYS[d]}</div>`;
      row.forEach((v, h) => { html += `<div class="cell" data-d="${d}" data-h="${h}" data-v="${v}" style="background:var(--seq-${step(v)})"></div>`; });
    });
    const el = $("#heatmap");
    el.innerHTML = html;
    let scale = el.nextElementSibling;
    if (!scale || !scale.classList.contains("heat-scale")) {
      scale = document.createElement("div"); scale.className = "heat-scale"; el.after(scale);
    }
    scale.innerHTML = `0 ${[1, 2, 3, 4, 5, 6, 7].map((i) => `<i style="background:var(--seq-${i})"></i>`).join("")} ${nf.format(max)}`;
  }
  $("#heatmap").addEventListener("mousemove", (e) => {
    const c = e.target.closest(".cell"); if (!c) return hideTip();
    const h = +c.dataset.h;
    showTip(e, `${WEEKDAYS[+c.dataset.d]} kl. ${String(h).padStart(2, "0")}–${String(h + 1).padStart(2, "0")}: <b>${nf.format(+c.dataset.v)}</b>`);
  });
  $("#heatmap").addEventListener("mouseleave", hideTip);

  function redrawCharts() { requestAnimationFrame(() => { drawDaily(); drawHourly(); drawHeatmap(); if (chartData.profile) drawProfile(chartData.profile); }); }

  // ------------------------------------------------------------- overview
  let dailyDays = 30;
  $$("#daily-range button").forEach((b) => b.addEventListener("click", async () => {
    $$("#daily-range button").forEach((x) => x.classList.toggle("on", x === b));
    dailyDays = +b.dataset.days;
    chartData.daily = await api(`/api/stats/daily?days=${dailyDays}`);
    drawDaily();
  }));

  function tile(label, value, delta, hero) {
    return `<div class="tile${hero ? " hero" : ""}"><div class="label">${label}</div><div class="value">${value}</div>${delta ? `<div class="delta">${delta}</div>` : ""}</div>`;
  }
  function deltaHtml(now, before, period) {
    const diff = now - before;
    if (diff === 0) return `samme som ${period}`;
    return `<span class="${diff > 0 ? "up" : "down"}">${diff > 0 ? "▲" : "▼"} ${nf.format(Math.abs(diff))}</span> vs. ${period}`;
  }

  async function loadOverview() {
    const [s, daily, hourly, heat, top, makes] = await Promise.all([
      api("/api/summary"), api(`/api/stats/daily?days=${dailyDays}`), api("/api/stats/hourly?days=30"),
      api("/api/stats/heatmap?days=90"), api("/api/stats/top?days=30&limit=10"), api("/api/stats/makes?days=90"),
    ]);
    const maxMake = Math.max(1, ...makes.map((m) => m.cars));
    $("#makes").innerHTML = makes.length ? makes.map((m) => `
      <div class="hbar" title="${esc(m.make)}: ${nf.format(m.cars)} forskellige biler">
        <span class="hbar-label">${esc(m.make)}</span>
        <span class="hbar-track"><i style="width:${(m.cars / maxMake) * 100}%"></i></span>
        <span class="hbar-val">${nf.format(m.cars)}</span></div>`).join("")
      : `<div class="empty">${LOOKUP.provider === "none" ? "Slå opslag i Motorregistret til (<code>lookup.provider</code>) for at se bilmærker" : "Ingen data endnu"}</div>`;
    const t = s.today;
    $("#tiles").innerHTML =
      tile("Hændelser i dag", nf.format(t.events), deltaHtml(t.events, s.yesterday.events, "i går"), true) +
      tile("Forskellige biler i dag", nf.format(t.unique), `${nf.format(t.unknown)} ukendte`) +
      tile("Ind / ud i dag", `${nf.format(t.arrivals)} / ${nf.format(t.departures)}`, t.first_arrival ? `første ankomst ${fmtTime(t.first_arrival)}` : "") +
      tile("Nye plader i dag", nf.format(t.new_plates), "aldrig set før") +
      tile("Sidste 7 dage", nf.format(s.week.events), deltaHtml(s.week.events, s.prev_week.events, "ugen før"));

    $("#present").innerHTML = s.present.length ? s.present.map((ev) => `
      <div class="car" data-event="${ev.id}">
        ${ev.crop ? `<img src="${snapUrl(ev.crop)}" alt="" loading="lazy">` : ""}
        ${plateHtml(ev)}
        <div class="who">${whoHtml(ev) || "&nbsp;"}</div>
        <div class="since">siden ${fmtWhen(ev.first_seen)}</div>
      </div>`).join("") : '<div class="empty">Ingen parkerede biler lige nu</div>';

    const latest = await api("/api/events?limit=8");
    $("#latest").innerHTML = latest.events.length ? latest.events.map(eventLi).join("") : '<li class="empty">Ingen hændelser endnu</li>';

    $("#top-table tbody").innerHTML = top.length ? top.map((r) => `
      <tr data-plate="${esc(r.plate)}"><td>${plateHtml(r)}</td><td>${r.label ? esc(r.label) : r.description ? `<span class="veh">${esc(r.description)}</span>` : '<span class="muted">–</span>'}</td>
      <td class="num">${nf.format(r.visits)}</td><td class="num">${nf.format(r.arrivals)}</td>
      <td class="num">${fmtDuration(r.avg_dwell_s)}</td><td class="when">${fmtWhen(r.last_seen)}</td></tr>`).join("")
      : '<tr><td colspan="6" class="empty">Ingen data endnu</td></tr>';

    chartData.daily = daily; chartData.hourly = hourly; chartData.heatmap = heat;
    if (!charts["daily-chart"]) redrawCharts();
    else { updateBars("daily-chart", daily); updateBars("hourly-chart", hourly); drawHeatmap(); }
  }
  function updateBars(id, rows) {
    const ch = charts[id]; if (!ch) return;
    if (ch.data.labels.length !== rows.length) return id === "daily-chart" ? drawDaily() : drawHourly();
    ch.data.datasets.forEach((ds) => { const d = DIRS.find((x) => x.label === ds.label); ds.data = rows.map((r) => r[d.key]); });
    ch.update("none");
  }

  function eventLi(ev) {
    return `<li data-event="${ev.id}">
      ${ev.crop ? `<img class="thumb" src="${snapUrl(ev.crop)}" alt="" loading="lazy">` : '<span class="thumb"></span>'}
      ${plateHtml(ev)}
      <span class="who">${dirHtml(ev.direction, ev.parked)} ${ev.vehicle_label ? "· " + esc(ev.vehicle_label) : vehDesc(ev) ? `· <span class="veh">${esc(vehDesc(ev))}</span>` : ""}${regBadge(ev)}</span>
      <span class="when">${fmtWhen(ev.first_seen)}</span></li>`;
  }

  // --------------------------------------------------------------- events
  let evOffset = 0;
  function filterQuery() {
    const f = new FormData($("#event-filters"));
    const q = new URLSearchParams();
    for (const [k, v] of f.entries()) if (v) q.set(k, v);
    return q;
  }
  async function loadEvents() {
    const q = filterQuery();
    $("#export-link").href = `/api/export.csv${q.get("days") ? `?days=${q.get("days")}` : ""}`;
    q.set("limit", PAGE); q.set("offset", evOffset);
    const data = await api(`/api/events?${q}`);
    $("#events-table tbody").innerHTML = data.events.length ? data.events.map((ev) => `
      <tr data-event="${ev.id}">
        <td>${ev.crop ? `<img class="thumb" src="${snapUrl(ev.crop)}" alt="" loading="lazy">` : ""}</td>
        <td class="when">${fmtWhen(ev.first_seen)}</td><td>${plateHtml(ev)}</td><td>${dirHtml(ev.direction, ev.parked)}</td>
        <td>${whoHtml(ev)}${regBadge(ev)}</td><td class="num">${fmtDuration(ev.dwell_s)}</td><td class="num">${Math.round(ev.confidence * 100)} %</td>
      </tr>`).join("") : '<tr><td colspan="7" class="empty">Ingen hændelser matcher</td></tr>';
    const end = Math.min(evOffset + PAGE, data.total);
    $("#page-info").textContent = data.total ? `${nf.format(evOffset + 1)}–${nf.format(end)} af ${nf.format(data.total)}` : "";
    $("#prev").disabled = evOffset === 0;
    $("#next").disabled = end >= data.total;
  }
  let filterTimer;
  $("#event-filters").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(() => { evOffset = 0; loadEvents(); }, 250); });
  $("#event-filters").addEventListener("submit", (e) => e.preventDefault());
  $("#prev").addEventListener("click", () => { evOffset = Math.max(0, evOffset - PAGE); loadEvents(); });
  $("#next").addEventListener("click", () => { evOffset += PAGE; loadEvents(); });

  // ------------------------------------------------------------- vehicles
  async function loadVehicles() {
    const rows = await api("/api/vehicles");
    $("#vehicles-table tbody").innerHTML = rows.length ? rows.map((v) => `
      <tr data-plate="${esc(v.plate)}"><td>${plateHtml(v)}</td><td>${esc(v.label) || '<span class="muted">–</span>'}</td>
      <td>${esc(CATEGORY[v.category] || v.category)}</td><td class="num">${nf.format(v.visits)}</td>
      <td class="when">${v.last_visit ? fmtWhen(v.last_visit) : "–"}</td>
      <td><button class="link" data-edit="${esc(v.plate)}">Redigér</button></td></tr>`).join("")
      : '<tr><td colspan="6" class="empty">Ingen køretøjer endnu – tilføj dine egne biler til højre</td></tr>';
    $$("[data-edit]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); editVehicle(rows.find((v) => v.plate === b.dataset.edit)); }));
  }
  function editVehicle(v) {
    const f = $("#vehicle-form");
    f.plate.value = v ? v.plate_display || v.plate : "";
    f.label.value = v?.label || ""; f.owner.value = v?.owner || ""; f.category.value = v?.category || "kendt";
    f.notes.value = v?.notes || ""; f.notify.checked = !!v?.notify;
    $("#vehicle-form-title").textContent = v ? "Redigér køretøj" : "Tilføj køretøj";
    f.label.focus();
  }
  $("#vehicle-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    await api(`/api/vehicles/${encodeURIComponent(f.plate.value)}`, { method: "PUT", body: JSON.stringify({
      label: f.label.value, owner: f.owner.value, category: f.category.value, notes: f.notes.value, notify: f.notify.checked }) });
    f.reset(); $("#vehicle-form-title").textContent = "Tilføj køretøj";
    loadVehicles();
  });

  // ---------------------------------------------------------------- modal
  const modal = $("#modal");
  async function openEvent(id) {
    const ev = await api(`/api/events/${id}`);
    $("#modal-body").innerHTML = `
      <div class="modal-title">${plateHtml(ev)} ${dirHtml(ev.direction, ev.parked)} <span class="muted">${fmtWhen(ev.first_seen)}</span></div>
      <div class="modal-grid">
        <div>${ev.snapshot ? `<img class="snap" src="${snapUrl(ev.snapshot)}" alt="Snapshot">` : '<div class="empty">Intet billede</div>'}
             ${ev.crop ? `<img class="crop" src="${snapUrl(ev.crop)}" alt="Nummerplade">` : ""}</div>
        <div>
          <dl class="kv">
            <dt>Navn</dt><dd>${ev.vehicle_label ? esc(ev.vehicle_label) : '<span class="muted">Ukendt</span>'}</dd>
            ${ev.register ? regRows(ev.register) : ""}
            ${ev.register_status === "not_found" ? `<dt>Register</dt><dd>${regBadge(ev)}</dd>` : ""}
            ${ev.original_plate ? `<dt>Aflæst som</dt><dd>${esc(ev.original_plate)} (rettet via Motorregistret)</dd>` : ""}
            <dt>Først set</dt><dd>${fmtDateTime(ev.first_seen)}</dd>
            <dt>Sidst set</dt><dd>${fmtDateTime(ev.last_seen)}</dd>
            ${ev.dwell_s != null ? `<dt>Ophold</dt><dd>${fmtDuration(ev.dwell_s)}${ev.departed_at ? "" : " (stadig her?)"}</dd>` : ""}
            <dt>Sikkerhed</dt><dd>${Math.round(ev.confidence * 100)} % (${ev.reads} aflæsninger)</dd>
            ${ev.alternatives.length ? `<dt>Alternativer</dt><dd>${ev.alternatives.map(([t, n]) => `${esc(t)} (${n})`).join(", ")}</dd>` : ""}
            ${ev.corrected ? "<dt>Rettet</dt><dd>Ja, manuelt</dd>" : ""}
          </dl>
          <h3>Ret aflæsning</h3>
          <form id="fix-form" class="stack">
            <label>Nummerplade <input name="plate" value="${esc(ev.plate)}" autocomplete="off"></label>
            <label>Retning <select name="direction">${DIRS.map((d) => `<option value="${d.key}"${d.key === ev.direction ? " selected" : ""}>${d.label}</option>`).join("")}</select></label>
            <label class="check"><input type="checkbox" name="all"> Ret pladen på alle hændelser</label>
            <div class="row"><button class="btn" type="submit">Gem</button>
              <button class="btn ghost" type="button" id="as-vehicle">${ev.vehicle_category ? "Redigér køretøj" : "Tilføj som kendt"}</button>
              <button class="btn danger" type="button" id="del-event">Slet</button></div>
            <div class="row">${extButton(ev.plate)}</div>
          </form>
        </div>
      </div>`;
    $("#fix-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      await api(`/api/events/${id}`, { method: "PATCH", body: JSON.stringify({ plate: f.plate.value, direction: f.direction.value, apply_to_all: f.all.checked }) });
      modal.close(); refresh();
    });
    $("#del-event").addEventListener("click", async () => {
      if (!confirm("Slet hændelsen og dens billeder?")) return;
      await api(`/api/events/${id}`, { method: "DELETE" }); modal.close(); refresh();
    });
    $("#as-vehicle").addEventListener("click", async () => {
      modal.close(); showTab("vehicles");
      const v = (await api("/api/vehicles")).find((x) => x.plate === ev.plate);
      editVehicle(v || { plate: ev.plate, plate_display: ev.plate_display });
    });
    if (!modal.open) modal.showModal();
  }

  async function openPlate(plate) {
    const p = await api(`/api/plates/${encodeURIComponent(plate)}`);
    chartData.profile = p;
    const v = p.vehicle;
    $("#modal-body").innerHTML = `
      <div class="modal-title">${plateHtml(p)} <strong>${v && v.label ? esc(v.label) : p.register && p.register.description ? esc(p.register.description) : "Ukendt køretøj"}</strong>
        ${v ? `<span class="badge">${esc(CATEGORY[v.category] || v.category)}</span>` : ""}</div>
      <dl class="kv">
        <dt>Besøg i alt</dt><dd>${nf.format(p.visits)}</dd>
        <dt>Først set</dt><dd>${p.first_seen ? fmtDateTime(p.first_seen) : "–"}</dd>
        <dt>Sidst set</dt><dd>${p.last_seen ? `${fmtDateTime(p.last_seen)} (${ago(p.last_seen)})` : "–"}</dd>
        <dt>Typisk ankomst</dt><dd>${p.avg_arrival ? `kl. ${p.avg_arrival}` : "–"}</dd>
        <dt>Gns. ophold</dt><dd>${fmtDuration(p.avg_dwell_s)}</dd>
      </dl>
      <h3>Motorregistret</h3>
      <dl class="kv" id="reg-info">${regInfoHtml(p.register)}</dl>
      <div class="row" style="margin-top:10px">
        ${LOOKUP.provider !== "none" ? '<button class="btn ghost" id="reg-refresh">Slå op nu</button>' : ""}
        ${extButton(p.plate)}<span class="muted" id="reg-msg"></span></div>
      <div class="mini-charts">
        <div><h3>Pr. time</h3><div class="chart-box"><canvas id="p-hours"></canvas></div></div>
        <div><h3>Pr. ugedag</h3><div class="chart-box"><canvas id="p-days"></canvas></div></div>
      </div>
      <h3>Historik</h3>
      <ul class="event-list">${p.events.map(eventLi).join("")}</ul>`;
    if (!modal.open) modal.showModal();
    drawProfile(p);
    $("#reg-refresh")?.addEventListener("click", async (e) => {
      e.target.disabled = true; $("#reg-msg").textContent = "Slår op …";
      try {
        const info = await api(`/api/plates/${encodeURIComponent(p.plate)}/lookup`, { method: "POST" });
        $("#reg-info").innerHTML = regInfoHtml(info.found ? { ...info, fetched_at: Date.now() / 1000 } : { found: false, fetched_at: Date.now() / 1000 });
        $("#reg-msg").textContent = "";
      } catch (err) { $("#reg-msg").textContent = err.message; }
      e.target.disabled = false;
    });
  }
  function regInfoHtml(r) {
    if (!r) return `<dt>Status</dt><dd class="muted">${LOOKUP.provider === "none" ? "Opslag er ikke slået til" : "Ikke slået op endnu"}</dd>`;
    if (!r.found) return `<dt>Status</dt><dd>⚠ Findes ikke i registret (tjekket ${fmtDateTime(r.fetched_at)})</dd>`;
    return regRows(r) + `<dt>Tjekket</dt><dd class="muted">${fmtDateTime(r.fetched_at)}</dd>`;
  }
  function drawProfile(p) {
    if (!document.getElementById("p-hours")) return;
    const single = (id, labels, values, label) => {
      if (charts[id]) charts[id].destroy();
      charts[id] = new Chart(document.getElementById(id), {
        type: "bar",
        data: { labels, datasets: [{ label, data: values, backgroundColor: css("--dir-in"), borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: false, maxBarThickness: 24 }] },
        options: { maintainAspectRatio: false, plugins: { legend: { display: false },
          tooltip: { backgroundColor: css("--ink"), titleColor: css("--page"), bodyColor: css("--page") } },
          scales: { x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 8 } },
                    y: { beginAtZero: true, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), precision: 0, maxTicksLimit: 4 } } } },
      });
    };
    single("p-hours", p.hours.map((_, h) => String(h).padStart(2, "0")), p.hours, "Besøg");
    single("p-days", WEEKDAYS, p.weekdays, "Besøg");
  }
  modal.addEventListener("close", () => { chartData.profile = null; });

  document.addEventListener("click", (e) => {
    const copy = e.target.closest("[data-copy]");
    if (copy) { try { navigator.clipboard?.writeText(copy.dataset.copy); } catch (_) { /* ignore */ } return; }
    const plate = e.target.closest(".plate[data-plate]");
    if (plate) { e.stopPropagation(); return openPlate(plate.dataset.plate); }
    const ev = e.target.closest("[data-event]");
    if (ev) return openEvent(ev.dataset.event);
    const row = e.target.closest("tr[data-plate]");
    if (row && !e.target.closest("button")) return openPlate(row.dataset.plate);
  });

  // ----------------------------------------------------------------- live
  let liveOn = false;
  function startLive() {
    if (liveOn) return;
    liveOn = true;
    const img = $("#live-img");
    img.onerror = () => { $("#live-msg").textContent = "Intet livebillede – kører kameradelen?"; };
    img.onload = () => { $("#live-msg").textContent = ""; };
    img.src = `/api/live.mjpg?t=${Date.now()}`;
  }
  function stopLive() { if (!liveOn) return; liveOn = false; $("#live-img").removeAttribute("src"); }

  async function loadStatus() {
    const s = await api("/api/status");
    TZ = s.timezone || TZ;
    LOOKUP = { provider: s.lookup_provider || "none", link: s.lookup_link || "" };
    const pill = $("#cam-status");
    if (!s.pipeline) { pill.className = "pill"; pill.lastElementChild.textContent = "Kun dashboard"; }
    else if (s.camera_connected) { pill.className = "pill ok"; pill.lastElementChild.textContent = `Kamera online · ${s.camera_fps} fps`; }
    else { pill.className = "pill bad"; pill.lastElementChild.textContent = "Kamera offline"; }
    if (current !== "live") return;
    const rows = s.pipeline ? [
      ["Kilde", esc(s.camera_source)],
      ["Opløsning", s.resolution ? `${s.resolution[0]} × ${s.resolution[1]}` : "–"],
      ["Kamera-fps", s.camera_fps],
      ["Analyse", `${s.proc_fps} billeder/s · ${s.infer_ms} ms pr. billede`],
      ["Bevægelse", s.motion ? `ja (${(s.motion_level * 100).toFixed(2)} %)` : `nej (${(s.motion_level * 100).toFixed(2)} %)`],
      ["Sidste plade", s.last_detection ? ago(s.last_detection) : "–"],
      ["Analyseret i alt", nf.format(s.processed)],
      ["Kørt i", fmtDuration(s.uptime_s)],
      ...(s.camera_error ? [["Fejl", esc(s.camera_error)]] : []),
      ...(s.error ? [["Genkendelsesfejl", esc(s.error)]] : []),
      ["Motorregister", s.lookup_provider === "none" ? "fra" : esc(s.lookup_provider) + (s.lookup_error ? ` – fejl: ${esc(s.lookup_error)}` : "")],
    ] : [["Status", "Kameradelen kører ikke i denne proces"]];
    $("#status-list").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    $("#tracks").innerHTML = (s.tracks || []).length ? s.tracks.map((t) => `
      <li>${t.text ? plateHtml({ plate: t.text, plate_display: t.text }) : "<span class='muted'>læser…</span>"}
        <span class="who">${t.state === "parked" ? "parkeret" : dirHtml(t.direction)}</span><span class="when">${fmtDuration(t.age)}</span></li>`).join("")
      : '<li class="empty">Ingen biler i billedet</li>';
  }

  // -------------------------------------------------------------- refresh
  let busy = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      await loadStatus();
      if (current === "overview") await loadOverview();
      else if (current === "events") await loadEvents();
      else if (current === "vehicles") await loadVehicles();
    } catch (err) {
      console.error(err);
    } finally { busy = false; }
  }
  setInterval(() => { if (!document.hidden && !modal.open) refresh(); }, 10000);
  setInterval(() => { if (!document.hidden && current === "live") loadStatus().catch(() => {}); }, 2000);

  const initial = location.hash.replace("#", "");
  showTab(["overview", "events", "vehicles", "live"].includes(initial) ? initial : "overview");
})();
