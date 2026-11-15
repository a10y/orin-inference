"use strict";

const $ = (s) => document.querySelector(s);
const HEADERS = { "X-Infer-Client": "1" };
let MODELS = {};
let selected = [];
const rendered = new Map(); // job id -> { el, sig }

function fmtBytes(n) {
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtDur(s) {
  if (!isFinite(s) || s < 0) return "";
  s = Math.round(s);
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? `${h}h ${m % 60}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
}
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...HEADERS, ...(opts.headers || {}) } });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(`${r.status}: ${msg}`);
  }
  return r.json();
}

// ---------- models ----------
async function loadModels() {
  const { models, default: def } = await api("/api/models");
  const sel = $("#model");
  const groups = {};
  for (const m of models) {
    MODELS[m.id] = m;
    (groups[m.family] ||= []).push(m);
  }
  for (const [fam, ms] of Object.entries(groups)) {
    const og = document.createElement("optgroup");
    og.label = fam + (ms[0].license.startsWith("AGPL") ? " — AGPL-3.0" : " — BSD-3");
    for (const m of ms) og.append(new Option(m.name, m.id, false, m.id === (localStorage.model || def)));
    sel.append(og);
  }
  if (!MODELS[sel.value]) sel.value = def;
  showModelNotes();
}
function showModelNotes() {
  const m = MODELS[$("#model").value];
  $("#model-notes").textContent = m ? `${m.notes} License: ${m.license}.` : "";
}

// ---------- form ----------
function setFiles(files) {
  selected = [...files];
  const ul = $("#file-list");
  ul.replaceChildren(...selected.map((f) => {
    const li = document.createElement("li");
    li.append(f.name, Object.assign(document.createElement("span"), { className: "muted", textContent: fmtBytes(f.size) }));
    return li;
  }));
  $("#submit").disabled = !selected.length;
  $("#drop-text").innerHTML = selected.length ? `${selected.length} file(s) selected — drop or <u>browse</u> to change` : "Drop images or videos here, or <u>browse</u>";
}

function upload(file, params, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const qs = new URLSearchParams({ filename: file.name, ...params });
    xhr.open("POST", `/api/jobs?${qs}`);
    for (const [k, v] of Object.entries(HEADERS)) xhr.setRequestHeader(k, v);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded, e.total);
    xhr.onload = () => {
      let body; try { body = JSON.parse(xhr.responseText); } catch { body = {}; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(`${file.name}: ${xhr.status} ${typeof body.detail === "string" ? body.detail : xhr.statusText}`));
    };
    xhr.onerror = () => reject(new Error(`${file.name}: network error`));
    xhr.send(file);
  });
}

async function submit(e) {
  e.preventDefault();
  if (!selected.length) return;
  const params = { model: $("#model").value, output: $("#output").value, conf: $("#conf").value };
  localStorage.model = params.model;
  localStorage.output = params.output;
  localStorage.conf = params.conf;
  const btn = $("#submit"), box = $("#upload-progress"), bar = box.querySelector(".bar > div"), label = box.querySelector("span");
  const err = $("#form-error");
  btn.disabled = true; box.hidden = false; err.hidden = true;
  const errors = [];
  const total = selected.reduce((a, f) => a + f.size, 0);
  let done = 0;
  for (const [i, f] of selected.entries()) {
    try {
      await upload(f, params, (loaded) => {
        bar.style.width = `${((done + loaded) / total) * 100}%`;
        label.textContent = `Uploading ${i + 1}/${selected.length}: ${f.name} (${fmtBytes(done + loaded)} / ${fmtBytes(total)})`;
      });
      refresh();
    } catch (ex) { errors.push(ex.message); }
    done += f.size;
  }
  box.hidden = true; bar.style.width = "0";
  if (errors.length) { err.textContent = errors.join("\n"); err.hidden = false; }
  $("#file").value = ""; setFiles([]);
  refresh();
}

// ---------- jobs ----------
function jobSig(j) { return JSON.stringify([j.status, j.processed, j.total, j.message, j.outputs.length, j.error]); }

function renderJob(j) {
  const prev = rendered.get(j.id);
  const sig = jobSig(j);
  if (prev && prev.sig === sig) return prev.el;
  const el = prev?.el || $("#job-tpl").content.firstElementChild.cloneNode(true);
  const m = MODELS[j.model];
  el.querySelector(".job-file").textContent = j.filename;
  el.querySelector(".job-meta").textContent =
    `${j.media} · ${fmtBytes(j.size)} · ${m ? m.name : j.model} · ${j.output === "json" ? "JSON" : "annotated " + j.media} · conf ≥ ${j.conf} · ${new Date(j.created * 1000).toLocaleString()}`;
  const badge = el.querySelector(".badge");
  badge.className = `badge ${j.status}`;
  badge.textContent = j.status;

  const bar = el.querySelector(".job-bar"), fill = bar.firstElementChild, prog = el.querySelector(".job-progress");
  const active = ["queued", "running", "canceling"].includes(j.status);
  bar.hidden = !active;
  const pct = j.total ? Math.min(100, (j.processed / j.total) * 100) : 0;
  bar.classList.toggle("indeterminate", j.status === "running" && !j.total);
  fill.style.width = j.status === "running" ? `${pct}%` : "0";
  if (j.status === "running" && j.total > 1) {
    const eta = j.fps ? (j.total - j.processed) / j.fps : NaN;
    prog.textContent = `${j.message || ""} — frame ${j.processed} / ${j.total} (${pct.toFixed(1)}%) · ${j.fps ?? "–"} fps${isFinite(eta) ? ` · ~${fmtDur(eta)} left` : ""}`;
  } else if (j.status === "done") {
    const s = j.summary;
    const took = j.finished && j.started ? ` in ${fmtDur(j.finished - j.started)}` : "";
    prog.textContent = `${s.total_detections} detections${s.frames > 1 ? ` across ${s.frames} frames` : ""}${took}${s.processing_fps ? ` (${s.processing_fps} fps)` : ""}`;
  } else {
    prog.textContent = j.message || "";
  }
  el.querySelector(".job-error").textContent = j.error || "";

  const classes = el.querySelector(".job-classes");
  classes.replaceChildren(...(j.summary?.classes || []).map((c) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    const sw = document.createElement("i"); sw.style.background = c.color;
    chip.append(sw, `${c.name} · ${c.detections}`);
    chip.title = j.media === "video" ? `${c.detections} detections in ${c.frames} frames` : `${c.detections} detections`;
    return chip;
  }));

  const preview = el.querySelector(".job-preview");
  const out = j.outputs[0];
  if (j.status === "done" && out && !preview.dataset.loaded) {
    preview.dataset.loaded = "1";
    const src = `${out.url}?inline=1`;
    if (out.kind === "image") preview.replaceChildren(Object.assign(document.createElement("img"), { src, loading: "lazy" }));
    else if (out.kind === "video") preview.replaceChildren(Object.assign(document.createElement("video"), { src, controls: true, preload: "metadata" }));
  }

  const actions = el.querySelector(".job-actions");
  const btns = [];
  for (const o of j.outputs) {
    btns.push(Object.assign(document.createElement("a"), { className: "button primary", href: o.url, textContent: `Download ${o.name} (${fmtBytes(o.size)})` }));
    if (o.kind === "json") btns.push(Object.assign(document.createElement("a"), { className: "button", href: `${o.url}?inline=1`, target: "_blank", textContent: "View JSON" }));
  }
  if (["queued", "running"].includes(j.status)) btns.push(button("Cancel", () => api(`/api/jobs/${j.id}/cancel`, { method: "POST" })));
  if (["done", "failed", "canceled"].includes(j.status)) btns.push(button("Delete", async () => {
    if (!confirm(`Delete job for ${j.filename}?`)) return;
    await api(`/api/jobs/${j.id}`, { method: "DELETE" });
  }));
  actions.replaceChildren(...btns);
  rendered.set(j.id, { el, sig });
  return el;
}
function button(text, fn) {
  const b = Object.assign(document.createElement("button"), { className: "secondary", type: "button", textContent: text });
  b.onclick = async () => { b.disabled = true; try { await fn(); } catch (e) { alert(e.message); } refresh(); };
  return b;
}

let refreshing = false, timer = null;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  clearTimeout(timer);
  let active = false;
  try {
    const [{ jobs }, st] = await Promise.all([api("/api/jobs"), api("/api/status")]);
    const box = $("#jobs");
    if (!jobs.length) box.innerHTML = '<p class="muted">No jobs yet.</p>';
    else {
      const els = jobs.map(renderJob);
      if (els.some((el, i) => box.children[i] !== el) || box.children.length !== els.length) box.replaceChildren(...els);
    }
    const ids = new Set(jobs.map((j) => j.id));
    for (const id of rendered.keys()) if (!ids.has(id)) rendered.delete(id);
    active = jobs.some((j) => ["queued", "running", "canceling"].includes(j.status));
    const c = st.counts;
    $("#queue-summary").textContent = `${c.running || 0} running · ${c.queued || 0} queued`;
    $("#server-status").textContent = `connected · GPU models loaded: ${st.loaded_models.join(", ") || "none"} · max upload ${fmtBytes(st.max_upload_bytes)} · results kept ${st.retention_days} days`;
  } catch (e) {
    $("#server-status").textContent = `disconnected (${e.message})`;
  } finally {
    refreshing = false;
    timer = setTimeout(refresh, active ? 1000 : 5000);
  }
}

// ---------- wiring ----------
const drop = $("#drop");
$("#file").onchange = (e) => setFiles(e.target.files);
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); setFiles(e.dataTransfer.files); };
$("#model").onchange = showModelNotes;
$("#conf").oninput = (e) => ($("#conf-val").textContent = e.target.value);
$("#job-form").onsubmit = submit;
if (localStorage.output) $("#output").value = localStorage.output;
if (localStorage.conf) { $("#conf").value = localStorage.conf; $("#conf-val").textContent = localStorage.conf; }

loadModels().catch((e) => ($("#form-error").textContent = e.message, $("#form-error").hidden = false));
refresh();
