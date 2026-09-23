/* ==========================================================================
   大云壁画工具箱 · 精卫 Jingwei
   app.js —— 状态管理、API 客户端、三条工作流

   三条工作流：
     1. 回贴定位  /api/analyze
     2. 对比导出  （复用 1 的结果）
     3. 区域裁剪  /api/upload_pair + /api/crop_pair
   ========================================================================== */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MAX_HISTORY = 20;

  let lastResult = null;

  /* ------------------------------------------------------------ 工具函数 */

  const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ESCAPE_MAP[c]);
  }

  function fmtTime(unixSeconds) {
    const d = new Date(unixSeconds * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  /** 避免 -0.004 被格式化成 "-0.00"。 */
  function fixZero(value) {
    return Math.abs(value) < 5e-3 ? 0 : value;
  }

  /** 后端统一错误结构 {error:{code,message,detail}}，也兼容旧的纯字符串。 */
  function errText(data, fallback) {
    if (!data) return fallback || "请求失败";
    const err = data.error;
    if (typeof err === "string") return err;
    if (err && typeof err === "object") {
      return err.detail ? (err.message + "（" + err.detail + "）") : (err.message || fallback);
    }
    return data.message || fallback || "请求失败";
  }

  /** 从 /api/… 返回里判断是否成功，失败直接抛人话错误。 */
  function unwrap(resp, data) {
    if (!resp.ok || !data || data.ok !== true) {
      throw new Error(errText(data, "请求失败（HTTP " + resp.status + "）"));
    }
    return data;
  }

  async function fetchJson(url, options) {
    const resp = await fetch(url, options);
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      throw new Error("服务返回了无法解析的内容（HTTP " + resp.status + "）");
    }
    return unwrap(resp, data);
  }

  function postJson(url, payload) {
    return fetchJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  /* ------------------------------------------------------------ 页头状态 */

  async function refreshHealth() {
    const dot = $("healthDot");
    const text = $("healthText");
    try {
      const data = await fetchJson("/api/health");
      dot.className = "status-dot ok";
      text.textContent = "服务正常";
      if (data.version) $("verLabel").textContent = "v" + data.version;
      if (data.port) $("portLabel").textContent = ":" + data.port;
    } catch (e) {
      dot.className = "status-dot bad";
      text.textContent = "服务未响应";
    }
  }

  /* ------------------------------------------------------------ 导航 */

  function switchPage(name) {
    document.querySelectorAll(".nav-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.page === name);
    });
    document.querySelectorAll(".page").forEach((section) => {
      section.hidden = section.id !== "page-" + name;
    });
    if (name === "crop") {
      // 画布尺寸依赖可见后的布局，切回来时重新适配一次
      setTimeout(() => { if (pairReady()) zoomFit(); }, 0);
    }
  }

  document.querySelectorAll(".nav-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchPage(btn.dataset.page));
  });

  /* ------------------------------------------------------------ 拖放选择 */

  function wireDrop(zoneId, inputId, nameId) {
    const zone = $(zoneId), input = $(inputId), nameEl = $(nameId);
    if (!zone || !input) return;

    const label = () => {
      const f = input.files && input.files[0];
      if (!f) {
        nameEl.textContent = "未选择";
        zone.classList.remove("has-file");
        return;
      }
      nameEl.innerHTML = escapeHtml(f.name) +
        "<small>" + (f.size / 1024 / 1024).toFixed(2) + " MB</small>";
      zone.classList.add("has-file");
    };

    zone.addEventListener("click", () => input.click());
    zone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    ["dragenter", "dragover"].forEach((ev) =>
      zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) =>
      zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", (e) => {
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!file) return;
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      label();
    });
    input.addEventListener("change", label);
  }

  wireDrop("dropLarge", "largeInput", "largeName");
  wireDrop("dropPatch", "patchInput", "patchName");

  /* ------------------------------------------------------------ 通用状态条 */

  function makeStatus(setter) {
    return (text, cls) => setter(text, cls || "");
  }

  const setStatus = makeStatus((text, cls) => {
    const el = $("status");
    el.textContent = text;
    el.className = "status " + cls;
  });

  /* ------------------------------------------------------------ 目录选择 */

  async function pickFolder(initial, onPicked, onStatus) {
    onStatus("正在打开文件夹选择对话框…", "working");
    try {
      const data = await postJson("/api/pick_folder", { initial_dir: initial || "" });
      if (data.path) {
        onPicked(data.path);
        onStatus("已选定导出目录。", "ok");
      } else {
        onStatus("未选择目录（保留当前值）。", "");
      }
    } catch (err) {
      onStatus("目录选择失败：" + err.message, "err");
    }
  }

  $("browseBtn").addEventListener("click", () =>
    pickFolder($("outputDir").value, (p) => { $("outputDir").value = p; }, setStatus));

  /* ============================================================ 工作流 1：回贴定位 */

  $("runBtn").addEventListener("click", async () => {
    const large = $("largeInput").files[0];
    const patch = $("patchInput").files[0];
    if (!large || !patch) {
      setStatus("请先选择原始大图和局部修复图。", "err");
      return;
    }

    const form = new FormData();
    form.append("large", large);
    form.append("patch", patch);
    form.append("search_long_side", $("searchLong").value);
    form.append("detect_long_side", $("detectLong").value);
    form.append("feather_px", $("featherPx").value);
    form.append("output_dir", $("outputDir").value);
    form.append("export_long_side", $("exportLong").value);

    $("runBtn").disabled = true;
    setStatus("正在用 SIFT 匹配，估计位置与缩放…", "working");

    try {
      const data = await fetchJson("/api/analyze", { method: "POST", body: form });
      lastResult = data;
      renderAlign(data);
      renderCompare(data);

      const saved = data.saved_subdir || "";
      [["savedRow", "savedPath"], ["savedRow2", "savedPath2"]].forEach(([rowId, pathId]) => {
        $(rowId).hidden = !saved;
        if (saved) $(pathId).textContent = saved;
      });
      $("exportPill").textContent = data.export_long_side > 0
        ? "导出长边 " + data.export_long_side + "px"
        : "原始分辨率";

      setStatus("完成，用时 " + data.elapsed_sec + "s。可切到「对比导出」拿同尺寸 PNG。", "ok");
      loadHistory();
    } catch (err) {
      setStatus(err.message || String(err), "err");
    } finally {
      $("runBtn").disabled = false;
    }
  });

  function renderAlign(data) {
    $("alignEmpty").hidden = true;
    $("alignResults").hidden = false;

    $("previewImg").src = data.preview || "";

    const m = data.match || {};
    $("mx").textContent = m.x;
    $("my").textContent = m.y;
    $("mw").textContent = m.width + " px";
    $("mh").textContent = m.height + " px";
    $("ms").textContent = Number(m.scale || 0).toFixed(4);
    $("mr").textContent = fixZero(Number(m.rotation_deg || 0)).toFixed(2);
    $("mp").textContent = (m.inliers || 0) + " / " + (m.good_matches || 0);
    $("mt").textContent = Number(data.elapsed_sec || 0).toFixed(2);

    const methodLabel = {
      "sift_scale_translation_ls_two_scale": "SIFT · 平移 + 缩放",
      "sift_similarity_ransac_two_scale": "SIFT · 含小角度",
    }[m.method] || m.method || "-";
    $("methodPill").textContent = methodLabel;

    $("runMeta").textContent = "run " + (data.run_id || "-");
    $("downloadMergedHard").href = (data.links && data.links.merged_hard) || "#";
    $("downloadMergedFeather").href = (data.links && data.links.merged_feather) || "#";
  }

  /* ============================================================ 工作流 2：对比导出 */

  function renderCompare(data) {
    $("compareEmpty").hidden = true;
    $("compareResults").hidden = false;

    const links = data.links || {};
    const dims = (data.match ? data.match.width + " × " + data.match.height : "-");

    $("compareDims").textContent = dims;
    $("srcDimA").textContent = dims;
    $("alnDimA").textContent = dims;
    $("srcImgA").src = links.source_crop || "";
    $("alnImgA").src = links.aligned_patch || "";

    $("dlSource").href = links.source_crop || "#";
    $("dlAligned").href = links.aligned_patch || "#";
    $("dlCompare").href = links.compare_png || "#";
    $("dlZip").href = links.zip || "#";
    $("dlZip").style.display = links.zip ? "" : "none";
  }

  /* ============================================================ 历史记录 */

  async function loadHistory() {
    const listEl = $("historyList");
    try {
      const data = await fetchJson("/api/history?limit=" + MAX_HISTORY);
      const items = data.items || [];
      if (!items.length) {
        listEl.innerHTML = '<div class="history-empty">还没有历史记录</div>';
        return;
      }
      listEl.innerHTML = items.map((it) => {
        const dim = (it.width && it.height) ? (it.width + " × " + it.height) : "-";
        const scale = it.scale != null ? Number(it.scale).toFixed(3) : "-";
        const out = it.output_dir
          ? '<div class="sub"><strong>out</strong> ' + escapeHtml(it.output_dir) + "</div>"
          : "";
        return '<div class="history-item" data-id="' + escapeHtml(it.run_id) + '">' +
          '<div class="top"><span class="time">' + fmtTime(it.mtime) + "</span>" +
          '<span class="pill">内点 ' + (it.inliers || 0) + "/" + (it.good_matches || 0) + "</span></div>" +
          '<div class="sub"><strong>位置</strong> (' + (it.x || 0) + "," + (it.y || 0) + ") " + dim +
          " · <strong>缩放</strong> " + scale + "</div>" + out + "</div>";
      }).join("");

      listEl.querySelectorAll(".history-item").forEach((node) => {
        node.addEventListener("click", () => reloadRun(node.dataset.id));
      });
    } catch (e) {
      listEl.innerHTML = '<div class="history-empty">历史记录读取失败</div>';
    }
  }

  async function reloadRun(id) {
    setStatus("正在加载历史记录 " + id + " …", "working");
    try {
      const data = await fetchJson("/api/run?id=" + encodeURIComponent(id));
      lastResult = data;
      renderAlign(data);
      renderCompare(data);
      $("exportPill").textContent = data.export_long_side > 0
        ? "导出长边 " + data.export_long_side + "px"
        : "原始分辨率";
      setStatus("已加载历史记录 " + id, "ok");
    } catch (err) {
      setStatus(err.message || String(err), "err");
    }
  }

  $("refreshHistory").addEventListener("click", loadHistory);

  /* ============================================================ 工作流 3：区域裁剪 */

  let pairSession = null;
  let fullW = 0, fullH = 0, prevW = 0, prevH = 0, ratioPrevToFull = 1;

  let scale = 1, panX = 0, panY = 0;
  let drawing = false, drawStart = null;
  let panning = false, panStart = null, panStartXY = null;
  let spaceDown = false;
  let rect = null;              // {x, y, w, h} in preview space
  let rectDrag = null;

  const stage = $("viewerStage");
  const viewerImg = $("viewerImg");
  const rectEl = $("viewerRect");

  const setPairStatus = makeStatus((text, cls) => {
    const el = $("pairStatus");
    el.textContent = text;
    el.className = "status " + cls;
  });

  const pairReady = () => !!pairSession;

  wireDrop("dropBefore", "beforeInput", "beforeName");
  wireDrop("dropAfter", "afterInput", "afterName");

  $("pairBrowseBtn").addEventListener("click", () =>
    pickFolder($("pairOutputDir").value, (p) => { $("pairOutputDir").value = p; }, setPairStatus));

  $("loadPairBtn").addEventListener("click", async () => {
    const before = $("beforeInput").files[0];
    const after = $("afterInput").files[0];
    if (!before || !after) {
      setPairStatus("请先选择修复前和修复后两张图。", "err");
      return;
    }

    setPairStatus("正在上传并生成预览…", "working");
    const form = new FormData();
    form.append("before", before);
    form.append("after", after);

    try {
      const data = await fetchJson("/api/upload_pair", { method: "POST", body: form });
      pairSession = data.session_id;
      fullW = data.full_size.w;
      fullH = data.full_size.h;
      prevW = data.preview_size.w;
      prevH = data.preview_size.h;
      ratioPrevToFull = data.ratio_preview_to_full;

      $("cropEmpty").hidden = true;
      $("cropResults").hidden = false;
      $("fullSizeTxt").textContent = fullW + " × " + fullH;
      viewerImg.src = data.preview_url;
      $("thumbBefore").src = data.before_preview_url;
      $("thumbAfter").src = data.preview_url;

      rect = null;
      zoomFit();
      $("cropPairBtn").disabled = false;

      setPairStatus(data.resized_msg
        ? data.resized_msg + " — 在画布上拖框，然后点「裁剪并保存」。"
        : "预览就绪。在画布上拖框，然后点「裁剪并保存」。", "ok");
    } catch (err) {
      setPairStatus(err.message || String(err), "err");
    }
  });

  function applyTransform() {
    viewerImg.style.transform = "translate(" + panX + "px, " + panY + "px) scale(" + scale + ")";

    if (rect) {
      rectEl.hidden = false;
      rectEl.style.left = (rect.x * scale + panX) + "px";
      rectEl.style.top = (rect.y * scale + panY) + "px";
      rectEl.style.width = (rect.w * scale) + "px";
      rectEl.style.height = (rect.h * scale) + "px";

      const fx = Math.round(rect.x / ratioPrevToFull);
      const fy = Math.round(rect.y / ratioPrevToFull);
      const fw = Math.round(rect.w / ratioPrevToFull);
      const fh = Math.round(rect.h / ratioPrevToFull);
      const text = "x=" + fx + " y=" + fy + " w=" + fw + " h=" + fh;
      rectEl.setAttribute("data-coord", text);
      $("rectLabel").textContent = text;
      $("fullBoxTxt").textContent = "(" + fx + ", " + fy + ") → (" + (fx + fw) + ", " + (fy + fh) + ")";
      $("fullBoxSize").textContent = fw + " × " + fh;
    } else {
      rectEl.hidden = true;
      $("rectLabel").textContent = "未选框";
      $("fullBoxTxt").textContent = "-";
      $("fullBoxSize").textContent = "-";
    }

    $("zoomLabel").textContent = Math.round(scale * 100) + "%";
  }

  function zoomFit() {
    if (!prevW) return;
    const sw = stage.clientWidth, sh = stage.clientHeight;
    if (!sw || !sh) return;
    scale = Math.min(sw / prevW, sh / prevH);
    panX = (sw - prevW * scale) / 2;
    panY = (sh - prevH * scale) / 2;
    applyTransform();
  }

  function zoomAt(cx, cy, factor) {
    const nx = (cx - panX) / scale;
    const ny = (cy - panY) / scale;
    scale = Math.max(0.02, Math.min(40, scale * factor));
    panX = cx - nx * scale;
    panY = cy - ny * scale;
    applyTransform();
  }

  $("zoomIn").addEventListener("click", () => zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 1.25));
  $("zoomOut").addEventListener("click", () => zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, 0.8));
  $("zoomFit").addEventListener("click", zoomFit);
  $("zoom100").addEventListener("click", () => {
    scale = 1;
    panX = (stage.clientWidth - prevW) / 2;
    panY = (stage.clientHeight - prevH) / 2;
    applyTransform();
  });
  $("clearRect").addEventListener("click", () => { rect = null; applyTransform(); });

  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space") return;
    if ($("page-crop").hidden) return;
    spaceDown = true;
    stage.classList.add("panning");
  });
  window.addEventListener("keyup", (e) => {
    if (e.code !== "Space") return;
    spaceDown = false;
    stage.classList.remove("panning");
  });

  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.15 : 0.87);
  }, { passive: false });

  stage.addEventListener("pointerdown", (e) => {
    const r = stage.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;

    if (e.button === 2 || spaceDown) {
      panning = true;
      panStart = { x, y };
      panStartXY = { x: panX, y: panY };
      stage.classList.add("dragging");
      stage.setPointerCapture(e.pointerId);
      return;
    }
    if (e.button !== 0) return;

    const px = (x - panX) / scale, py = (y - panY) / scale;
    drawing = true;
    drawStart = { xp: px, yp: py };
    rect = { x: px, y: py, w: 0, h: 0 };
    stage.setPointerCapture(e.pointerId);
  });

  stage.addEventListener("pointermove", (e) => {
    const r = stage.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;

    if (panning) {
      panX = panStartXY.x + (x - panStart.x);
      panY = panStartXY.y + (y - panStart.y);
      applyTransform();
      return;
    }
    if (drawing) {
      const px = (x - panX) / scale, py = (y - panY) / scale;
      rect.x = Math.min(drawStart.xp, px);
      rect.y = Math.min(drawStart.yp, py);
      rect.w = Math.abs(px - drawStart.xp);
      rect.h = Math.abs(py - drawStart.yp);
      applyTransform();
    }
  });

  stage.addEventListener("pointerup", () => {
    if (panning) { panning = false; stage.classList.remove("dragging"); }
    if (drawing) {
      drawing = false;
      if (rect && (rect.w < 2 || rect.h < 2)) { rect = null; applyTransform(); }
    }
  });
  stage.addEventListener("pointercancel", () => {
    drawing = false;
    panning = false;
    stage.classList.remove("dragging");
  });
  stage.addEventListener("contextmenu", (e) => e.preventDefault());

  rectEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !rect) return;
    e.stopPropagation();
    rectDrag = { sx: e.clientX, sy: e.clientY, ox: rect.x, oy: rect.y };
    rectEl.setPointerCapture(e.pointerId);
  });
  rectEl.addEventListener("pointermove", (e) => {
    if (!rectDrag) return;
    const dx = (e.clientX - rectDrag.sx) / scale;
    const dy = (e.clientY - rectDrag.sy) / scale;
    rect.x = Math.max(0, Math.min(prevW - rect.w, rectDrag.ox + dx));
    rect.y = Math.max(0, Math.min(prevH - rect.h, rectDrag.oy + dy));
    applyTransform();
  });
  rectEl.addEventListener("pointerup", () => { rectDrag = null; });
  rectEl.addEventListener("pointercancel", () => { rectDrag = null; });

  $("cropPairBtn").addEventListener("click", async () => {
    if (!pairSession) return;
    if (!rect || rect.w < 1 || rect.h < 1) {
      setPairStatus("请先在画布上拖出一个选框。", "err");
      return;
    }

    const fx = Math.round(rect.x / ratioPrevToFull);
    const fy = Math.round(rect.y / ratioPrevToFull);
    const fw = Math.round(rect.w / ratioPrevToFull);
    const fh = Math.round(rect.h / ratioPrevToFull);
    if (fw < 1 || fh < 1) {
      setPairStatus("选框太小，已清空，请重新框选。", "err");
      rect = null;
      applyTransform();
      return;
    }

    setPairStatus("正在按原图全分辨率裁剪并保存…", "working");
    const form = new FormData();
    form.append("session_id", pairSession);
    form.append("output_dir", $("pairOutputDir").value);
    form.append("name_prefix", $("pairNamePrefix").value);
    form.append("x", fx);
    form.append("y", fy);
    form.append("w", fw);
    form.append("h", fh);

    try {
      const data = await fetchJson("/api/crop_pair", { method: "POST", body: form });
      setPairStatus("已保存至 " + data.saved_dir, "ok");
      rect = null;
      applyTransform();
    } catch (err) {
      setPairStatus(err.message || String(err), "err");
    }
  });

  window.addEventListener("resize", () => { if (pairReady()) zoomFit(); });

  /* ============================================================ 深浅主题 */

  const THEME_KEY = "jingwei-theme";

  function applyTheme(value) {
    const dark = value === "dark";
    document.body.classList.toggle("dark", dark);
    const btn = $("themeToggle");
    if (btn) btn.textContent = dark ? "浅色" : "深色";
    try { localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); } catch (e) { /* 隐私模式忽略 */ }
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* 忽略 */ }
    applyTheme(saved === "dark" ? "dark" : "light");
    const btn = $("themeToggle");
    if (btn) {
      btn.addEventListener("click", () =>
        applyTheme(document.body.classList.contains("dark") ? "light" : "dark"));
    }
  }

  /* ============================================================ 初始化 */

  initTheme();

  (async () => {
    refreshHealth();
    setInterval(refreshHealth, 15000);
    try {
      const data = await fetchJson("/api/default_output_dir");
      if (data.path) {
        $("outputDir").placeholder = data.path;
        $("pairOutputDir").placeholder = data.path;
      }
    } catch (e) { /* 忽略：占用位符保持默认文案 */ }
    loadHistory();
  })();
})();
