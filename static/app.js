/* ScanWeb 前端逻辑 */
"use strict";

function setStatus(t, isErr) {
  const el = document.getElementById("status");
  if (!el) return;
  el.textContent = t || "";
  el.className = "status" + (isErr ? " err" : "");
}

/* 首页：动态加载 scanimage -L 的设备列表 + 探测能力 */
let _devCaps = [];   // 缓存设备能力数据

// 扫描模式中英文映射（scanimage 参数保持英文，界面显示中文）
const MODE_CN = { "Color": "彩色", "Gray": "灰度", "Lineart": "黑白" };

function loadDevices() {
  const devMenu = document.getElementById("devMenu");
  if (!devMenu) return;
  fetch("/api/devices").then(r => r.json()).then(data => {
    _devCaps = data.devs || [];
    _devCaps.forEach(dev => {
      const item = document.createElement("div");
      item.className = "dd-item";
      item.dataset.value = dev.name;
      item.textContent = dev.name;
      devMenu.appendChild(item);
    });
    if (_devCaps.length > 0) {
      updateDeviceOptions("");  // 空值 = 默认设备 = 第一台
    } else {
      const hint = document.getElementById("advHint");
      if (hint) hint.textContent = "未探测到扫描设备，请检查设备连接和权限。";
    }
  }).catch(() => {});
}

/* 根据选中设备的能力，动态显示/隐藏 ADF 选项和模式选项 */
function updateDeviceOptions(selectedDev) {
  // 选中设备为空 = 默认设备，取第一台探测到的设备能力
  const dev = selectedDev
    ? _devCaps.find(d => d.name === selectedDev)
    : _devCaps[0];

  const adfLabel = document.getElementById("adfLabel");
  const sourceName = document.getElementById("source_name");
  const modeDropdown = document.getElementById("modeDropdown");
  const hint = document.getElementById("advHint");

  if (!dev) {
    // 探测失败：全部显示（降级模式，由 scanimage 报错兜底）
    if (adfLabel) adfLabel.style.display = "";
    if (hint) hint.textContent = "设备能力探测失败，所有选项可用。不支持的参数扫描时会报错。";
    return;
  }

  // ADF：设备有非 Flatbed 的 source 才显示"扫描方式"区块
  const adfSources = (dev.sources || []).filter(s =>
    /adf|feeder/i.test(s) && !/flatbed/i.test(s)
  );
  const sourceFieldset = document.getElementById("sourceFieldset");
  if (sourceFieldset) {
    // 无 ADF 时隐藏整个"扫描方式"区块（只有一个选项的 radio 无意义）
    sourceFieldset.style.display = adfSources.length > 0 ? "" : "none";
  }
  if (adfLabel) {
    adfLabel.style.display = adfSources.length > 0 ? "" : "none";
    if (adfSources.length === 0) {
      const flatbedRadio = document.querySelector('input[name="source"][value="flatbed"]');
      if (flatbedRadio) flatbedRadio.checked = true;
    }
  }
  if (sourceName) {
    sourceName.value = adfSources.length > 0 ? adfSources[0] : "";
  }

  // 模式：只保留设备支持的选项（自定义下拉版本）
  if (modeDropdown && dev.modes && dev.modes.length > 0) {
    const modeToggle = modeDropdown.querySelector(".dd-toggle");
    const modeHidden = modeDropdown.querySelector("input[type=hidden]");
    const modeItems = modeDropdown.querySelectorAll(".dd-item");
    const currentVal = modeHidden.value;
    modeItems.forEach(item => {
      item.classList.toggle("hidden", !dev.modes.includes(item.dataset.value));
    });
    // 当前选中的模式如果不支持，切到第一个支持的
    if (!dev.modes.includes(currentVal)) {
      const firstOk = Array.from(modeItems).find(i => dev.modes.includes(i.dataset.value) && !i.classList.contains("hidden"));
      if (firstOk) {
        modeHidden.value = firstOk.dataset.value;
        modeToggle.firstChild.textContent = firstOk.textContent;
        modeItems.forEach(i => i.classList.remove("selected"));
        firstOk.classList.add("selected");
      }
    }
  }

  // 提示文字（模式用中文显示）
  if (hint) {
    const parts = [];
    const cnModes = (dev.modes || ["未知"]).map(m => MODE_CN[m] || m);
    parts.push("模式：" + cnModes.join("/"));
    if (adfSources.length > 0) {
      parts.push("ADF 源：" + adfSources.join("/"));
    } else {
      parts.push("无 ADF（平板模式）");
    }
    hint.textContent = "设备能力已探测 → " + parts.join("，");
  }
}

/* 任务页：平板扫一页（阻塞式，后端持有设备锁） */
async function scanPage(job) {
  const b = document.getElementById("btnScan");
  b.disabled = true;
  setStatus("正在扫描，请稍候（约需 10–30 秒）…");
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

/* ---------- 自定义下拉框逻辑 ---------- */
function initDropdowns() {
  // 点击 toggle 展开/收起菜单
  document.querySelectorAll(".dd-toggle").forEach(toggle => {
    toggle.addEventListener("click", e => {
      e.stopPropagation();
      e.preventDefault();
      const menu = toggle.nextElementSibling;
      const isOpen = menu.classList.contains("open");
      // 先关闭所有菜单
      document.querySelectorAll(".dd-menu.open").forEach(m => {
        if (m !== menu) m.classList.remove("open");
      });
      menu.classList.toggle("open");
    });
  });

  // 点击选项：选中并关闭
  document.querySelectorAll(".dd-menu").forEach(menu => {
    menu.addEventListener("click", e => {
      const item = e.target.closest(".dd-item");
      if (!item || item.classList.contains("hidden")) return;
      const dd = menu.closest(".dropdown");
      const toggle = dd.querySelector(".dd-toggle");
      const hidden = dd.querySelector("input[type=hidden]");
      // 更新显示文本（保留 caret）
      toggle.firstChild.textContent = item.textContent;
      hidden.value = item.dataset.value;
      // 标记选中
      menu.querySelectorAll(".dd-item").forEach(i => i.classList.remove("selected"));
      item.classList.add("selected");
      // 关闭菜单
      menu.classList.remove("open");
      // 设备切换时触发能力探测
      if (hidden.name === "device") {
        updateDeviceOptions(hidden.value);
      }
    });
  });

  // 点击页面其他地方关闭所有菜单
  document.addEventListener("click", () => {
    document.querySelectorAll(".dd-menu.open").forEach(m => m.classList.remove("open"));
  });
}
