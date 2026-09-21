/* ScanWeb 前端逻辑 */
"use strict";

function setStatus(t, isErr) {
  const el = document.getElementById("status");
  if (!el) return;
  el.textContent = t || "";
  el.className = "status" + (isErr ? " err" : "");
}

/* 首页：动态加载 scanimage -L 的设备列表到高级选项 */
function loadDevices() {
  const sel = document.getElementById("devsel");
  if (!sel) return;
  fetch("/api/devices").then(r => r.json()).then(d => {
    (d.devs || []).forEach(dev => {
      const o = document.createElement("option");
      o.value = dev; o.textContent = dev;
      sel.appendChild(o);
    });
  }).catch(() => {});
}

/* 任务页：平板扫一页（阻塞式，后端持有设备锁） */
async function scanPage(job) {
  const b = document.getElementById("btnScan");
  b.disabled = true;
  setStatus("正在扫描，请稍候（平板扫描约需 10–30 秒）…");
  try {
    const r = await fetch(`/api/jobs/${job}/scan`, { method: "POST" });
    const d = await r.json();
    if (!d.ok) throw new Error(d.msg || "扫描失败");
    addThumb(job, d.file);
    setStatus("已完成：" + d.file);
  } catch (e) {
    setStatus("扫描失败：" + e.message, true);
  }
  b.disabled = false;
}

let adfTimer = null;

/* 任务页：ADF 连续扫描（后台执行，前端轮询进度） */
async function startAdf(job) {
  const b = document.getElementById("btnAdf");
  b.disabled = true;
  const r = await fetch(`/api/jobs/${job}/adf`, { method: "POST" });
  const d = await r.json();
  if (!d.ok) { setStatus(d.msg || "启动失败", true); b.disabled = false; return; }
  setStatus("ADF 连续扫描已启动，请放入整叠纸…");
  adfTimer = setInterval(async () => {
    try {
      const s = await (await fetch(`/api/jobs/${job}/status`)).json();
      if (s.state === "scanning") setStatus(`${s.msg || ""}（已扫 ${s.pages} 页）`);
      if (s.state === "done") {
        clearInterval(adfTimer);
        setStatus(s.msg || "完成");
        setTimeout(() => location.reload(), 800);
      }
      if (s.state === "error") {
        clearInterval(adfTimer);
        setStatus(s.msg || "出错", true);
        b.disabled = false;
      }
    } catch (e) { /* 网络抖动，下一轮再试 */ }
  }, 1500);
}

function addThumb(job, png) {
  const wall = document.getElementById("wall");
  const empty = document.getElementById("empty");
  if (empty) empty.remove();
  const fig = document.createElement("figure");
  fig.innerHTML =
    `<a href="/job/${job}/raw/${png}" target="_blank"><img src="/job/${job}/thumb/${png.replace(".png", ".jpg")}"></a>` +
    `<figcaption><span>${png}</span><a href="/job/${job}/raw/${png}" download title="下载">⬇</a></figcaption>`;
  wall.appendChild(fig);
  const c = document.getElementById("pgcount");
  if (c) c.textContent = wall.querySelectorAll("figure").length;
}

async function delJob(job) {
  if (!confirm(`确定删除任务 ${job} ？此操作不可恢复。`)) return;
  const r = await fetch(`/api/jobs/${job}`, { method: "DELETE" });
  const d = await r.json();
  if (d.ok) {
    location.href = "/";
  } else {
    alert(d.msg || "删除失败");
  }
}

/* 任务页初始化：灯箱 */
function initJobPage() {
  const lb = document.getElementById("lightbox");
  const wall = document.getElementById("wall");
  if (!lb || !wall) return;
  wall.addEventListener("click", e => {
    const a = e.target.closest("a");
    if (a && a.querySelector("img")) {
      e.preventDefault();
      lb.querySelector("img").src = a.href;
      lb.style.display = "flex";
    }
  });
}
