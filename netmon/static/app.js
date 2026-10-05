/* NetMon — frontend del dashboard (sin dependencias externas) */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const state = { data: null, events: [], cards: new Map(), prevIf: new Map(), sound: false, connected: false };
  const SEV_ICON = { critical: "🔴", warning: "🟠", info: "🟢" };
  const STATUS = {
    up: ["✓", "En línea"], down: ["✕", "Caído"], snmp_fail: ["!", "Sin SNMP"], unknown: ["…", "Conectando"],
  };

  // ------------------------------------------------------------ utilidades
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Mensajes del servidor: solo se permiten <b>, <i>, <code>
  const safeMsg = (s) => esc(s).replace(/&lt;(\/?)(b|i|code)&gt;/g, "<$1$2>");
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  function fmtBps(v) {
    if (v == null) return "—";
    const u = ["bps", "Kbps", "Mbps", "Gbps"];
    let i = 0;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
  }
  function fmtAxis(v) {
    const u = ["", "K", "M", "G"];
    let i = 0;
    while (v >= 1000 && i < 3) { v /= 1000; i++; }
    return `${Number.isInteger(v) || v >= 10 ? Math.round(v) : v.toFixed(1)}${u[i]}bps`;
  }
  function fmtDur(s) {
    if (s == null) return "—";
    s = Math.max(0, Math.floor(s));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s % 60}s`;
    return `${s}s`;
  }
  const ago = (ts) => (ts ? `hace ${fmtDur(Date.now() / 1000 - ts)}` : "—");
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("es-CO", { hour12: false });
  const dateTime = (ts) => new Date(ts * 1000).toLocaleString("es-CO", { hour12: false, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });

  function model(d) {
    const s = d.sys_descr || "";
    if (d.vendor === "fortigate") return (s.split(",")[0] || "FortiGate").trim();
    const all = [...s.matchAll(/\(([A-Z][A-Z0-9-]{2,})[\s)]/g)];
    return all.length ? all[all.length - 1][1] : (s.slice(0, 40) || d.vendor);
  }

  async function api(path, method = "POST") {
    const r = await fetch(path, { method });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }

  // ------------------------------------------------------------ gráficas (canvas)
  function drawChart(cv, pts, series, opt) {
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w) return;
    if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const padL = 50, padR = 6, padT = 6, padB = 14;
    const pw = w - padL - padR, ph = h - padT - padB;
    let max = opt.max;
    if (max == null) {
      max = 0;
      for (const p of pts) for (const s of series) if (p[s.key] != null) max = Math.max(max, p[s.key]);
      max = niceMax(max || 1);
    }
    const n = pts.length;
    const t1 = n ? pts[n - 1].t : Date.now() / 1000;
    const span = opt.span;
    const x = (t) => padL + pw * (1 - (t1 - t) / span);
    const y = (v) => padT + ph * (1 - Math.min(v, max) / max);

    // grid + ejes (recesivos)
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillStyle = css("--text-muted");
    ctx.strokeStyle = css("--grid");
    ctx.lineWidth = 1;
    for (const f of [0, 0.5, 1]) {
      const yy = Math.round(padT + ph * (1 - f)) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
      ctx.textAlign = "right"; ctx.textBaseline = "middle";
      ctx.fillText(opt.fmt(max * f), padL - 5, yy);
    }
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.fillText(`-${Math.round(span / 60)} min`, padL, h - 2);
    ctx.textAlign = "right"; ctx.fillText("ahora", w - padR, h - 2);

    // umbral
    if (opt.threshold != null) {
      ctx.save(); ctx.setLineDash([3, 3]); ctx.strokeStyle = css("--text-muted");
      const yy = Math.round(y(opt.threshold)) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke(); ctx.restore();
    }

    // series (2px, uniones redondeadas, huecos en null)
    for (const s of series) {
      ctx.strokeStyle = css(s.color); ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.beginPath();
      let pen = false;
      for (const p of pts) {
        const v = p[s.key];
        if (v == null || t1 - p.t > span) { pen = false; continue; }
        const X = x(p.t), Y = y(v);
        if (!pen) { ctx.moveTo(X, Y); pen = true; } else ctx.lineTo(X, Y);
      }
      ctx.stroke();
    }

    // hover
    const hov = cv._hover;
    if (hov != null && n) {
      let best = null, bd = 1e9;
      for (const p of pts) { const d = Math.abs(x(p.t) - hov); if (d < bd) { bd = d; best = p; } }
      if (best && bd < 30) {
        const X = Math.round(x(best.t)) + 0.5;
        ctx.strokeStyle = css("--text-muted"); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(X, padT); ctx.lineTo(X, padT + ph); ctx.stroke();
        for (const s of series) {
          if (best[s.key] == null) continue;
          ctx.fillStyle = css(s.color); ctx.strokeStyle = css("--surface-1"); ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(X, y(best[s.key]), 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        }
        showTip(cv, best, series, opt);
      }
    }
  }
  function niceMax(v) {
    const e = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * e) return m * e;
    return 10 * e;
  }
  const tip = $("#tip");
  function showTip(cv, p, series, opt) {
    const r = cv.getBoundingClientRect();
    tip.innerHTML = `<div class="t">${clock(p.t)}</div>` + series.map((s) =>
      `<div class="row"><i style="background:${css(s.color)}"></i>${s.label}: <b class="num">${p[s.key] == null ? "—" : opt.fmt(p[s.key], true)}</b></div>`).join("");
    tip.hidden = false;
    const tx = Math.min(r.left + cv._hover + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = `${tx}px`;
    tip.style.top = `${r.top - tip.offsetHeight - 6}px`;
  }
  function bindHover(cv, redraw) {
    cv.addEventListener("mousemove", (e) => { cv._hover = e.clientX - cv.getBoundingClientRect().left; redraw(); });
    cv.addEventListener("mouseleave", () => { cv._hover = null; tip.hidden = true; redraw(); });
  }

  // ------------------------------------------------------------ tarjetas
  function createCard(d) {
    const el = document.createElement("article");
    el.className = "card";
    el.innerHTML = `
      <div class="card-head">
        <div><h3>${esc(d.name)}</h3><div class="meta"></div></div>
        <span class="badge"></span>
      </div>
      <div class="facts"></div>
      <div class="meters"></div>
      <div class="charts">
        <div class="chart-title"><span>CPU y memoria (%)</span>
          <span class="legend"><span><i style="background:var(--series-1)"></i>CPU</span><span><i style="background:var(--series-2)"></i>Memoria</span></span></div>
        <canvas class="chart c-res" role="img" aria-label="CPU y memoria última hora"></canvas>
        <div class="chart-title"><span>Tráfico total</span>
          <span class="legend"><span><i style="background:var(--series-1)"></i>Entrada</span><span><i style="background:var(--series-2)"></i>Salida</span></span></div>
        <canvas class="chart c-net" role="img" aria-label="Tráfico total última hora"></canvas>
      </div>
      <div class="ifaces"></div>
      <div class="card-foot"></div>`;
    const card = { el, name: d.name, data: d };
    card.redraw = () => drawCharts(card);
    bindHover($(".c-res", el), card.redraw);
    bindHover($(".c-net", el), card.redraw);
    el.addEventListener("click", onCardClick);
    return card;
  }

  function drawCharts(card) {
    const d = card.data, span = Math.max(600, (state.data?.poll_interval || 10) * d.history.length);
    const spanCap = Math.min(span, 3600);
    drawChart($(".c-res", card.el), d.history,
      [{ key: "cpu", color: "--series-1", label: "CPU" }, { key: "mem", color: "--series-2", label: "Memoria" }],
      { max: 100, span: spanCap, fmt: (v, full) => full ? `${v.toFixed(1)}%` : `${Math.round(v)}%` });
    drawChart($(".c-net", card.el), d.history,
      [{ key: "in", color: "--series-1", label: "Entrada" }, { key: "out", color: "--series-2", label: "Salida" }],
      { span: spanCap, fmt: (v, full) => full ? fmtBps(v) : fmtAxis(v) });
  }

  function meter(label, color, val, th, unit = "%") {
    if (val == null) return "";
    const hot = th != null && val >= th;
    const fill = hot ? "var(--critical)" : color;
    return `<div class="meter"><span class="name"><i style="background:${color}"></i>${label}</span>
      <div class="track" role="meter" aria-valuenow="${val}" aria-valuemin="0" aria-valuemax="100" aria-label="${label}">
        <div class="fill" style="width:${Math.min(val, 100)}%;background:${fill}"></div>
        ${th != null ? `<div class="th" style="left:calc(${th}% - 1px)" title="Umbral ${th}${unit}"></div>` : ""}
      </div>
      <span class="val num ${hot ? "hot" : ""}">${val.toFixed(0)}${unit}</span></div>`;
  }

  function updateCard(card, d) {
    card.data = d;
    const el = card.el, st = STATUS[d.status] || STATUS.unknown, sim = state.data.simulate;
    el.className = `card ${d.status}`;
    $(".meta", el).textContent = `${d.role || ""} · ${model(d)} · ${d.host}`;
    const badge = $(".badge", el);
    badge.className = `badge ${d.status}`;
    badge.innerHTML = `<span aria-hidden="true">${st[0]}</span>${st[1]}`;

    const facts = [["Uptime", d.status === "up" ? fmtDur(d.uptime) : "—"]];
    if (d.sessions != null) facts.push(["Sesiones", d.sessions.toLocaleString("es-CO")]);
    if (d.temp != null) facts.push(["Temp.", `${d.temp.toFixed(0)} °C`]);
    if (d.disk != null) facts.push(["Disco", `${d.disk}%`]);
    const up = d.interfaces.filter((i) => i.oper === "up").length;
    facts.push(["Interfaces", `${up}/${d.interfaces.length} up`]);
    facts.push(["Respuesta", d.poll_ms != null && d.status === "up" ? `${d.poll_ms} ms` : "—"]);
    $(".facts", el).innerHTML = facts.slice(0, 6).map(([l, v]) =>
      `<div class="fact"><div class="l">${l}</div><div class="v num">${esc(v)}</div></div>`).join("");

    const th = d.thresholds || {};
    const stale = d.status !== "up";
    $(".meters", el).innerHTML = stale && d.cpu == null ? "" :
      meter("CPU", "var(--series-1)", d.cpu, th.cpu_high) + meter("Memoria", "var(--series-2)", d.mem, th.mem_high);
    $(".meters", el).style.opacity = stale ? 0.45 : 1;

    // interfaces
    const prev = state.prevIf.get(d.name) || {};
    const now = {};
    const rows = d.interfaces.map((i) => {
      const key = `${i.admin}/${i.oper}`;
      now[i.idx] = key;
      const changed = prev[i.idx] && prev[i.idx] !== key;
      let pill;
      if (stale) pill = `<span class="pill stale">? sin datos</span>`;
      else if (i.admin === "down") pill = `<span class="pill admin">⏻ shutdown</span>`;
      else if (i.oper === "up") pill = `<span class="pill up">▲ up</span>`;
      else pill = `<span class="pill down">▼ down</span>`;
      const simBtns = sim ? `<td><div class="sim-row">
          <button class="btn small" data-act="admin" data-idx="${esc(i.idx)}" title="${i.admin === "down" ? "undo shutdown" : "shutdown"}">${i.admin === "down" ? "▶" : "⏻"}</button>
          <button class="btn small" data-act="link" data-idx="${esc(i.idx)}" title="Conectar/desconectar cable" ${i.admin === "down" ? "disabled" : ""}>${i.oper === "up" ? "✂" : "🔌"}</button>
        </div></td>` : "";
      return `<tr class="${changed ? "changed" : ""}">
        <td>${pill}</td>
        <td class="name" title="${esc(i.name)}${i.alias ? " — " + esc(i.alias) : ""}">${esc(i.name)}</td>
        <td class="rate num">${fmtBps(i.in_bps)}</td>
        <td class="rate num">${fmtBps(i.out_bps)}</td>${simBtns}</tr>`;
    }).join("");
    state.prevIf.set(d.name, now);
    $(".ifaces", el).innerHTML = d.interfaces.length ? `<table>
      <thead><tr><th>Estado</th><th>Interfaz</th><th style="text-align:right">Entrada</th><th style="text-align:right">Salida</th>${sim ? "<th></th>" : ""}</tr></thead>
      <tbody>${rows}</tbody></table>` : `<div class="empty">Sin datos de interfaces todavía</div>`;

    const foot = [];
    if (d.last_error && d.status !== "up") foot.push(`<div class="err">Último error: ${esc(d.last_error)}</div>`);
    if (sim) {
      foot.push(`<button class="btn small ${d.status === "down" ? "" : "danger"}" data-act="power">${d.status === "down" ? "⏻ Encender" : "⏻ Apagar"}</button>`);
      foot.push(`<button class="btn small" data-act="spike">📈 Pico CPU/Mem</button>`);
    }
    foot.push(`<button class="btn small" data-act="poll" title="Consultar ahora">↻ Consultar</button>`);
    foot.push(`<span class="spacer"></span><span class="last">Último dato ${ago(d.last_ok)}</span>`);
    $(".card-foot", el).innerHTML = foot.join("");
    drawCharts(card);
  }

  async function onCardClick(e) {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const card = [...state.cards.values()].find((c) => c.el.contains(b));
    const n = encodeURIComponent(card.name), act = b.dataset.act;
    b.disabled = true;
    try {
      if (act === "power") await api(`/api/sim/${n}/power`);
      else if (act === "spike") { await api(`/api/sim/${n}/spike`); toast("info", `Pico de CPU/memoria en ${card.name} durante 60 s`); }
      else if (act === "poll") await api(`/api/poll/${n}`);
      else await api(`/api/sim/${n}/iface/${encodeURIComponent(b.dataset.idx)}/${act}`);
    } catch (err) { toast("warning", esc(err.message)); }
    setTimeout(() => (b.disabled = false), 600);
  }

  // ------------------------------------------------------------ KPIs / paneles
  function renderKpis(s) {
    const devs = s.devices;
    const up = devs.filter((d) => d.status === "up").length;
    const ifs = devs.flatMap((d) => (d.status === "up" ? d.interfaces : []));
    const ifUp = ifs.filter((i) => i.oper === "up").length;
    const crit = s.active.filter((a) => a.severity === "critical").length;
    const lastPoll = Math.max(0, ...devs.map((d) => d.last_poll || 0));
    $("#kpis").innerHTML = `
      <div class="kpi"><div class="label">Equipos en línea</div><div class="value num">${up}<small> / ${devs.length}</small></div>
        <div class="sub">${devs.length - up ? `${devs.length - up} con problemas` : "Todos respondiendo"}</div></div>
      <div class="kpi"><div class="label">Interfaces arriba</div><div class="value num">${ifUp}<small> / ${ifs.length}</small></div>
        <div class="sub">${ifs.length - ifUp} caídas o deshabilitadas</div></div>
      <div class="kpi"><div class="label">Alertas activas</div><div class="value num">${s.active.length}</div>
        <div class="sub">${crit} críticas</div></div>
      <div class="kpi"><div class="label">Último sondeo</div><div class="value num" style="font-size:20px">${lastPoll ? clock(lastPoll) : "—"}</div>
        <div class="sub">cada ${s.poll_interval} s · activo ${fmtDur(s.ts - s.started)}</div></div>`;
  }

  function evItem(e, isNew) {
    return `<li class="ev ${isNew ? "new" : ""}"><span class="ic" aria-label="${e.severity}">${SEV_ICON[e.severity] || "•"}</span>
      <div><div class="msg">${safeMsg(e.message)}</div><div class="when">${dateTime(e.ts)}</div></div></li>`;
  }
  function renderActive(s) {
    const c = $("#active-count");
    c.textContent = s.active.length;
    c.className = `count ${s.active.some((a) => a.severity === "critical") ? "hot" : ""}`;
    $("#active").innerHTML = s.active.length
      ? s.active.map((e) => `<li class="ev"><span class="ic">${SEV_ICON[e.severity]}</span><div><div class="msg">${safeMsg(e.message)}</div><div class="when">${ago(e.ts)}</div></div></li>`).join("")
      : `<li class="empty">✅ Sin alertas activas</li>`;
  }
  function renderEvents(newId) {
    const fd = $("#f-dev").value, fs = $("#f-sev").value;
    const list = state.events.filter((e) => (!fd || e.device === fd) && (!fs || e.severity === fs));
    $("#events").innerHTML = list.length ? list.slice(0, 300).map((e) => evItem(e, e.id === newId)).join("") : `<li class="empty">Sin eventos</li>`;
  }
  function syncDeviceFilter(devs) {
    const sel = $("#f-dev");
    const have = new Set([...sel.options].map((o) => o.value));
    for (const d of devs) if (!have.has(d.name)) sel.add(new Option(d.name, d.name));
  }

  function renderState(s) {
    state.data = s;
    $("#sim-banner").hidden = !s.simulate;
    renderKpis(s);
    renderActive(s);
    syncDeviceFilter(s.devices);
    const host = $("#devices");
    for (const d of s.devices) {
      let card = state.cards.get(d.name);
      if (!card) { card = createCard(d); state.cards.set(d.name, card); host.appendChild(card.el); }
      updateCard(card, d);
    }
  }

  // ------------------------------------------------------------ toasts / sonido
  function toast(sev, html) {
    const t = document.createElement("div");
    t.className = `toast ${sev}`;
    t.innerHTML = `${SEV_ICON[sev] || ""} ${html}`;
    $("#toasts").prepend(t);
    setTimeout(() => t.remove(), sev === "critical" ? 12000 : 6000);
    while ($("#toasts").children.length > 4) $("#toasts").lastChild.remove();
  }
  let audio;
  function beep() {
    try {
      audio = audio || new AudioContext();
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = "square"; o.frequency.value = 880; g.gain.value = 0.05;
      o.connect(g).connect(audio.destination); o.start();
      o.frequency.setValueAtTime(660, audio.currentTime + 0.18);
      o.stop(audio.currentTime + 0.36);
    } catch { /* sin audio */ }
  }

  // ------------------------------------------------------------ WebSocket
  function setConn(ok, text) {
    const c = $("#conn");
    c.className = `conn ${ok ? "ok" : "bad"}`;
    $("span", c).textContent = text;
  }
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    let ping;
    ws.onopen = () => { setConn(true, "En vivo"); ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 20000); };
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "state") renderState(msg.state);
      else if (msg.type === "events") { state.events = msg.events; renderEvents(); }
      else if (msg.type === "event") {
        const e = msg.event;
        state.events.unshift(e);
        state.events.length = Math.min(state.events.length, 1000);
        renderEvents(e.id);
        if (e.kind !== "connected") toast(e.severity, safeMsg(e.message));
        if (e.severity === "critical" && state.sound) beep();
      }
    };
    ws.onclose = () => { clearInterval(ping); setConn(false, "Reconectando…"); setTimeout(connect, 2000); };
  }

  // ------------------------------------------------------------ controles
  $("#f-dev").addEventListener("change", () => renderEvents());
  $("#f-sev").addEventListener("change", () => renderEvents());
  $("#btn-tg").addEventListener("click", async () => {
    try { await api("/api/telegram/test"); toast("info", "Mensaje de prueba enviado a Telegram"); }
    catch (err) { toast("warning", esc(err.message)); }
  });
  $("#btn-sound").addEventListener("click", (e) => {
    state.sound = !state.sound;
    e.currentTarget.textContent = state.sound ? "🔔 Sonido" : "🔕 Sonido";
    e.currentTarget.setAttribute("aria-pressed", state.sound);
    if (state.sound) beep();
  });
  const root = document.documentElement;
  try { const t = localStorage.getItem("netmon-theme"); if (t) root.dataset.theme = t; } catch { /* */ }
  $("#btn-theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("netmon-theme", root.dataset.theme); } catch { /* */ }
    state.cards.forEach((c) => c.redraw());
  });
  window.addEventListener("resize", () => state.cards.forEach((c) => c.redraw()));
  setInterval(() => state.data && renderKpis(state.data), 5000);

  connect();
})();
