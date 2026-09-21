/* admin.js — v1.10 管理页逻辑 */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const gate = $("pinGate"), panel = $("adminPanel");
  let pinFirst = false;   // 是否处于"首次设置 PIN"流程

  async function api(url, opts) {
    const r = await fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
    let d = {};
    try { d = await r.json(); } catch (e) { /* 非 JSON */ }
    d._status = r.status;
    return d;
  }

  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return "-";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
    return (bytes / 1073741824).toFixed(2) + " GB";
  }

  function show(el, text, isErr) {
    el.style.display = "";
    el.textContent = text;
    el.classList.toggle("err", !!isErr);
  }

  /* ---------------- PIN 门 ---------------- */
  async function checkStatus() {
    const d = await api("/api/admin/status");
    if (d.logged) { openPanel(); return; }
    gate.style.display = "flex";
    if (!d.has_pin) {
      pinFirst = true;
      $("pinTitle").textContent = "🔒 首次使用 · 设置管理 PIN";
      $("pinHint").textContent = "为管理页设置一个 PIN 码（至少 4 位），之后每次访问需输入";
      $("pinConfirmHint").style.display = "";
      $("pinConfirm").style.display = "";
    }
    $("pinInput").focus();
  }

  async function tryLogin() {
    const pin = $("pinInput").value.trim();
    const body = { pin: pin };
    if (pinFirst) body.confirm = $("pinConfirm").value.trim();
    const d = await api("/api/admin/login", { method: "POST", body: JSON.stringify(body) });
    if (d.ok) {
      gate.style.display = "none";
      openPanel();
    } else {
      const err = $("pinErr");
      err.style.display = "";
      err.textContent = d.msg || "验证失败";
    }
  }

  function openPanel() {
    gate.style.display = "none";
    panel.style.display = "";
    loadOverview();
    loadDevices(false);
    loadConfigs();
    loadJobs();
    startAutoRefresh();
  }

  $("pinBtn").addEventListener("click", tryLogin);
  $("pinInput").addEventListener("keydown", e => { if (e.key === "Enter") tryLogin(); });
  $("pinConfirm").addEventListener("keydown", e => { if (e.key === "Enter") tryLogin(); });
  $("btnLogout").addEventListener("click", async () => {
    await api("/api/admin/logout", { method: "POST" });
    location.reload();
  });

  /* ---------------- 概览 / 资源 ---------------- */
  function setBar(barId, valId, percent, text) {
    const bar = $(barId);
    bar.style.width = Math.min(100, Math.max(0, percent)) + "%";
    bar.classList.toggle("warn", percent >= 80);
    $(valId).textContent = text;
  }

  async function loadOverview(onlyResources) {
    const d = await api("/api/admin/overview");
    if (d._status === 401) { location.reload(); return; }
    $("verBadge").textContent = "v" + (d.version || "");
    $("uptime").textContent = d.uptime || "--";
    // 资源（每次都刷新）
    setBar("barCpu", "valCpu", d.cpu.percent, d.cpu.percent + "%");
    const temp = d.cpu.temp;
    setBar("barTemp", "valTemp", temp == null ? 0 : Math.min(100, temp / 90 * 100),
      temp == null ? "无传感器" : temp + " °C");
    setBar("barMem", "valMem", d.mem.percent,
      d.mem.percent + "%（" + d.mem.used_mb + " / " + d.mem.total_mb + " MB）");
    setBar("barRoot", "valRoot", d.disk_root.percent,
      d.disk_root.percent + "%（剩 " + d.disk_root.free_gb + " GB）");
    setBar("barScan", "valScan", d.disk_scan.percent,
      d.disk_scan.percent + "%（剩 " + d.disk_scan.free_gb + " GB）");
    $("scanDirSize").textContent = "扫描目录当前占用：" + fmtSize(d.scan_dir_size);
    // 依赖表和配置回显只在首次加载时执行，自动刷新时跳过（防闪烁）
    if (onlyResources) return;
    const tb = $("envTable").querySelector("tbody");
    tb.innerHTML = "";
    (d.env.deps || []).forEach(x => {
      tb.insertAdjacentHTML("beforeend",
        "<tr><td>" + x.name + "</td><td>" + (x.ok ? "✓" : "✗") + "</td><td>" + (x.ver || "") + "</td></tr>");
    });
    tb.insertAdjacentHTML("beforeend",
      "<tr><td>服务用户</td><td>" + (d.env.user ? "✓" : "?") + "</td><td>" + (d.env.user || "未知") +
      "（所属组：" + (d.env.groups || []).join(", ") + "）</td></tr>");
    tb.insertAdjacentHTML("beforeend",
      "<tr><td>扫描仪访问组 (lp/scanner)</td><td>" + (d.env.scan_group_ok ? "✓" : "✗") +
      "</td><td>" + (d.env.scan_group_ok ? "已加入，可访问 USB 扫描设备" : "未加入 lp/scanner 组，可能无法发现设备") + "</td></tr>");
    tb.insertAdjacentHTML("beforeend",
      "<tr><td>存储目录</td><td>" + (d.env.scan_root_writable ? "✓" : "✗") + "</td><td>" + d.env.scan_root +
      (d.env.scan_root_writable ? "（可写）" : (d.env.scan_root_exists ? "（无写入权限！）" : "（不存在！）")) + "</td></tr>");
    // 配置回显（首次加载）
    if (!$("scanRootInput").value) $("scanRootInput").value = d.scan_root || "";
    if (document.activeElement !== $("cfgJobs")) $("cfgJobs").value = d.cleanup.max_jobs;
    if (document.activeElement !== $("cfgDays")) $("cfgDays").value = d.cleanup.max_age_days;
    if (document.activeElement !== $("cfgMB")) $("cfgMB").value = d.cleanup.max_total_mb;
  }

  let autoTimer = null;
  function startAutoRefresh() {
    if (autoTimer) clearInterval(autoTimer);
    autoTimer = setInterval(() => loadOverview(true), 30000);
  }

  /* ---------------- 设备（含别名+默认值） ---------------- */
  const MODE_CN = { Color: "彩色", Gray: "灰度", Lineart: "黑白" };
  let _devAliases = {};
  let _devDefaults = {};
  async function loadDevices(force) {
    const box = $("devBox");
    box.innerHTML = '<p class="hint">探测中…（scanimage -L/-A，ARM 设备约需数秒）</p>';
    const d = await api("/api/admin/devices" + (force ? "?refresh=1" : ""));
    if (d._status === 401) { location.reload(); return; }
    if (!d.scanimage_ok) {
      box.innerHTML = '<p class="status err">scanimage 未安装，无法探测设备</p>';
      return;
    }
    if (!(d.devs || []).length) {
      box.innerHTML = '<p class="status err">未发现扫描设备：请检查扫描仪电源与 USB 连接</p>';
      return;
    }
    // 获取当前配置（别名+默认值）
    const cfg = await api("/api/admin/config");
    _devAliases = cfg.device_alias || {};
    _devDefaults = cfg.scan_defaults || {};

    box.innerHTML = "";
    d.devs.forEach((x, i) => {
      const modes = (x.modes || []).map(m => MODE_CN[m] || m).join(" / ") || "未知";
      const alias = (_devAliases[x.name] || "").trim();
      const devTitle = alias ? alias + ' <span class="hint">(' + x.name + ')</span>' : x.name;
      const eid = btoa(x.name).replace(/=/g, "");
      const dv = _devDefaults[x.name] || {};
      const curDpi = dv.dpi || "150";
      const curMode = dv.mode || "Gray";
      const curCrop = dv.crop ? "checked" : "";
      // DPI 选项从设备探测结果生成
      const dpis = x.dpi_raw || ["75","100","150","200","300","600","1200"];
      const dpiOpts = dpis.map(d => '<option value="'+d+'"'+(d===curDpi?' selected':'')+'>'+d+'</option>').join("");
      const modeOpts = (x.modes || ["Gray","Color"]).map(m =>
        '<option value="'+m+'"'+(m===curMode?' selected':'')+'>'+(MODE_CN[m]||m)+'</option>').join("");

      box.insertAdjacentHTML("beforeend",
        '<div class="dev-card">' +
        (d.devs.length > 1 ? '<span class="dev-num">' + (i + 1) + '</span> ' : '') +
        '<b>' + devTitle + "</b>" +
        '<p class="hint">' + (x.desc || "") + "</p>" +
        '<table class="dev-table">' +
        "<tr><td>扫描能力</td><td>" + x.scan_type + "</td></tr>" +
        "<tr><td>分辨率范围</td><td>" + (x.dpi || "未知") + "</td></tr>" +
        "<tr><td>色彩模式</td><td>" + modes + "</td></tr>" +
        (x.error ? '<tr><td>探测异常</td><td class="err">' + x.error + "</td></tr>" : "") +
        "</table>" +
        '<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:10px">' +
        '<div class="cfg-row" style="margin-bottom:6px">' +
        '<label class="hint" style="white-space:nowrap">别名</label>' +
        '<input type="text" id="alias_'+eid+'" placeholder="'+x.name+'" value="'+alias+'" style="flex:1;min-width:80px">' +
        '</div>' +
        '<div class="cfg-grid" style="grid-template-columns:auto auto auto 1fr;gap:6px;align-items:center">' +
        '<span class="hint" style="display:flex;align-items:center;gap:4px">默认DPI <select id="dpi_'+eid+'" style="padding:4px;border-radius:6px;border:1px solid var(--border)">'+dpiOpts+'</select></span>' +
        '<span class="hint" style="display:flex;align-items:center;gap:4px">默认色彩 <select id="mode_'+eid+'" style="padding:4px;border-radius:6px;border:1px solid var(--border)">'+modeOpts+'</select></span>' +
        '<span class="hint" style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="crop_'+eid+'" '+curCrop+'> 裁边</span>' +
        '<button class="primary" data-dev-save="'+x.name+'" style="white-space:nowrap">保存</button>' +
        '</div></div></div>');
    });
    $("devCacheNote").textContent = d.cached ? "（5 分钟内使用缓存，点「重新探测」强制刷新）" : "（刚完成实时探测）";

    // 绑定保存按钮
    box.querySelectorAll("[data-dev-save]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const devName = btn.dataset.devSave;
        const eid = btoa(devName).replace(/=/g, "");
        const alias = $("alias_" + eid).value.trim();
        const dpi = $("dpi_" + eid).value;
        const mode = $("mode_" + eid).value;
        const crop = $("crop_" + eid).checked;
        // 保存别名
        if (alias) { _devAliases[devName] = alias; } else { delete _devAliases[devName]; }
        // 保存默认值
        _devDefaults[devName] = { dpi: dpi, mode: mode, crop: crop };
        const d = await api("/api/admin/config", {
          method: "POST",
          body: JSON.stringify({ device_alias: _devAliases, scan_defaults: _devDefaults })
        });
        if (d.ok) {
          btn.textContent = "已保存 ✓";
          setTimeout(() => btn.textContent = "保存", 1500);
        } else {
          alert(d.msg || "保存失败");
        }
      });
    });
  }

  $("btnProbe").addEventListener("click", () => loadDevices(true));
  $("btnRefreshAll").addEventListener("click", () => { loadOverview(); loadDevices(false); loadConfigs(); loadJobs(); });
  $("btnTestScan").addEventListener("click", async () => {
    if (!confirm("将执行一次平板单页测试扫描（约 10–30 秒，灰度 150dpi），生成一个「测试扫描」任务。继续？")) return;
    const btn = $("btnTestScan");
    btn.disabled = true; btn.textContent = "扫描中…";
    const d = await api("/api/admin/testscan", { method: "POST" });
    btn.disabled = false; btn.textContent = "测试扫描";
    if (d.ok) {
      alert("测试扫描成功！已生成任务：" + d.job + "（" + d.pages + " 页），可在历史任务中查看");
      loadJobs();
    } else {
      alert(d.msg || "测试扫描失败");
    }
  });

  /* ---------------- 配置 ---------------- */
  async function loadConfigs() {
    const cfg = await api("/api/admin/config");
    if (cfg._status === 401) { return; }
    if ($("scanRootInput")) $("scanRootInput").value = cfg.scan_root || "";
    if ($("cfgJobs")) $("cfgJobs").value = (cfg.cleanup || {}).max_jobs || 0;
    if ($("cfgDays")) $("cfgDays").value = (cfg.cleanup || {}).max_age_days || 0;
    if ($("cfgMB")) $("cfgMB").value = (cfg.cleanup || {}).max_total_mb || 0;
  }
  $("btnSaveRoot").addEventListener("click", async () => {
    const msg = $("rootMsg");
    const path = $("scanRootInput").value.trim();
    const d = await api("/api/admin/config", {
      method: "POST",
      body: JSON.stringify({ scan_root: path, confirm: $("scanRootInput").dataset.confirm === "1" })
    });
    if (d.ok) {
      $("scanRootInput").dataset.confirm = "";
      show(msg, "已保存 ✓ 存储路径：" + d.scan_root + (d.deleted && d.deleted.length ? "（本次清理 " + d.deleted.length + " 个任务）" : ""));
      loadOverview(); loadJobs();
    } else if (d.need_confirm) {
      show(msg, d.msg + " 点击「保存路径」再次确认以创建。", true);
      $("scanRootInput").dataset.confirm = "1";
    } else {
      show(msg, d.msg || "保存失败", true);
      $("scanRootInput").dataset.confirm = "";
    }
  });

  $("btnSaveCleanup").addEventListener("click", async () => {
    const msg = $("cleanupMsg");
    const d = await api("/api/admin/config", {
      method: "POST",
      body: JSON.stringify({ cleanup: { max_jobs: $("cfgJobs").value, max_age_days: $("cfgDays").value, max_total_mb: $("cfgMB").value } })
    });
    if (d.ok) {
      const n = (d.deleted || []).length;
      show(msg, "清理策略已保存 ✓" + (n ? "本次立即清理了 " + n + " 个超限任务（锁定任务未动）" : "（当前无超限任务）"));
      loadJobs();
    } else {
      show(msg, d.msg || "保存失败", true);
    }
  });

  /* ---------------- 任务管理 ---------------- */
  async function loadJobs() {
    const d = await api("/api/admin/jobs");
    if (d._status === 401) { location.reload(); return; }
    const tb = $("jobsTable").querySelector("tbody");
    tb.innerHTML = "";
    if (!(d.jobs || []).length) {
      tb.innerHTML = '<tr><td colspan="5" class="hint">暂无任务</td></tr>';
      return;
    }
    d.jobs.forEach(j => {
      tb.insertAdjacentHTML("beforeend",
        "<tr" + (j.locked ? ' class="locked-row"' : "") + ">" +
        "<td>" + (j.locked ? "🔒 " : "") + (j.remark || j.name) + "<br><span class='hint'>" + j.name + "</span></td>" +
        "<td>" + j.created + "</td>" +
        "<td>" + j.pages + "</td>" +
        "<td>" + fmtSize(j.size) + "</td>" +
        "<td>" +
        '<button class="btn sm" data-lock="' + j.name + '" data-v="' + (j.locked ? 0 : 1) + '">' + (j.locked ? "解锁" : "锁定") + "</button> " +
        '<button class="btn sm danger" data-del="' + j.name + '"' + (j.locked ? " disabled" : "") + ">删除</button>" +
        "</td></tr>");
    });
    tb.querySelectorAll("button[data-lock]").forEach(b => b.addEventListener("click", async () => {
      await api("/api/admin/jobs/" + encodeURIComponent(b.dataset.lock) + "/lock",
        { method: "POST", body: JSON.stringify({ locked: b.dataset.v === "1" }) });
      loadJobs();
    }));
    tb.querySelectorAll("button[data-del]").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("确认删除任务 " + b.dataset.del + " ？此操作不可恢复。")) return;
      const d2 = await api("/api/admin/jobs/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
      if (d2.ok) loadJobs();
      else alert(d2.msg || "删除失败");
    }));
  }
  $("btnReloadJobs").addEventListener("click", loadJobs);

  /* ---------------- 启动 ---------------- */
  checkStatus();
})();