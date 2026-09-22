/* ScanWeb 前端逻辑 */
"use strict";

/* ---------- 自定义弹窗（替代浏览器 confirm） ---------- */
function customConfirm(message, title) {
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const modal = document.createElement("div");
    modal.className = "modal-box";
    modal.innerHTML =
      '<div class="modal-title">' + (title || "确认操作") + '</div>' +
      '<div class="modal-body">' + message + '</div>' +
      '<div class="modal-actions">' +
      '<button class="btn modal-cancel">取消</button>' +
      '<button class="btn modal-ok">确认</button>' +
      '</div>';
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    overlay.style.display = "flex";
    const close = (val) => { overlay.remove(); resolve(val); };
    modal.querySelector(".modal-ok").addEventListener("click", () => close(true));
    modal.querySelector(".modal-cancel").addEventListener("click", () => close(false));
    overlay.addEventListener("click", e => { if (e.target === overlay) close(false); });
    modal.querySelector(".modal-ok").focus();
  });
}

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
    // 动态填充设备下拉
    devMenu.innerHTML = "";
    _devCaps.forEach((dev, i) => {
      const item = document.createElement("div");
      item.className = "dd-item" + (i === 0 ? " selected" : "");
      item.dataset.value = dev.name;
      item.textContent = dev.alias || dev.name;
      devMenu.appendChild(item);
    });
    if (_devCaps.length > 0) {
      const first = _devCaps[0];
      const devToggle = document.querySelector("#devDropdown .dd-toggle");
      const devHidden = document.querySelector("#devDropdown input[type=hidden]");
      if (devToggle) devToggle.firstChild.textContent = first.alias || first.name;
      if (devHidden) devHidden.value = first.name;
      updateDeviceOptions(first.name);
    } else {
      const devToggle = document.querySelector("#devDropdown .dd-toggle");
      if (devToggle) devToggle.firstChild.textContent = "未找到有效设备";
      const dpiToggle = document.querySelector('[data-name="dpi"] .dd-toggle');
      if (dpiToggle) dpiToggle.firstChild.textContent = "—";
      const modeToggle = document.querySelector("#modeDropdown .dd-toggle");
      if (modeToggle) modeToggle.firstChild.textContent = "—";
      const hint = document.getElementById("advHint");
      if (hint) hint.textContent = "未找到有效设备，请检查扫描仪连接和权限。";
    }
  }).catch(() => {});
}

/* 根据选中设备的能力，动态填充分辨率和色彩模式 */
function updateDeviceOptions(selectedDev) {
  const dev = selectedDev
    ? _devCaps.find(d => d.name === selectedDev)
    : _devCaps[0];

  const adfLabel = document.getElementById("adfLabel");
  const sourceName = document.getElementById("source_name");
  const modeDropdown = document.getElementById("modeDropdown");
  const dpiDropdown = document.querySelector('[data-name="dpi"]');
  const hint = document.getElementById("advHint");
  const sourceFieldset = document.getElementById("sourceFieldset");

  if (!dev) {
    if (sourceFieldset) sourceFieldset.style.display = "none";
    if (modeDropdown) modeDropdown.style.display = "none";
    if (dpiDropdown) dpiDropdown.style.display = "none";
    if (hint) hint.textContent = "未找到有效设备，请检查扫描仪连接和权限。";
    return;
  }
  if (modeDropdown) modeDropdown.style.display = "";
  if (dpiDropdown) dpiDropdown.style.display = "";

  // ADF
  const adfSources = (dev.sources || []).filter(s =>
    /adf|feeder/i.test(s) && !/flatbed/i.test(s)
  );
  if (sourceFieldset) sourceFieldset.style.display = adfSources.length > 0 ? "" : "none";
  if (adfLabel) adfLabel.style.display = adfSources.length > 0 ? "" : "none";
  if (sourceName) sourceName.value = adfSources.length > 0 ? adfSources[0] : "";

  // 动态填充色彩模式（选中管理页设置的默认值或第一个）
  if (modeDropdown) {
    const modeMenu = modeDropdown.querySelector(".dd-menu");
    const modeToggle = modeDropdown.querySelector(".dd-toggle");
    const modeHidden = modeDropdown.querySelector("input[type=hidden]");
    const modes = dev.modes || ["Gray"];
    const dv = dev.defaults || {};
    const defaultMode = dv.mode || modes[0];
    modeMenu.innerHTML = "";
    modes.forEach(m => {
      const item = document.createElement("div");
      const isSel = (m === defaultMode) || (!modes.includes(defaultMode) && m === modes[0]);
      item.className = "dd-item" + (isSel ? " selected" : "");
      item.dataset.value = m;
      item.textContent = MODE_CN[m] || m;
      modeMenu.appendChild(item);
    });
    const selMode = modes.includes(defaultMode) ? defaultMode : modes[0];
    modeHidden.value = selMode;
    modeToggle.firstChild.textContent = MODE_CN[selMode] || selMode;
  }

  // 动态填充分辨率（根据设备探测到的 DPI 列表 + 管理页默认值）
  if (dpiDropdown) {
    const dpiMenu = dpiDropdown.querySelector(".dd-menu");
    const dpiToggle = dpiDropdown.querySelector(".dd-toggle");
    const dpiHidden = dpiDropdown.querySelector("input[type=hidden]");
    const dpis = dev.dpis || ["150", "300", "600"];
    const dv = dev.defaults || {};
    const defaultDpi = dv.dpi || dpis[0];
    dpiMenu.innerHTML = "";
    dpis.forEach(d => {
      const item = document.createElement("div");
      const isSel = (d === defaultDpi) || (!dpis.includes(defaultDpi) && d === dpis[0]);
      item.className = "dd-item" + (isSel ? " selected" : "");
      item.dataset.value = d;
      item.textContent = d;
      dpiMenu.appendChild(item);
    });
    const selDpi = dpis.includes(defaultDpi) ? defaultDpi : dpis[0];
    dpiHidden.value = selDpi;
    dpiToggle.firstChild.textContent = selDpi;
  }

  // 裁边 checkbox 默认值
  const cropCheckbox = document.querySelector('input[name="crop"]');
  if (cropCheckbox) {
    cropCheckbox.checked = !!(dev.defaults && dev.defaults.crop);
  }

  // 提示文字
  if (hint) {
    const cnModes = (dev.modes || ["未知"]).map(m => MODE_CN[m] || m);
    hint.textContent = "设备能力已探测 → 模式：" + cnModes.join("/") +
      (adfSources.length > 0 ? "，ADF 源：" + adfSources.join("/") : "，无 ADF（平板模式）");
  }
}

/* 任务页：平板扫一页（后端扫描完即返回，转换在后台进行） */
async function scanPage(job) {
  const b = document.getElementById("btnScan");
  b.disabled = true;
  setStatus("正在扫描，请稍候（约需 10–30 秒）…");
  try {
    const r = await fetch(`/api/jobs/${job}/scan`, { method: "POST" });
    const d = await r.json();
    if (!d.ok) throw new Error(d.msg || "扫描失败");
    // 扫描完成，先添加占位图（显示"正在转换"）
    addThumbPlaceholder(job, d.file);
    setStatus("扫描完成，正在后台转换为 PNG…");
    b.disabled = false;               // 立即恢复按钮，可以继续扫描下一页
    // 轮询缩略图是否就绪（转换完成）
    pollThumbReady(job, d.file);
  } catch (e) {
    setStatus("扫描失败：" + e.message, true);
    b.disabled = false;
  }
}

function addThumbPlaceholder(job, png) {
  const wall = document.getElementById("wall");
  const empty = document.getElementById("empty");
  if (empty) empty.remove();
  const fig = document.createElement("figure");
  fig.dataset.name = png;
  fig.id = "fig_" + png.replace(".", "_");
  fig.className = "converting";
  fig.innerHTML =
    '<a><div class="convert-placeholder"><div class="spinner"></div>' +
    '<span class="convert-text">正在转换…</span></div></a>' +
    '<figcaption><span>' + png + ' · 转换中</span></figcaption>';
  wall.appendChild(fig);
  const c = document.getElementById("pgcount");
  if (c) c.textContent = wall.querySelectorAll("figure").length;
}

function pollThumbReady(job, png, retries) {
  retries = retries || 0;
  if (retries > 60) {                // 最多等 60 次 × 1 秒 = 60 秒
    const fig = document.getElementById("fig_" + png.replace(".", "_"));
    if (fig) fig.querySelector("figcaption span").textContent = png + " · 转换超时";
    setStatus("转换超时，可能需要刷新页面查看", true);
    return;
  }
  const thumbUrl = `/job/${job}/thumb/${png.replace(".png", ".jpg")}`;
  fetch(thumbUrl, { cache: "no-store" }).then(r => {
    if (r.ok) {
      // 转换完成，刷新缩略图
      const fig = document.getElementById("fig_" + png.replace(".", "_"));
      if (fig) {
        fig.classList.remove("converting");
        const a = fig.querySelector("a");
        a.innerHTML = '<img src="' + thumbUrl + "?t=" + Date.now() + '" alt="' + png + '">';
        a.href = '/job/' + job + '/raw/' + png;
        fig.querySelector("figcaption").innerHTML =
          '<span>' + png + '</span><a href="/job/' + job + '/raw/' + png + '" download title="下载">\u2B07</a>';
      }
      setStatus("已完成：" + png);
    } else {
      setTimeout(() => pollThumbReady(job, png, retries + 1), 1000);
    }
  }).catch(() => setTimeout(() => pollThumbReady(job, png, retries + 1), 1000));
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

/* 任务页：扫码取件（手机扫码直达本任务页） */
function showQr(job) {
  const layer = document.getElementById("qrLayer");
  const img = document.getElementById("qrImg");
  img.onerror = () => {
    layer.style.display = "none";
    alert("二维码功能未启用：请在服务端安装 qrcode（pip install qrcode）");
  };
  img.src = "/job/" + encodeURIComponent(job) + "/qrcode?" + Date.now();
  layer.style.display = "flex";
}

/* 任务页初始化：灯箱 */
function initJobPage() {
  const lb = document.getElementById("lightbox");
  const wall = document.getElementById("wall");
  if (!lb || !wall) return;
  wall.addEventListener("click", e => {
    if (wall.classList.contains("reorder-mode")) return;  // 排序模式下不开灯箱
    const a = e.target.closest("a");
    if (a && a.querySelector("img")) {
      e.preventDefault();
      lb.querySelector("img").src = a.href;
      lb.style.display = "flex";
    }
  });
}

/* ---------- 页面顺序调整 ---------- */
let _reorderMode = false;
let _draggedFig = null;
let _dropTarget = null;   // 当前蓝框目标（几何最近的那张图）= 松手后的落位图
let _btnSortMode = false;  // 按钮排序模式开关（默认拖拽，开启后显示箭头按钮）
let _deleteMode = false;    // 删除图片模式

function toggleReorderMode(job) {
  const wall = document.getElementById("wall");
  const btnReorder = document.getElementById("btnReorder");
  const btnSaveOrder = document.getElementById("btnSaveOrder");
  const btnCancelOrder = document.getElementById("btnCancelOrder");
  if (!wall) return;

  _reorderMode = !_reorderMode;

  if (_reorderMode) {
    wall.classList.add("reorder-mode");
    btnReorder.style.display = "none";
    btnSaveOrder.style.display = "";
    document.getElementById("btnDeletePages").style.display = "";
    btnCancelOrder.style.display = "";
    _btnSortMode = false;   // 默认拖拽模式，不显示箭头按钮
    // 控制栏插入「按钮排序」开关按钮
    let btnToggle = document.getElementById("btnToggleBtnSort");
    if (!btnToggle) {
      btnToggle = document.createElement("button");
      btnToggle.id = "btnToggleBtnSort";
      btnToggle.className = "btn";
      btnToggle.textContent = "按钮排序";
      btnToggle.addEventListener("click", toggleBtnSortMode);
      btnCancelOrder.parentNode.insertBefore(btnToggle, btnSaveOrder);
    }
    btnToggle.style.display = "";
    // 给每个 figure 加序号标签与左移/右移按钮（按钮默认隐藏，开关控制显隐）
    wall.querySelectorAll("figure").forEach((fig, i) => {
      fig.draggable = true;
      fig.classList.add("reorder-item");
      let badge = fig.querySelector(".reorder-badge");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "reorder-badge";
        fig.appendChild(badge);
      }
      badge.textContent = i + 1;
      if (!fig.querySelector(".mv-btns")) {
        const wrap = document.createElement("div");
        wrap.className = "mv-btns";
        wrap.style.display = "none";   // 默认隐藏
        const left = document.createElement("button");
        left.className = "mv-btn"; left.type = "button";
        left.textContent = "←"; left.title = "前移一位";
        left.addEventListener("click", ev => { ev.stopPropagation(); moveFig(fig, -1); });
        const right = document.createElement("button");
        right.className = "mv-btn"; right.type = "button";
        right.textContent = "→"; right.title = "后移一位";
        right.addEventListener("click", ev => { ev.stopPropagation(); moveFig(fig, 1); });
        wrap.appendChild(left);
        wrap.appendChild(right);
        fig.appendChild(wrap);
      }
      // 同步显隐
      fig.querySelector(".mv-btns").style.display = _btnSortMode ? "flex" : "none";
    });
    if (_btnSortMode) refreshMoveButtons();
  } else {
    cancelReorder();
  }
}

function cancelReorder() {
  const wall = document.getElementById("wall");
  if (!wall) return;
  _reorderMode = false;
  _deleteMode = false;
  wall.classList.remove("reorder-mode", "delete-mode");
  wall.querySelectorAll("figure").forEach((fig, i) => {
    fig.draggable = false;
    fig.classList.remove("reorder-item", "drag-over", "drop-before", "drop-after", "drop-h", "drop-v", "dragging", "del-selected");
    const badge = fig.querySelector(".reorder-badge");
    if (badge) badge.remove();
    const mv = fig.querySelector(".mv-btns");
    if (mv) mv.remove();
    const dm = fig.querySelector(".del-mark");
    if (dm) dm.remove();
  });
  const btnToggle = document.getElementById("btnToggleBtnSort");
  if (btnToggle) btnToggle.remove();
  const btnDel = document.getElementById("btnDeletePages");
  if (btnDel) { btnDel.textContent = "删除图片"; btnDel.classList.remove("danger-fill"); btnDel.classList.add("btn"); }
  _btnSortMode = false;
  // 恢复按钮栏可用状态
  document.querySelectorAll(".actions button, .actions a, .reorder-controls button").forEach(b => b.classList.remove("controls-disabled"));
  // 恢复原始 DOM 顺序
  location.reload();
}

/* 「按钮排序」开关：开启时各图显示 ←/→ 箭头，关闭时纯拖拽（默认） */
function toggleBtnSortMode() {
  _btnSortMode = !_btnSortMode;
  const btnToggle = document.getElementById("btnToggleBtnSort");
  btnToggle.classList.toggle("primary", _btnSortMode);
  btnToggle.textContent = _btnSortMode ? "✓ 按钮排序" : "按钮排序";
  document.querySelectorAll("#wall figure .mv-btns").forEach(wrap => {
    wrap.style.display = _btnSortMode ? "flex" : "none";
  });
  if (_btnSortMode) refreshMoveButtons();
}

/* ---------- 删除图片模式 ---------- */
/* 流程：点删除图片 → 每图显示删除按钮 → 点按钮弹自定义确认 → 确认后变预删除(灰色+文字"取消删除") → 点取消删除直接恢复
   点保存删除弹自定义确认 → 确认后删文件+重编号。取消按钮始终可用。 */
function toggleDeleteMode(job) {
  const wall = document.getElementById("wall");
  const btnDel = document.getElementById("btnDeletePages");
  if (!wall || !btnDel) return;   // v1.14：锁定任务无删除图片按钮

  if (!_deleteMode) {
    _deleteMode = true;
    wall.classList.add("delete-mode");
    btnDel.textContent = "保存删除";
    const btnSave = document.getElementById("btnSaveOrder");
    const btnToggle = document.getElementById("btnToggleBtnSort");
    if (btnSave) btnSave.classList.add("controls-disabled");
    if (btnToggle) btnToggle.classList.add("controls-disabled");
    document.querySelectorAll("#wall figure .mv-btns").forEach(w => w.style.display = "none");
    wall.querySelectorAll("figure").forEach(fig => {
      if (fig.classList.contains("converting")) return;
      fig.draggable = false;
      let mark = fig.querySelector(".del-mark");
      if (mark) mark.remove();
      addDeleteMark(fig);
    });
  } else {
    const marked = Array.from(wall.querySelectorAll("figure.del-selected")).map(f => f.dataset.name);
    if (marked.length === 0) {
      exitDeleteMode();
      return;
    }
    customConfirm("确认删除 " + marked.length + " 张图片？删除后页面将重新编号。", "保存删除").then(ok => {
      if (!ok) return;
      const remaining = Array.from(wall.querySelectorAll("figure:not(.del-selected)"))
        .filter(f => !f.classList.contains("converting"))
        .map(f => f.dataset.name);
      saveDeletePages(job, remaining);
    });
  }
}

function addDeleteMark(fig) {
  const mark = document.createElement("div");
  mark.className = "del-mark del-btn";
  mark.textContent = "\u2715 删除";
  mark.title = "删除此页";
  mark.addEventListener("click", async function(ev) {
    ev.stopPropagation();
    ev.preventDefault();
    // 只在 del-btn 状态下响应（del-cancel 由全局 listener 处理）
    if (!mark.classList.contains("del-btn")) return;
    const ok = await customConfirm("确认将此页标记为删除？", "删除图片");
    if (ok) {
      fig.classList.add("del-selected");
      mark.className = "del-mark del-cancel";
      mark.textContent = "\u21BA 取消删除";
      mark.title = "点击取消删除";
    }
  });
  fig.appendChild(mark);
}

/* 点击取消删除：直接恢复，不需要确认（全局监听，拦截在冒泡前） */
document.addEventListener("click", function(ev) {
  const mark = ev.target.closest(".del-mark.del-cancel");
  if (!mark) return;
  ev.stopPropagation();
  ev.preventDefault();
  const fig = mark.closest("figure");
  fig.classList.remove("del-selected");
  mark.className = "del-mark del-btn";
  mark.textContent = "\u2715 删除";
  mark.title = "删除此页";
}, true);  /* 使用捕获阶段，确保在 addDeleteMark 的 click 之前执行 */

function exitDeleteMode() {
  const wall = document.getElementById("wall");
  const btnDel = document.getElementById("btnDeletePages");
  const btnSave = document.getElementById("btnSaveOrder");
  const btnToggle = document.getElementById("btnToggleBtnSort");
  _deleteMode = false;
  wall.classList.remove("delete-mode");
  if (btnDel) btnDel.textContent = "删除图片";   // v1.14：锁定任务无此按钮，判空
  if (btnSave) btnSave.classList.remove("controls-disabled");
  if (btnToggle) btnToggle.classList.remove("controls-disabled");
  // 移除删除图标和标记
  wall.querySelectorAll("figure").forEach(fig => {
    fig.classList.remove("del-selected");
    const mark = fig.querySelector(".del-mark");
    if (mark) mark.remove();
    const mv = fig.querySelector(".mv-btns");
    if (mv) mv.style.display = _btnSortMode ? "flex" : "none";
    fig.draggable = true;
  });
}

async function saveDeletePages(job, remaining) {
  // 调用 reorder 接口，只传剩余文件——后端会删除不在此列表的文件并重新编号
  const r = await fetch("/api/jobs/" + job + "/reorder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order: remaining, delete: true })
  });
  const d = await r.json();
  if (d.ok) {
    location.reload();
  } else {
    alert(d.msg || "删除失败");
    exitDeleteMode();
  }
}

/* 左移/右移按钮：与相邻图交换位置（触屏/鼠标均可用的排序方式） */
function moveFig(fig, dir) {
  if (!_reorderMode) return;
  const prev = fig.previousElementSibling;
  const next = fig.nextElementSibling;
  if (dir < 0 && prev) {
    prev.before(fig);
  } else if (dir > 0 && next) {
    next.after(fig);
  }
  updateBadges();
  refreshMoveButtons();
}

function refreshMoveButtons() {
  const figs = document.querySelectorAll("#wall figure");
  figs.forEach((fig, i) => {
    const btns = fig.querySelectorAll(".mv-btn");
    if (btns.length === 2) {
      btns[0].disabled = i === 0;
      btns[1].disabled = i === figs.length - 1;
    }
  });
}

function initReorderDrag() {
  const wall = document.getElementById("wall");
  if (!wall) return;

  wall.addEventListener("dragstart", e => {
    if (!_reorderMode) return;
    const fig = e.target.closest("figure");
    if (!fig) return;
    _draggedFig = fig;
    fig.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    // 必须 setData，否则部分浏览器拖拽行为不稳定
    e.dataTransfer.setData("text/plain", fig.dataset.name);
  });

  wall.addEventListener("dragend", () => {
    if (!_reorderMode) return;
    if (_draggedFig) _draggedFig.classList.remove("dragging");
    clearDropHint();
    _draggedFig = null;
    _dropTarget = null;
    updateBadges();
  });

  wall.addEventListener("dragover", e => {
    if (!_reorderMode || !_draggedFig) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const d = computeDropAt(e.clientX, e.clientY);
    if (!d) return;
    _dropTarget = d.target;
    updateDropHint();
  });

  wall.addEventListener("drop", e => {
    if (!_reorderMode || !_draggedFig) return;
    e.preventDefault();
    // 占位语义：蓝框亮在哪张图上，被拖图就放到哪张图的位置（序号），
    // 其余图保持相对顺序自动顺延；与鼠标在目标图的左半边还是右半边无关
    const d = computeDropAt(e.clientX, e.clientY);
    if (d && d.target !== _draggedFig) {
      const all = Array.from(wall.querySelectorAll("figure"));
      if (all.indexOf(_draggedFig) < all.indexOf(d.target)) {
        d.target.after(_draggedFig);
      } else {
        d.target.before(_draggedFig);
      }
    }
    _draggedFig.classList.remove("dragging");
    clearDropHint();
    _draggedFig = null;
    _dropTarget = null;
    updateBadges();
  });
}

/* 蓝框目标计算：找离坐标最近的图（拖到间隙也能正确判定），即松手后的落位图 */
function computeDropAt(x, y) {
  const wall = document.getElementById("wall");
  if (!wall || !_draggedFig) return null;
  const figs = Array.from(wall.querySelectorAll("figure")).filter(f => f !== _draggedFig);
  if (!figs.length) return null;
  let best = null, bestDist = Infinity;
  for (const f of figs) {
    const r = f.getBoundingClientRect();
    // 鼠标到该图矩形的最短距离（为0表示在矩形内部）
    const dx = Math.max(r.left - x, 0, x - r.right);
    const dy = Math.max(r.top - y, 0, y - r.bottom);
    const dist = dx * dx + dy * dy;
    if (dist < bestDist) { bestDist = dist; best = f; }
  }
  return { target: best };
}

/* 蓝框提示：亮起的图 = 松手后图片的落位（与 drop 逻辑完全一致） */
function updateDropHint() {
  const wall = document.getElementById("wall");
  if (!wall) return;
  wall.querySelectorAll("figure").forEach(f => {
    f.classList.remove("drag-over", "drop-before", "drop-after", "drop-h", "drop-v");
  });
  if (_dropTarget) {
    _dropTarget.classList.add("drag-over");
  }
}

function clearDropHint() {
  const wall = document.getElementById("wall");
  if (!wall) return;
  wall.querySelectorAll("figure").forEach(f => {
    f.classList.remove("drag-over", "drop-before", "drop-after", "drop-h", "drop-v");
  });
}

function updateBadges() {
  const wall = document.getElementById("wall");
  if (!wall) return;
  wall.querySelectorAll(".reorder-badge").forEach((badge, i) => {
    badge.textContent = i + 1;
  });
}

async function saveReorder(job) {
  const wall = document.getElementById("wall");
  if (!wall) return;
  const order = Array.from(wall.querySelectorAll("figure")).map(f => f.dataset.name);
  if (order.length < 2) {
    cancelReorder();
    return;
  }
  const r = await fetch(`/api/jobs/${job}/reorder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order: order })
  });
  const d = await r.json();
  if (d.ok) {
    location.reload();
  } else {
    setStatus(d.msg || "保存失败", true);
  }
}

/* ---------- 自定义下拉框逻辑 ---------- */
function initDropdowns() {
  // mousedown 展开/收起菜单（不等到 mouseup，防止和 document click 冲突）
  document.querySelectorAll(".dd-toggle").forEach(toggle => {
    toggle.addEventListener("mousedown", e => {
      e.preventDefault();
      e.stopPropagation();
      const menu = toggle.nextElementSibling;
      const isOpen = menu.classList.contains("open");
      document.querySelectorAll(".dd-menu.open").forEach(m => {
        if (m !== menu) m.classList.remove("open");
      });
      menu.classList.toggle("open");
    });
  });

  // click 选项：选中并关闭
  document.querySelectorAll(".dd-menu").forEach(menu => {
    menu.addEventListener("click", e => {
      const item = e.target.closest(".dd-item");
      if (!item || item.classList.contains("hidden")) return;
      const dd = menu.closest(".dropdown");
      const toggle = dd.querySelector(".dd-toggle");
      const hidden = dd.querySelector("input[type=hidden]");
      toggle.firstChild.textContent = item.textContent;
      hidden.value = item.dataset.value;
      menu.querySelectorAll(".dd-item").forEach(i => i.classList.remove("selected"));
      item.classList.add("selected");
      menu.classList.remove("open");
      if (hidden.name === "device") {
        updateDeviceOptions(hidden.value);
      }
    });
  });

  // mousedown 在页面其他地方时关闭所有菜单
  document.addEventListener("mousedown", e => {
    if (!e.target.closest(".dropdown")) {
      document.querySelectorAll(".dd-menu.open").forEach(m => m.classList.remove("open"));
    }
  });
}

/* 全局扫描占用状态：首页/任务页展示当前是否有人在扫描，防止并发冲突困惑 */
async function checkScanStatus() {
  try {
    const r = await fetch("/api/scan-status");
    const d = await r.json();
    const bar = document.getElementById("scanBusyBar");
    if (!bar) return;
    if (d.busy) {
      bar.style.display = "";
      bar.textContent = "📊 " + d.job + " 正在扫描（已用 " + d.elapsed + " 秒），其他任务需等待完成";
    } else {
      bar.style.display = "none";
    }
  } catch (e) { /* 忽略 */ }
}
