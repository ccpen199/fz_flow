/* FZ 智能分析工作台 · 对话模式
 * 左侧对话流，右侧随对话自然演进的动态画布。
 * 画布上每个产物都是一张可寻址的卡片（artifact），按 kind 分派渲染，
 * 因此新增卡片类型不需要改动对话主循环。
 */
(function () {
  "use strict";

  var state = {
    base: "http://127.0.0.1:18900",
    cid: null,
    canvas: null,
    messages: [],
    charts: new Map(),
    selected: new Set(),
    ui: {
      chartTab: {},
      chartType: {},
      deckPage: {},
      drawer: {},
      clarifyDraft: {},
      canvasSig: "",
      // 用户正在编辑但还没提交的内容（SQL / 本页结论 / 版式…）。
      // 存下来的原因很实际：任何一次 renderCanvas 都会重建 DOM，没有草稿的话
      // 用户刚敲的字会在切换标签、拖一下排序之后凭空消失。
      drafts: {},
      // 展开态同样要记：SQL 折叠块、大纲某页的内容编辑区、预览区的编辑面板。
      sqlOpen: {},
      pageEditor: {},
      slideEdit: {},
      // 中栏（动态画布）是否收成窄条。**只由用户点「收起」触发**：
      // 勾选 PPT 模式不再自动收起画布——后续对话还要对着大纲改，画布得在场。
      canvasCollapsed: false,
      // 是否勾选了「PPT 模式」（= 右侧第三栏存在）。null 表示还没确认过：
      // 页面加载 / 切换会话时用它做「是否要恢复/关闭」的判断。
      pptMode: false,
      // 右侧工作台里点了「改主题重搭」：暂时用主题输入形态盖住大纲条。
      pptEditingTopic: false
    },
    busy: false,
    // 全局模型档位（本页头部可直接切换）。后端 /api/v1/model-selection 的真源，
    // 这里只是它的一份本地镜像，用于渲染下拉；读不到就置空、下拉禁用。
    modelSelection: { current: "", options: [] },
    // 拖拽排序时记住"正在拖的是哪一页"（dataTransfer 在部分浏览器读不到）。
    draggingPageId: null
  };

  // 快捷问法。刻意覆盖三类**不同性质的**动作，让"能聊到什么粒度"一眼可见：
  // 取数（要新数据）、改画法/加条件（作用在已有图上）、改大纲（作用在已有汇报上）。
  var QUICK = [
    "最近想复盘一下 Zara，帮我分析看看",
    "先看近 5 个月的纤维混纺和价格带情况，出图直观点",
    "换成折线图看看",
    "只看 400 元以上的价格带",
    "把色彩那页去掉",
    "大纲压到 3 页",
    "帮我串个 4 页左右的 PPT 汇报思路",
    "回退一下"
  ];

  // The 高级参数 drawer maps onto the price-band policy the SQL agent already
  // understands (context.price_band_policy), so no new backend vocabulary.
  var PRICE_STRATEGIES = [
    { value: "rounded_width", label: "自适应（等宽 + 整百/整千取整）" },
    { value: "quantile", label: "分位数（同价不拆分）" },
    { value: "equal_width", label: "等宽" }
  ];
  var TIME_WINDOWS = ["最近30天", "近 6 个月", "近 12 个月", ""];

  // 图表形态。取值与后端 canvas_action_service.ALLOWED_CHART_TYPES 一致。
  var CHART_TYPES = [
    { value: "bar", label: "柱状图" },
    { value: "line", label: "折线图" },
    { value: "area", label: "面积图" },
    { value: "pie", label: "饼图" },
    { value: "ring", label: "圆环图" }
  ];
  // 后端 chart_spec 的历史取值（老配置页用 SVG 自绘，认 column）。
  // ECharts 不认它，会**静默丢掉整个 series**，所以展示前一律归一化。
  var LEGACY_CHART_TYPES = { column: "bar", vertical: "bar" };

  // 幻灯片版式。取值与后端 report_composer / canvas_action_service 的
  // ALLOWED_PAGE_LAYOUTS 一致，用户在大纲和 PPT 预览里换的是同一个字段。
  var PAGE_LAYOUTS = [
    { value: "chart_right", label: "左文右图" },
    { value: "chart_left", label: "左图右文" },
    { value: "chart_only", label: "仅图表" },
    { value: "text_only", label: "仅文案" }
  ];

  function currentChartType(spec) {
    var raw = String((spec && spec.chart_type) || "");
    return LEGACY_CHART_TYPES[raw] || raw;
  }

  function layoutLabel(layout) {
    var hit = PAGE_LAYOUTS.filter(function (item) {
      return item.value === layout;
    })[0];
    return hit ? hit.label : layout || "左文右图";
  }

  function layoutSelect(current, onChange) {
    var select = el("select", "layout-select");
    select.title = "这一页的版式（保存后编译幻灯片时生效）";
    PAGE_LAYOUTS.forEach(function (item) {
      var option = el("option", null, item.label);
      option.value = item.value;
      if ((current || "chart_right") === item.value) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      onChange(select.value);
    });
    return select;
  }

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // ---------------------------------------------------------------- transport

  function api(method, path, body) {
    var options = { method: method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) options.body = JSON.stringify(body);
    return fetch(state.base + path, options).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          var detail = data && data.detail ? data.detail : "请求失败 " + res.status;
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        return data;
      });
    });
  }

  // ------------------------------------------------------------------ helpers

  function toNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    var text = String(value).replace(/[,%]/g, "").trim();
    if (text === "") return null;
    var num = Number(text);
    return Number.isFinite(num) ? num : null;
  }

  function formatCell(value) {
    if (value === null || value === undefined) return "-";
    var num = toNumber(value);
    if (num === null) return String(value);
    if (Math.abs(num) >= 1000) return num.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
    if (Number.isInteger(num)) return String(num);
    return num.toFixed(2);
  }

  function chip(text, cls) {
    return el("span", "badge " + (cls || "gray"), text);
  }

  // 后端按「整类目」截断时要在卡片上如实说明：图上的"趋势"少了一截，
  // 用户必须看得见，不能由着它静默截断（2026-10-12 的 101 个月 → 6 个月事故）。
  function truncationHint(spec) {
    var info = spec && spec.truncation;
    if (!info || !info.omitted_rows) return "";
    var total = spec.total_rows || info.kept_rows + info.omitted_rows;
    return (
      "⚠ 数据按整类目截断：图上 " +
      info.kept_rows +
      " / 全量 " +
      total +
      " 行，类目 " +
      info.kept_categories +
      "/" +
      info.total_categories
    );
  }

  // ------------------------------------------------------------------ drafts
  // 草稿是「绑定到某个服务端值」的：base 变了（说明服务端内容换了，比如重新取过数），
  // 旧草稿立刻失效。否则用户会看着一份过期的输入框，还以为那就是最新结果。

  function draftOf(kind, key, base) {
    var slot = state.ui.drafts[kind + ":" + key];
    if (slot && slot.base === base) return slot.text;
    return base;
  }

  function setDraft(kind, key, base, text) {
    state.ui.drafts[kind + ":" + key] = { base: base, text: text };
  }

  function clearDraft(kind, key) {
    delete state.ui.drafts[kind + ":" + key];
  }

  function multiLineField(className, value, rows) {
    var area = el("textarea", className || "field-area");
    area.rows = rows || 3;
    area.value = value || "";
    return area;
  }

  function linesToList(text) {
    return String(text || "")
      .split("\n")
      .map(function (line) { return line.trim(); })
      .filter(Boolean);
  }

  function notify(text) {
    if (!text) return;
    state.messages.push({ role: "system", content: text });
    renderMessages();
  }

  function reportError(prefix, err) {
    notify(prefix + (err && err.message ? err.message : err));
  }

  // 「改服务端状态」的统一出口：置忙 → 调接口 → 把新的画布收回来 → 失败原因落进对话流。
  // 画布上的四个渲染器（换一批 / SQL 重跑 / 大纲增删改排序 / 预览区直改）都走这里，
  // 否则每个按钮各写一套，忙碌态和报错口径迟早会漂移。
  function mutate(method, path, body, opts) {
    opts = opts || {};
    setBusy(true, opts.busyLabel || "正在处理…");
    return api(method, path, body)
      .then(function (res) {
        if (res && res.canvas) state.canvas = res.canvas;
        if (opts.onDone) opts.onDone(res || {});
        if (!opts.followUp) return res;
        return Promise.resolve(opts.followUp(res || {})).then(function (next) {
          if (next && next.canvas) state.canvas = next.canvas;
          return next || res;
        });
      })
      .then(function (res) {
        if (opts.skipRefresh) {
          renderCanvas();
          return res;
        }
        return refreshDetail();
      })
      .catch(function (err) {
        reportError(opts.errPrefix || "操作未完成：", err);
      })
      .finally(function () {
        setBusy(false);
      });
  }

  /** 大纲是「源」，PPT 预览是它的投影。
   *
   * 任何改动大纲的动作（排序/增删/直改/重跑取数）之后，只要画布上**已经有**预览，
   * 就让它跟着重排一次——否则左边大纲变了、右边预览还是旧的，用户会以为刚才那下
   * 没生效。画布上没有预览就什么都不做：用户没要 PPT，不能凭空给他造一份。
   */
  function recompileIfDeckExists() {
    var items = (state.canvas && state.canvas.items) || [];
    var hasDeck = items.some(function (item) {
      return item.kind === "deck_card";
    });
    if (!hasDeck) return Promise.resolve(null);
    return api("POST", conversationPath("/outline/compile")).then(function (res) {
      // 大纲变了 → 编辑器里那份就是旧的，按最新大纲重载一次（没勾 PPT 模式就不管）。
      if (state.cid && state.ui.pptMode) {
        return openPptEditor(true).then(function () {
          return res;
        });
      }
      return res;
    });
  }

  // ------------------------------------------------- PPT 编辑器（最右侧那一栏）
  //
  // 生成 PPT 不再是"后端画一张 python-pptx 死模板"，而是把大纲 + 真实图表
  // 编成 PPTist 元素级文档，交给 TrainPPTAgent 的画布（可拖拽、可改字、可导出）。
  // 我们只做两件事：给它交接包、收它回写的文字。

  function setCanvasCollapsed(collapsed) {
    state.ui.canvasCollapsed = !!collapsed;
    var pane = $("canvasPane");
    if (pane) pane.classList.toggle("is-collapsed", !!collapsed);
  }

  function setPptMeta(text) {
    var node = $("pptMeta");
    if (node) node.textContent = text || "";
  }

  function pptEditorBase(fallback) {
    // 编辑器地址可覆盖（换端口/换机器时不用改代码）。
    return localStorage.getItem("fz_ppt_editor") || fallback || "http://127.0.0.1:5173/editor";
  }

  function hasDeckCard() {
    var items = (state.canvas && state.canvas.items) || [];
    return items.some(function (item) {
      return item.kind === "deck_card";
    });
  }

  /** 右侧编辑器头部的「导出 .pptx」：PPT 卡不再出现在中间画布里，导出入口跟着搬过来。 */
  function updatePptExportLink() {
    var link = $("pptExportLink");
    if (!link) return;
    var decks = ((state.canvas && state.canvas.items) || []).filter(function (item) {
      return item.kind === "deck_card";
    });
    var deck = decks[decks.length - 1];
    var url = deck && deck.payload && deck.payload.export_url;
    if (!url) {
      link.hidden = true;
      link.removeAttribute("href");
      return;
    }
    link.hidden = false;
    link.href = state.base + url;
  }

  /** 载入（或重载）右侧编辑器的内容。**只管 iframe，不管这一栏开不开。**
   *
   *  `fresh=true` = 按当前大纲重编（「重新载入大纲」）。
   *  开关这一栏一律走 `setPptMode()`——"生成完自动跳过去"已被去掉，
   *  否则用户刚出完 PPT 就被抢走画布，后续想改大纲还得先退出来。
   */
  function openPptEditor(fresh) {
    if (!state.cid) return Promise.resolve(null);
    setPptMeta(fresh ? "正在按最新大纲重建…" : "正在准备编辑器…");
    return api("GET", conversationPath("/deck/handoff") + (fresh ? "?fresh=1" : ""))
      .then(function (handoff) {
        var doc = (handoff && handoff.editor_document) || {};
        var handoffUrl = handoff.handoff_url || state.base + conversationPath("/deck/handoff");
        var base = pptEditorBase(handoff.editor_url);
        var src = base + (base.indexOf("?") >= 0 ? "&" : "?") + "fz_handoff_url=" + encodeURIComponent(handoffUrl);
        var frame = $("pptFrame");
        frame.hidden = false;
        frame.src = src;
        updatePptSetup();
        setPptMeta((doc.slides || []).length + " 页 · 可直接改");
        notify("PPT 编辑器已载入；改完在编辑器里点「同步到分析会话」，再点这边「读回改动」。");
        return handoff;
      })
      .catch(function (err) {
        setPptMeta("载入失败：" + err.message);
        notify("没打开编辑器：" + err.message);
      });
  }

  /** 画布上的大纲卡状态——右侧工作台与中间画布读的是**同一份事实**。 */
  function outlineState() {
    var items = (state.canvas && state.canvas.items) || [];
    var card = null;
    items.forEach(function (item) {
      if (item.kind === "outline_card") card = item;
    });
    var p = (card && card.payload) || {};
    return {
      exists: !!card,
      needsTopic: String(p.status || "") === "needs_topic",
      topic: String(p.topic || ""),
      pages: (p.pages || []).length,
      confirmed: !!p.confirmed,
      pageCount: p.page_count || null
    };
  }

  function setPptSetupStatus(text) {
    var node = $("pptSetupStatus");
    if (node) node.textContent = text || "";
  }

  /** 右侧工作台三种形态：**给主题** / **确认大纲** / **编辑器**。
   *
   * 以前勾上「PPT 模式」但还没出稿时是直接拦回去的（"先在画布点生成 PPT"），
   * 用户看到的是"勾了没反应"。现在这一栏本身就是入口：先给主题，
   * 大纲出来后在同一个地方确认，出稿后才换成编辑器。
   */
  function updatePptSetup() {
    var setup = $("pptSetup");
    var bar = $("pptOutlineBar");
    var frame = $("pptFrame");
    if (!setup || !bar || !frame) return;
    var deck = hasDeckCard();
    var outline = outlineState();
    var editingTopic = !!state.ui.pptEditingTopic;
    var showSetup = !deck && (editingTopic || !outline.exists || outline.needsTopic);
    var showBar = !deck && !showSetup;
    setup.hidden = !showSetup;
    bar.hidden = !showBar;
    frame.hidden = !deck;

    // 「读回改动 / 重新载入大纲」只对编辑器有意义。
    var pull = $("pptSyncPullBtn");
    var reload = $("pptReloadBtn");
    if (pull) pull.hidden = !deck;
    if (reload) reload.hidden = !deck;

    if (showSetup) {
      var input = $("pptTopicInput");
      if (input && !input.value) input.value = outline.topic || "";
      var pages = $("pptPageCount");
      if (pages && !pages.value) pages.value = outline.pageCount || outline.pages || 4;
      setPptMeta(editingTopic ? "改主题" : "先定主题");
      return;
    }
    if (showBar) {
      $("pptOutlineTopic").textContent = outline.topic || "（还没定主题）";
      $("pptOutlineHint").textContent =
        outline.pages + " 页 · " + (outline.confirmed ? "已确认" : "待确认")
        + "。大纲在中间画布，页序/标题/结论都能改；确认之后才出稿。";
      $("pptConfirmBtn").textContent = outline.confirmed ? "按大纲重新生成" : "确认大纲，生成 PPT";
      setPptMeta(outline.pages + " 页 · " + (outline.confirmed ? "已确认" : "待确认"));
    }
  }

  /** 从右侧工作台给主题：走**同一条对话链路**（agent 判配置与方向，后端搭大纲）。
   *
   * 为什么不直接调 `/outline/topic`：那条只按主题 + 已有上下文搭页，不经过 agent
   * 判断"这套业务该用哪套配置、该看哪些维度"。用户要的是"依据上下文 + 数据库关系 +
   * 业务需求"生成，所以这里把主题当成一句话发给 agent —— 和他在聊天框里说完全等价，
   * 只是入口换到右边。会花一次模型调用，和聊天里那句一样。
   */
  function submitPptTopic() {
    var input = $("pptTopicInput");
    var topic = ((input && input.value) || "").trim();
    if (!topic) {
      if (input) input.focus();
      notify("先写主题：这份 PPT 要讲什么？");
      return Promise.resolve(null);
    }
    if (state.busy) return Promise.resolve(null);
    var raw = parseInt(($("pptPageCount") || {}).value, 10);
    var pages = isFinite(raw) ? Math.min(12, Math.max(2, raw)) : 0;
    state.ui.pptEditingTopic = false;
    setPptSetupStatus("正在按主题「" + topic + "」搭大纲（会先判定该用哪套配置）…");
    return send(
      "帮我做一份《" + topic + "》的 PPT" + (pages ? "，" + pages + " 页" : "")
    ).then(function () {
      var outline = outlineState();
      if (outline.exists && !outline.needsTopic) {
        setPptSetupStatus("大纲已生成：" + outline.pages + " 页，确认之后才出稿。");
      } else {
        setPptSetupStatus("这一轮没搭出大纲，原因在左侧对话里。");
      }
    });
  }

  /** 在右侧工作台直接确认大纲 → 出稿 → 载入编辑器。 */
  function confirmPptFromPane() {
    var outline = outlineState();
    if (!outline.topic) {
      notify("先定主题：这份 PPT 要讲什么？");
      return;
    }
    mutate("POST", conversationPath("/outline/confirm"), { topic: outline.topic }, {
      busyLabel: "正在按主题和大纲装配 PPT…",
      errPrefix: "确认失败：",
      onDone: function (res) {
        notify("已按《" + res.topic + "》生成 " + res.slide_count + " 页 PPT。");
        if (state.ui.pptMode) openPptEditor(false);
      }
    });
  }

  /** 「改主题重搭」：把输入框露出来（主题预填当前值），提交后整份大纲按新主题重建。 */
  function changePptTopic() {
    var outline = outlineState();
    state.ui.pptEditingTopic = true;
    var input = $("pptTopicInput");
    if (input) {
      input.value = outline.topic || "";
      input.focus();
    }
    var pages = $("pptPageCount");
    if (pages) pages.value = outline.pageCount || outline.pages || 4;
    updatePptSetup();
  }

  /** 勾选 / 取消「PPT 模式」——右侧第三栏**唯一**的开关入口。
   *
   * 为什么从"生成后自动打开"改成手动勾选：生成完 PPT 往往还要接着在对话里改大纲
   * （换维度、补页、调结论），自动切过去会把画布一起收掉、把编辑器和聊天割裂开。
   * 现在勾上 = 三栏并排（画布保留；嫌挤可再点「收起」），不勾 = 布局完全不变。
   *
   * **没有 PPT 也允许勾**（2026-10-28 用户要求）：这一栏先当工作台——让用户输入主题，
   * 而不是"勾了没反应、还得回画布找按钮"。
   */
  function setPptMode(on, opts) {
    var options = opts || {};
    var want = !!on;
    var pane = $("pptPane");
    var check = $("pptModeCheck");
    var main = $("agentMain");
    if (!pane) return Promise.resolve(null);

    if (want && !state.cid) {
      if (check) check.checked = false;
      if (!options.silent) notify("先新建或打开一个会话，再进 PPT 工作台。");
      return Promise.resolve(null);
    }

    state.ui.pptMode = want;
    if (check) check.checked = want;
    if (main) main.classList.toggle("ppt-open", want);
    pane.hidden = !want;
    if (!want) {
      $("pptFrame").src = "about:blank";
      state.ui.pptEditingTopic = false;
      setPptMeta("");
      // 退出模式时把画布放回来：否则会剩下一条窄条，右边却什么也没有。
      setCanvasCollapsed(false);
      return Promise.resolve(null);
    }
    updatePptExportLink();
    updatePptSetup();

    // 还没有 PPT：这一栏就是"给主题 / 确认大纲"，不去载空白编辑器。
    if (!hasDeckCard()) return Promise.resolve(null);

    // 已经载过就别重载：用户可能正在编辑器里改，重载会把他的改动冲掉。
    var frame = $("pptFrame");
    var loaded = frame && frame.getAttribute("src") && frame.getAttribute("src") !== "about:blank";
    if (!options.force && loaded) return Promise.resolve(null);
    return openPptEditor(!!options.fresh);
  }

  /** 画布重渲染之后校正 PPT 模式（切会话、删掉 deck 卡之后要跟着变）。 */
  function syncPptModeWithCanvas() {
    updatePptExportLink();
    updatePptSetup();
    if (!state.ui.pptMode) return;
    if (!hasDeckCard()) {
      // 没有 deck 就留在"给主题/确认大纲"形态，**不自动退出**：
      // 刚勾上就被关掉，用户会以为这个开关坏了。
      var blank = $("pptFrame");
      if (blank) blank.src = "about:blank";
      return;
    }
    var frame = $("pptFrame");
    var loaded = frame && frame.getAttribute("src") && frame.getAttribute("src") !== "about:blank";
    if (!loaded) openPptEditor(false);
  }

  function conversationPath(suffix) {
    return "/api/v1/conversations/" + state.cid + suffix;
  }

  function artifactPath(card, suffix) {
    return conversationPath("/artifacts/" + card.artifact_id + (suffix || ""));
  }

  function disposeCharts() {
    state.charts.forEach(function (chart) {
      try {
        chart.dispose();
      } catch (err) {
        /* 图表实例可能已被 DOM 移除，忽略 */
      }
    });
    state.charts.clear();
  }

  // -------------------------------------------------------------- chart build

  function looksLikeTime(values) {
    if (!values.length) return false;
    return values.every(function (value) {
      return /^\d{4}([-/年]\d{1,2})?/.test(value) || /^\d{4}-\d{2}/.test(value);
    });
  }

  // 图上最多画多少条系列，多出来的归并成「其他（M 种）」。
  // 注意是**归并**不是丢弃：老代码的 slice(0, 12) 会把第 13 条起静默扔掉，
  // 堆叠柱因此少算一截却毫无提示（实测 16 种纤维只画了 12 种）。
  var MAX_SERIES = 12;

  function buildOption(spec) {
    var rows = (spec.rows || []).filter(function (row) {
      return row && typeof row === "object";
    });
    var xf = spec.x_field;
    var yf = spec.y_field;
    var sf = spec.series_field;
    var type = String(spec.chart_type || "column");
    if (!rows.length || !yf) return null;

    // 口径：全链路的百分数都是「0–100 的数值」（SQL 里就是 ROUND(x * 100, 2) 算出来的），
    // 不是 0–1 的小数。这里以前又乘了一次 100，把整条 y 轴放大 100 倍 ——
    // 实测 2017-10 的堆叠和 79202.5 被显示成 "8000000%"。格式化只补 "%"，不做换算。
    var isPercent = spec.value_format === "percent";
    var axisFormatter = function (value) {
      if (value === null || value === undefined) return "";
      return isPercent ? formatCell(value) + "%" : formatCell(value);
    };
    var tooltipFormatter = function (params) {
      var list = Array.isArray(params) ? params : [params];
      var head = list.length ? list[0].axisValueLabel || list[0].name : "";
      var lines = list.map(function (item) {
        var raw = item.value;
        var shown = formatCell(raw);
        if (isPercent) {
          var num = toNumber(raw);
          shown = num === null ? "-" : num.toFixed(2) + "%";
        }
        return item.marker + " " + item.seriesName + "：<b>" + shown + "</b>";
      });
      return head + "<br/>" + lines.join("<br/>");
    };

    if (type === "pie" || type === "ring") {
      var pieData = rows
        .map(function (row) {
          return { name: String(row[xf] === undefined ? "-" : row[xf]), value: toNumber(row[yf]) };
        })
        .filter(function (item) {
          return item.value !== null;
        });
      return {
        tooltip: { trigger: "item", formatter: "{b}：{d}%" },
        legend: { type: "scroll", bottom: 0, textStyle: { color: "#9bb0c8", fontSize: 11 } },
        color: palette(),
        series: [
          {
            type: "pie",
            radius: type === "ring" ? ["45%", "70%"] : "65%",
            center: ["50%", "45%"],
            data: pieData,
            label: { color: "#eef4ff", fontSize: 10 },
            labelLine: { lineStyle: { color: "#2e4f73" } }
          }
        ]
      };
    }

    var categories = [];
    rows.forEach(function (row) {
      var label = String(row[xf] === undefined ? "-" : row[xf]);
      if (categories.indexOf(label) < 0) categories.push(label);
    });
    if (looksLikeTime(categories)) {
      categories.sort(function (a, b) {
        return a < b ? -1 : a > b ? 1 : 0;
      });
    }
    // 只有 1~2 个类目时**必须画点**：折线用 showSymbol:false 时，单点会被画成
    // 一条看不见的线 —— 图表区看起来是空的（实测 2026-10-19「最近一个月」的
    // 淘宝数据只有一个时间点，用户以为没出数）。
    var sparse = categories.length <= 2;

    var series = [];
    // 后端 chart_spec 的默认类型是 "column"（老配置页用 SVG 自绘，"column"/"bar" 都认），
    // 但 ECharts 只有 "bar" —— 传 "column" 进去它会把整个 series 静默丢掉，
    // 结果只剩一条 x 轴类目、没有 y 轴也没有柱子（"图出不来"的真凶）。
    var chartType =
      type === "area" ? "line" :
      type === "column" || type === "vertical" ? "bar" :
      type === "bar" ? "bar" : type;
    if (chartType === "radar" || chartType === "scatter") chartType = "line";

    if (sf) {
      // 一次 O(rows) 建索引。以前每个 (类目 × 系列) 都要 rows.find 扫全表，
      // 行数上限提高后（60 → 3000）会退化成 O(n²)。
      var buckets = new Map();
      var allNames = [];
      var totals = new Map();
      rows.forEach(function (row) {
        var category = String(row[xf] === undefined ? "-" : row[xf]);
        var name = String(row[sf] === undefined ? "-" : row[sf]);
        var value = toNumber(row[yf]);
        var bucket = buckets.get(category);
        if (!bucket) {
          bucket = new Map();
          buckets.set(category, bucket);
        }
        if (!bucket.has(name)) bucket.set(name, value);
        if (!totals.has(name)) {
          totals.set(name, 0);
          allNames.push(name);
        }
        totals.set(name, totals.get(name) + Math.abs(value || 0));
      });

      // 系列上限：超出就按「合计值」取前 MAX_SERIES 条，其余**显式归并**成一条
      // 「其他（M 种）」。以前是 names.slice(0, 12) —— 第 13 条之后被**静默丢掉**，
      // 堆叠柱因此少算、且没有任何提示（实测 16 种纤维只画了 12 种）。
      // 归并值是「该类目下其余系列的真实合计」，不是估算，图例也直接写明条数。
      var ranked = allNames.slice().sort(function (a, b) {
        var diff = totals.get(b) - totals.get(a);
        return diff !== 0 ? diff : a < b ? -1 : a > b ? 1 : 0;
      });
      var drawnNames = ranked.slice(0, MAX_SERIES);
      var mergedNames = ranked.slice(MAX_SERIES);

      series = drawnNames.map(function (name) {
        return {
          name: name,
          type: chartType,
          // 后端对 column/area 明确要求堆叠（chart_spec_service），照它办。
          stack: spec.stack ? "total" : undefined,
          areaStyle: type === "area" ? { opacity: 0.55 } : undefined,
          smooth: chartType === "line",
          showSymbol: sparse,
          emphasis: { focus: "series" },
          data: categories.map(function (category) {
            var bucket = buckets.get(category);
            var value = bucket ? bucket.get(name) : undefined;
            return value === undefined ? null : value;
          })
        };
      });

      if (mergedNames.length) {
        series.push({
          name: "其他（" + mergedNames.length + " 种）",
          type: chartType,
          stack: spec.stack ? "total" : undefined,
          areaStyle: type === "area" ? { opacity: 0.55 } : undefined,
          smooth: chartType === "line",
          showSymbol: sparse,
          emphasis: { focus: "series" },
          data: categories.map(function (category) {
            var bucket = buckets.get(category);
            if (!bucket) return null;
            var sum = null;
            mergedNames.forEach(function (name) {
              var value = bucket.has(name) ? bucket.get(name) : null;
              if (value !== null && value !== undefined) {
                sum = (sum === null ? 0 : sum) + value;
              }
            });
            return sum;
          })
        });
      }
    } else {
      var single = new Map();
      rows.forEach(function (row) {
        var category = String(row[xf] === undefined ? "-" : row[xf]);
        if (!single.has(category)) single.set(category, toNumber(row[yf]));
      });
      series = [
        {
          name: yf,
          type: chartType,
          smooth: chartType === "line",
          areaStyle: type === "area" ? {} : undefined,
          showSymbol: sparse,
          data: categories.map(function (category) {
            return single.has(category) ? single.get(category) : null;
          })
        }
      ];
    }

    return {
      tooltip: { trigger: "axis", formatter: tooltipFormatter },
      legend: sf
        ? { type: "scroll", bottom: 0, textStyle: { color: "#9bb0c8", fontSize: 11 } }
        : { show: false },
      grid: { left: 52, right: 18, top: 24, bottom: sf ? 52 : 28 },
      color: palette(),
      xAxis: {
        type: "category",
        data: categories,
        axisLabel: {
          color: "#9bb0c8",
          fontSize: 10,
          rotate: categories.length > 10 ? 32 : 0,
          hideOverlap: true
        },
        axisLine: { lineStyle: { color: "#2e4f73" } }
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#9bb0c8", fontSize: 10, formatter: axisFormatter },
        splitLine: { lineStyle: { color: "rgba(46,79,115,0.45)" } }
      },
      series: series
    };
  }

  function palette() {
    return ["#2d8cff", "#40d4c8", "#f0b429", "#ef6f6c", "#8f7bf0", "#4fc3f7", "#9ccc65", "#ff9f6e"];
  }

  function mountChart(holder, spec, key) {
    if (!window.echarts) {
      holder.appendChild(el("div", "canvas-empty", "图表库未加载，请检查 vendor/echarts.min.js"));
      return;
    }
    var option = buildOption(spec);
    if (!option) {
      holder.appendChild(el("div", "canvas-empty", "该页没有可绘制的数值列"));
      return;
    }
    var chart = window.echarts.init(holder, null, { renderer: "canvas" });
    chart.setOption(option);
    state.charts.set(key, chart);
  }

  // ------------------------------------------------------------------- chat

  function renderMessages() {
    var box = $("messages");
    box.innerHTML = "";
    if (!state.messages.length) {
      box.appendChild(el("div", "canvas-empty", "还没有对话。试试输入“最近想复盘一下 Zara，帮我分析看看”。"));
      return;
    }
    state.messages.forEach(function (message) {
      var row = el("div", "msg " + message.role);
      row.appendChild(el("div", "role", message.role === "user" ? "你" : message.role === "assistant" ? "分析助手" : "系统"));
      row.appendChild(el("div", "bubble", message.content || ""));
      // 「这一轮用了哪套配置」逐轮钉在对应的回答下面。头部胶囊只反映**最后一轮**，
      // 翻历史时得能一眼看出每句调了哪套（或为什么没加载），否则上下滚两下就分不清了。
      if (message.role === "assistant") {
        var configChip = configChipFor(message.turn_config);
        if (configChip) row.appendChild(configChip);
      }
      if (message.artifact_refs && message.artifact_refs.length) {
        var refs = el("div", "refs");
        message.artifact_refs.forEach(function (ref, index) {
          refs.appendChild(el("span", "ref-pill", "已更新画布产物 #" + (index + 1)));
        });
        row.appendChild(refs);
      }
      box.appendChild(row);
    });
    box.scrollTop = box.scrollHeight;
  }

  /** 把一条 assistant 消息的 turn_config 渲染成一枚只读小胶囊。
   *
   * 取值口径与头部 `updateSceneChip` 完全一致：有配置就报配置名，没配置就照 note
   * 说清原因（"本轮不涉及配置" / "没确定是哪一套" / "服务连不上"），**不统一成一句话**。
   * 没有 turn_config 的历史消息（本次改动之前产生的）直接不渲染——不假装有值。
   */
  function configChipFor(tc) {
    if (!tc || typeof tc !== "object" || !tc.source) return null;
    var wrap = el("div", "msg-config");
    wrap.appendChild(el("span", "msg-config-label", "本轮配置"));
    var names = (tc.names || []).filter(function (name) {
      return !!name;
    });
    if (names.length) {
      wrap.appendChild(el("b", "msg-config-value", names.join(" + ")));
      wrap.appendChild(
        el("span", "dim-hint", tc.merged ? "（合并 " + names.length + " 套）" : "（按需加载）")
      );
      wrap.title = "config_ids：" + (tc.config_ids || []).join(", ");
      return wrap;
    }
    wrap.appendChild(el("b", "msg-config-value none", "未加载"));
    wrap.appendChild(el("span", "dim-hint", tc.note ? "（" + tc.note + "）" : ""));
    wrap.title = tc.note || "本轮未加载配置";
    return wrap;
  }

  function renderQuickChips() {
    var box = $("quickChips");
    box.innerHTML = "";
    QUICK.forEach(function (text) {
      var button = el("button", "quick-chip", text);
      button.type = "button";
      button.addEventListener("click", function () {
        $("input").value = text;
        $("input").focus();
      });
      box.appendChild(button);
    });
  }

  function setBusy(busy, label) {
    state.busy = busy;
    $("sendBtn").disabled = busy;
    $("typing").hidden = !busy;
    if (label) $("typingText").textContent = label;
    // A turn can legitimately take minutes (LLM intent + real MySQL取数 + PPT).
    // Say so instead of looking frozen; cleared with the busy flag.
    if (state.busyTimer) {
      clearTimeout(state.busyTimer);
      state.busyTimer = null;
    }
    if (busy) {
      state.busyTimer = setTimeout(function () {
        state.busyTimer = null;
        if (state.busy) $("typingText").textContent = "仍在处理（模型判定 + 真实取数，请稍候）…";
      }, 15000);
    }
  }

  function send(text, keys) {
    if (state.busy) return Promise.resolve(null);
    var content = (text || "").trim();
    if (!content && !(keys && keys.length)) return Promise.resolve(null);
    if (!state.cid) {
      return createConversation().then(function () {
        return send(content, keys);
      });
    }
    $("input").value = "";
    setBusy(true, keys && keys.length ? "正在取数并出图（约 15 秒）…" : "正在识别意图…");

    var localUser = { role: "user", content: keys && keys.length ? content + "（已选维度：" + keys.join(", ") + "）" : content };
    state.messages.push(localUser);
    renderMessages();

    return api("POST", "/api/v1/conversations/" + state.cid + "/messages", {
      text: content || "看这些维度",
      selected_dimension_keys: keys && keys.length ? keys : null
    })
      .then(function (res) {
        state.canvas = res.canvas;
        state.selected.clear();
        return refreshDetail();
      })
      .catch(function (err) {
        state.messages.push({ role: "system", content: "本次请求失败：" + err.message });
        renderMessages();
      })
      .finally(function () {
        setBusy(false);
      });
  }

  // ------------------------------------------------------------------ canvas

  function renderCanvas() {
    var box = $("canvas");
    // Clearing the box resets scrollTop, which used to yank the canvas back to
    // the top on every tab switch / drawer toggle. Remember and restore it.
    var keepScroll = box.scrollTop;
    disposeCharts();
    box.innerHTML = "";
    var canvas = state.canvas;
    if (!canvas || !canvas.items || !canvas.items.length) {
      box.appendChild(
        el("div", "canvas-empty", "画布还是空的。开始一段对话后，维度卡片、图表、汇报大纲和 PPT 预览会依次出现在这里。")
      );
      $("canvasMeta").textContent = "";
      syncPptModeWithCanvas();
      return;
    }
    // 中间画布只管「分析过程」（维度/数据卡/大纲）；成稿的 PPT 只在最右侧那一栏。
    // 同一份稿子两处都显示时，改哪边都像没生效，所以这里把 deck 卡过滤掉。
    var items = (canvas.items || []).filter(function (item) {
      return item.kind !== "deck_card";
    });
    $("canvasMeta").textContent = "共 " + items.length + " 个产物";
    items.forEach(function (card, index) {
      var renderer = renderers[card.kind];
      var node = renderer ? renderer(card) : defaultCard(card);
      node.style.animationDelay = Math.min(index * 40, 200) + "ms";
      box.appendChild(node);
    });
    if (!items.length) {
      box.appendChild(
        el(
          "div",
          "canvas-empty",
          "PPT 已在最右侧；勾选顶部「PPT 模式」可以打开编辑，这里只放分析过程的产物。"
        )
      );
    }
    box.scrollTop = keepScroll;
    revealPendingCheckpoint(items);
    // 切会话 / 删卡片之后，右侧那一栏要跟着画布变：没有 deck 就自动退出 PPT 模式。
    syncPptModeWithCanvas();
  }

  // A confirmation / clarification card is useless if it lands below the fold
  // of a long canvas. When such a card appears (the canvas set changed), bring
  // it into view so its options are immediately reachable. Guarded so test
  // harnesses without layout (jsdom) do not blow up.
  function revealPendingCheckpoint(items) {
    var sig = items
      .map(function (card) { return card.artifact_id + ":" + (card.status || ""); })
      .join("|");
    if (sig === state.ui.canvasSig) return;
    state.ui.canvasSig = sig;

    var pending = null;
    for (var i = items.length - 1; i >= 0; i -= 1) {
      var status = items[i].status || "";
      if (status === "needs_confirmation" || status === "needs_clarification") {
        pending = items[i];
        break;
      }
    }
    if (!pending) return;

    requestAnimationFrame(function () {
      var node = document.querySelector(
        '.card[data-artifact-id="' + pending.artifact_id + '"]'
      );
      if (node && typeof node.scrollIntoView === "function") {
        node.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });
  }

  function cardShell(card, kindLabel, badge, actions) {
    var node = el("article", "card");
    // Expose identity on the DOM so a card can be addressed directly when
    // debugging or driving the page from a test harness.
    node.dataset.artifactId = card.artifact_id;
    node.dataset.kind = card.kind;
    var head = el("div", "card-head");
    head.appendChild(el("span", "kind-tag", kindLabel));
    var titles = el("div", "titles");
    titles.appendChild(el("h3", null, card.title || "未命名产物"));
    if (card.subtitle) titles.appendChild(el("p", null, card.subtitle));
    head.appendChild(titles);
    if (badge) head.appendChild(badge);
    head.appendChild(el("span", "spacer"));
    if (actions) actions.forEach(function (action) { head.appendChild(action); });
    node.appendChild(head);
    return node;
  }

  function defaultCard(card) {
    var node = cardShell(card, card.kind, null, null);
    node.appendChild(el("div", "card-body", "暂不支持渲染 " + card.kind));
    return node;
  }

  function labeled(labelText, control, hint) {
    var wrap = el("div", "field-row");
    wrap.appendChild(el("label", "field-label", labelText));
    wrap.appendChild(control);
    if (hint) wrap.appendChild(el("div", "dim-hint", hint));
    return wrap;
  }

  function clearDropMarks() {
    Array.prototype.forEach.call(
      document.querySelectorAll(".outline-page.drop-target"),
      function (node) { node.classList.remove("drop-target"); }
    );
  }

  /** 大纲里的一页：可拖拽排序、可直接改标题/问题/要点/结论/版式、可单独重跑取数。 */
  function outlinePageRow(card, page) {
    var pageId = page.page_id;
    var row = el("div", "outline-page");
    row.dataset.pageId = pageId;
    row.draggable = true;

    var handle = el("div", "order drag-handle", page.order);
    handle.title = "拖动调整页序";
    row.appendChild(handle);

    // --- 拖拽排序 ---------------------------------------------------------
    // dataTransfer 在部分浏览器/环境下读不到，所以另存一份"正在拖的是谁"。
    row.addEventListener("dragstart", function (event) {
      state.draggingPageId = pageId;
      row.classList.add("dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try {
          event.dataTransfer.setData("text/plain", pageId);
        } catch (err) {
          /* 个别浏览器只允许在 dragstart 里设一次数据，失败了也不影响拖拽本身 */
        }
      }
    });
    row.addEventListener("dragend", function () {
      state.draggingPageId = null;
      row.classList.remove("dragging");
      clearDropMarks();
    });
    row.addEventListener("dragover", function (event) {
      if (!state.draggingPageId || state.draggingPageId === pageId) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      row.classList.add("drop-target");
    });
    row.addEventListener("dragleave", function () {
      row.classList.remove("drop-target");
    });
    row.addEventListener("drop", function (event) {
      event.preventDefault();
      row.classList.remove("drop-target");
      var movingId =
        state.draggingPageId || (event.dataTransfer && event.dataTransfer.getData("text/plain"));
      if (!movingId || movingId === pageId) return;
      mutate("POST", conversationPath("/outline/pages/" + movingId + "/move"), { to_order: page.order }, {
        busyLabel: "正在调整页序…",
        errPrefix: "调整页序失败：",
        skipRefresh: true,
        followUp: recompileIfDeckExists
      });
    });

    // --- 标题与目标 -------------------------------------------------------
    // 输入框读的是「草稿」：任何一次 renderCanvas 都会重建 DOM，没有草稿的话
    // 用户敲进去的字会在拖一下排序之后凭空消失。草稿绑定服务端值，服务端一变即失效。
    var main = el("div", "page-main");
    var baseTitle = String(page.title || "");
    var titleInput = el("input", "page-title");
    titleInput.value = draftOf("page-title", pageId, baseTitle);
    titleInput.placeholder = "这一页的标题";
    titleInput.addEventListener("input", function () {
      setDraft("page-title", pageId, baseTitle, titleInput.value);
    });
    titleInput.addEventListener("change", function () {
      var text = titleInput.value.trim();
      if (!text || text === baseTitle) return;
      patchPage(pageId, { title: text });
    });
    main.appendChild(titleInput);

    var baseObjective = String(page.objective || "");
    var objectiveInput = el("input", "page-objective-input");
    objectiveInput.value = draftOf("page-objective", pageId, baseObjective);
    objectiveInput.placeholder = "这一页要回答的问题（用于约束内容，不占幻灯片版面）";
    objectiveInput.addEventListener("input", function () {
      setDraft("page-objective", pageId, baseObjective, objectiveInput.value);
    });
    objectiveInput.addEventListener("change", function () {
      var text = objectiveInput.value.trim();
      if (text === baseObjective) return;
      patchPage(pageId, { objective: text });
    });
    main.appendChild(objectiveInput);

    // --- 内容编辑区（展开才显示） -----------------------------------------
    var expanded = !!state.ui.pageEditor[pageId];
    if (expanded) {
      var baseFindings = (page.findings || []).join("\n");
      var findingsArea = multiLineField(
        "field-area",
        draftOf("page-findings", pageId, baseFindings),
        3
      );
      findingsArea.placeholder = "每行一条要点。留空则沿用这张图自动生成的洞察。";
      findingsArea.addEventListener("input", function () {
        setDraft("page-findings", pageId, baseFindings, findingsArea.value);
      });
      findingsArea.addEventListener("change", function () {
        var list = linesToList(findingsArea.value);
        if (list.join("\n") === baseFindings) return;
        patchPage(pageId, { findings: list });
      });
      main.appendChild(labeled("本页要点（每行一条）", findingsArea));

      var baseNarrative = String(page.narrative || "");
      var narrativeArea = multiLineField(
        "field-area",
        draftOf("page-narrative", pageId, baseNarrative),
        3
      );
      narrativeArea.placeholder = "这一页要讲的那句话。留空则沿用图表卡的业务解读。";
      narrativeArea.addEventListener("input", function () {
        setDraft("page-narrative", pageId, baseNarrative, narrativeArea.value);
      });
      narrativeArea.addEventListener("change", function () {
        var text = narrativeArea.value.trim();
        if (text === baseNarrative) return;
        patchPage(pageId, { narrative: text });
      });
      main.appendChild(labeled("业务解读 / 结论", narrativeArea));
    }

    row.appendChild(main);

    // --- 状态与操作 -------------------------------------------------------
    var actions = el("div", "page-actions");
    if (page.bound_artifact_id) {
      // 有编号就带上：用户对着大纲说"第 2 页用图 3"时，两边指的是同一个东西。
      actions.appendChild(
        chip(
          page.bound_chart_no
            ? "图" + page.bound_chart_no + "：" + (page.bound_chart_title || "图表")
            : "已绑定：" + (page.bound_chart_title || "图表"),
          "ok"
        )
      );
    } else if (page.kind === "text") {
      actions.appendChild(chip("文案页", "gray"));
    } else {
      actions.appendChild(chip("待取数", "warn"));
    }

    var layout = layoutSelect(page.layout, function (value) {
      if (value === String(page.layout || "")) return;
      patchPage(pageId, { layout: value });
    });
    layout.disabled = page.kind !== "chart";
    if (layout.disabled) layout.title = "文案页固定整页文案，版式不适用";
    actions.appendChild(layout);

    var edit = el("button", "ghost", expanded ? "收起内容" : "编辑内容");
    edit.type = "button";
    edit.addEventListener("click", function () {
      state.ui.pageEditor[pageId] = !state.ui.pageEditor[pageId];
      renderCanvas();
    });
    actions.appendChild(edit);

    if (page.kind === "chart") {
      var regen = el("button", "ghost", "重新取数");
      regen.type = "button";
      regen.title = "只重跑这一页的数据，其他页原样不动";
      regen.addEventListener("click", function () {
        mutate("POST", conversationPath("/outline/pages/" + pageId + "/regenerate"), {}, {
          busyLabel: "正在重跑这一页的取数…",
          errPrefix: "重新取数未完成：",
          skipRefresh: true,
          onDone: function (res) {
            notify(res.message || "这一页已重新取数。");
          }
        });
      });
      actions.appendChild(regen);
    }

    var del = el("button", "ghost", "删除");
    del.type = "button";
    del.addEventListener("click", function () {
      patchPage(pageId, { delete: true });
    });
    actions.appendChild(del);

    row.appendChild(actions);
    return row;
  }

  /** 幻灯片对应的大纲页 id。
   *
   * 编译时把 page_id 写在 task_id 与 lineage_summary.report_page_id 上，
   * 这里读回来才能让"在预览区改的字"知道该落到哪一页——否则只能猜。
   */
  function slidePageId(slide) {
    var lineage = (slide && slide.lineage_summary) || {};
    return String(lineage.report_page_id || slide.task_id || "");
  }

  function slideLayout(slide) {
    var lineage = (slide && slide.lineage_summary) || {};
    return String(lineage.layout || "chart_right");
  }

  /** 「先定主题」：还没主题、也还没页。TrainPPT 的第一步就是这里。 */
  function outlineTopicCard(card, payload) {
    var draftKey = "outline-topic";
    var input = el("textarea", "topic-input");
    input.rows = 2;
    input.value = draftOf(draftKey, card.artifact_id, "");
    input.placeholder = "这份 PPT 要讲什么？例如：ZARA 2026 春夏竞品与价格复盘";
    input.title = "主题就是这份 PPT 的标题，也是出稿依据";
    input.addEventListener("input", function () {
      setDraft(draftKey, card.artifact_id, "", input.value);
      sync();
    });

    var submit = el("button", null, "生成大纲");
    submit.type = "button";
    submit.title = "按主题把会话里已有的图表和结论串成大纲；大纲确认之后才出稿";
    function sync() {
      submit.disabled = !(input.value || "").trim();
    }
    function send() {
      var topic = (input.value || "").trim();
      if (!topic) {
        input.focus();
        notify("先写主题：这份 PPT 要讲什么？");
        return;
      }
      mutate("POST", conversationPath("/outline/topic"), {
        topic: topic,
        page_count: payload.page_count || null
      }, {
        busyLabel: "正在按主题搭大纲…",
        errPrefix: "生成大纲失败：",
        onDone: function (res) {
          notify(
            "已按《" + res.topic + "》搭了 " + res.page_count + " 页大纲，"
              + "确认大纲之后才出稿。"
          );
        }
      });
    }
    submit.addEventListener("click", send);
    input.addEventListener("keydown", function (event) {
      // 单行回车提交，Shift+Enter 换行——和大多数输入框的习惯一致。
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    sync();

    var node = cardShell(card, "汇报主题", chip("待填写", "warn"), []);
    var body = el("div", "card-body");
    body.appendChild(
      el(
        "div",
        "dim-hint",
        "顺序是「主题 → 大纲 → 确认 → 出稿」。主题会作为这份 PPT 的标题和出稿依据；"
          + "大纲只引用这个会话里已经有的图表和结论，不会凭空补数。"
      )
    );
    body.appendChild(input);
    var actions = el("div", "topic-actions");
    actions.appendChild(submit);
    body.appendChild(actions);
    node.appendChild(body);
    return node;
  }

  var renderers = {
    dimension_card: function (card) {
      var options = (card.payload && card.payload.options) || [];
      var liveKeys = options.map(function (option) { return option.dimension_key; });

      // 「换一批」不是重新编几个说法，而是把场景里**还没看过**的那些方向换上来：
      // 候选来自后端按真实字段推导出的全部主题，看完一轮它就会如实说"没有了"。
      var rotate = el("button", "ghost", "换一批");
      rotate.type = "button";
      rotate.title = "把还没看过的分析方向换上来";
      rotate.addEventListener("click", function () {
        mutate(
          "POST",
          conversationPath("/dimensions/rotate"),
          { exclude_keys: liveKeys },
          {
            busyLabel: "正在换一批分析切入点…",
            errPrefix: "换一批未完成：",
            onDone: function () {
              // 被换下去的方向可能还挂着勾选，留着会指向卡片上已不存在的选项。
              liveKeys.forEach(function (key) { state.selected.delete(key); });
            }
          }
        );
      });

      var node = cardShell(card, "分析维度", chip(options.length + " 个切入点", "gray"), [rotate]);
      var body = el("div", "card-body");
      var grid = el("div", "dim-grid");

      if (!options.length) {
        body.appendChild(el("div", "canvas-empty", "这个场景暂时没有可推荐的分析切入点，可以点右上角「换一批」再试。"));
      }

      options.forEach(function (option) {
        var key = option.dimension_key;
        var button = el("button", "dim-option" + (state.selected.has(key) ? " selected" : ""));
        button.type = "button";
        var title = el("div", "dim-title");
        title.appendChild(el("span", "tick", "✓"));
        title.appendChild(el("span", null, option.title || key));
        button.appendChild(title);
        if (option.angle) button.appendChild(el("div", "dim-angle", option.angle));
        if (option.reason) button.appendChild(el("div", "dim-reason", option.reason));
        var fields = (option.dimensions || []).slice(0, 4).join(" · ");
        if (fields) button.appendChild(el("div", "dim-fields", "维度：" + fields));
        if (option.metrics && option.metrics.length) {
          button.appendChild(el("div", "dim-fields", "指标：" + option.metrics.join(" · ")));
        }
        button.addEventListener("click", function () {
          if (state.selected.has(key)) state.selected.delete(key);
          else state.selected.add(key);
          renderCanvas();
        });
        grid.appendChild(button);
      });

      body.appendChild(grid);
      var actions = el("div", "dim-actions");
      var run = el("button", null, "看选中的维度");
      run.type = "button";
      var hint = el("span", "dim-hint", "点卡片多选，或直接在左侧输入");
      actions.appendChild(run);
      actions.appendChild(hint);
      run.addEventListener("click", function () {
        var keys = Array.from(state.selected);
        if (!keys.length) {
          hint.textContent = "先点选至少一个维度";
          return;
        }
        var titles = options
          .filter(function (option) { return keys.indexOf(option.dimension_key) >= 0; })
          .map(function (option) { return option.title; });
        send("先看这几个维度：" + titles.join("、") + "，出图直观点。", keys);
      });
      body.appendChild(actions);
      node.appendChild(body);
      return node;
    },

    chart_card: function (card) {
      var failed = card.status === "failed";
      var badge = failed ? chip("取数失败", "err") : chip("已就绪", "ok");
      var holderId = "chart_" + card.artifact_id;
      var body = el("div", "card-body");
      var spec = (card.payload && card.payload.spec) || {};

      var head = el("div", "card-head");
      head.appendChild(el("span", "kind-tag", "数据图表"));
      // 会话内编号：让用户能说"把图2 换成饼图"。号是后端发的、只增不改，
      // 删掉一张不会让后面的图集体换号。
      if (card.chart_no) {
        var noTag = el("span", "chart-no", "图" + card.chart_no);
        noTag.title = "本会话的第 " + card.chart_no + " 张图；在聊天里直接说「图" + card.chart_no + "」就能指代它";
        head.appendChild(noTag);
      }
      var titles = el("div", "titles");
      titles.appendChild(el("h3", null, card.title));
      if (card.subtitle) titles.appendChild(el("p", null, card.subtitle));
      var cutHint = truncationHint(spec);
      if (cutHint) titles.appendChild(el("p", "cut-hint", cutHint));
      head.appendChild(titles);
      head.appendChild(badge);
      head.appendChild(el("span", "spacer"));

      // Offered on failed cards too: a rejected SQL is exactly when changing
      // the price-band strategy (or the window) and re-running is worth a try.
      var drawerToggle = el(
        "button",
        "ghost" + (state.ui.drawer[card.artifact_id] ? " active" : ""),
        "高级参数"
      );
      drawerToggle.type = "button";
      drawerToggle.addEventListener("click", function () {
        state.ui.drawer[card.artifact_id] = !state.ui.drawer[card.artifact_id];
        renderCanvas();
      });
      head.appendChild(drawerToggle);

      if (!failed) {
        var tabs = el("div", "tabs");
        var currentTab = state.ui.chartTab[card.artifact_id] || "chart";
        ["chart", "table"].forEach(function (name) {
          var tab = el("button", "tab-btn" + (currentTab === name ? " active" : ""), name === "chart" ? "图表" : "数据表");
          tab.type = "button";
          tab.addEventListener("click", function () {
            state.ui.chartTab[card.artifact_id] = name;
            renderCanvas();
          });
          tabs.appendChild(tab);
        });
        head.appendChild(tabs);

        // 只列 ECharts 真认得的形态。老契约里的 column 会**静默丢整条 series**
        // （图变空白），所以展示时归一化成 bar，不再把它放进下拉。
        var select = el("select", "chart-type-select");
        select.title = "换画法，不重跑数据（不满意可以点「回退」）";
        var currentType = currentChartType(spec);
        CHART_TYPES.forEach(function (item) {
          var option = el("option", null, item.label);
          option.value = item.value;
          if (currentType === item.value) option.selected = true;
          select.appendChild(option);
        });
        select.addEventListener("change", function () {
          var chosen = select.value;
          if (chosen === currentType) return;
          mutate("POST", artifactPath(card, "/revise"), { chart_type: chosen }, {
            busyLabel: "正在换画法…",
            errPrefix: "切换图表形态失败："
          });
        });
        head.appendChild(select);

        var rollback = el("button", "ghost", "回退");
        rollback.type = "button";
        rollback.addEventListener("click", function () {
          mutate("POST", artifactPath(card, "/rollback"), {}, {
            busyLabel: "正在回退到上一个版本…",
            errPrefix: "回退失败："
          });
        });
        head.appendChild(rollback);
      }
      var node = cardShell(card, "数据图表", null, null);
      node.innerHTML = "";
      node.appendChild(head);

      if (failed) {
        body.appendChild(
          el("div", "error-box", (card.payload && card.payload.error) || "该维度本次未取到数据。")
        );
        var detail = (card.payload && card.payload.error_detail) || "";
        if (detail && detail !== (card.payload.error || "")) {
          var fold = document.createElement("details");
          fold.className = "error-detail";
          var foldHead = document.createElement("summary");
          foldHead.textContent = "查看完整报错与建议";
          fold.appendChild(foldHead);
          fold.appendChild(el("div", "error-detail-body", detail));
          body.appendChild(fold);
        }
        body.appendChild(appliedFilters(card));
        body.appendChild(sqlEditor(card));
        if (state.ui.drawer[card.artifact_id]) {
          body.appendChild(paramDrawer(card));
        }
        node.appendChild(body);
        return node;
      }

      var currentTabName = state.ui.chartTab[card.artifact_id] || "chart";
      if (currentTabName === "chart") {
        var holder = el("div", "chart-holder");
        holder.id = holderId;
        body.appendChild(holder);
        requestAnimationFrame(function () {
          var target = document.getElementById(holderId);
          if (target) mountChart(target, spec, card.artifact_id);
        });
      } else {
        body.appendChild(dataTable(spec));
      }

      body.appendChild(appliedFilters(card));

      var findings = (spec.findings && spec.findings.length ? spec.findings : card.payload.findings) || [];
      if (findings.length || (card.payload && card.payload.narrative)) {
        var block = el("div", "findings");
        block.appendChild(el("h4", null, "数据洞察"));
        if (findings.length) {
          var list = el("ul");
          findings.forEach(function (item) {
            list.appendChild(el("li", null, item));
          });
          block.appendChild(list);
        }
        if (card.payload.narrative) {
          block.appendChild(el("div", "narrative", card.payload.narrative));
        }
        body.appendChild(block);
      }

      if (state.ui.drawer[card.artifact_id]) {
        body.appendChild(paramDrawer(card));
      }

      body.appendChild(sqlEditor(card));
      node.appendChild(body);
      return node;
    },

    resolution_card: function (card) {
      var payload = card.payload || {};
      var options = payload.options || [];
      var chosen = payload.chosen || "";
      var settled = card.status === "ready" || payload.status === "resolved";
      var badge = settled ? chip("已确认", "ok") : chip("待你确认", "warn");
      var node = cardShell(card, "待确认", badge, null);
      var body = el("div", "card-body");
      body.appendChild(
        el(
          "div",
          "dim-hint",
          "品牌「" + (payload.term || "") + "」在标准品牌字典里命中 " + options.length +
            " 个候选。口径不同结果差很多，选定后我立刻取数。"
        )
      );

      var list = el("div", "pick-list");
      options.forEach(function (option) {
        var isChosen = chosen === option.value;
        var button = el("button", "pick-option" + (isChosen ? " selected" : ""));
        button.type = "button";
        button.appendChild(el("span", "pick-value", option.value));
        button.appendChild(el("span", "pick-count", (option.count || 0) + " 条"));
        if (isChosen) button.appendChild(el("span", "tick", "✓"));
        if (!settled) {
          button.addEventListener("click", function () {
            applyArtifact(card.artifact_id, { value: option.value }, "正在按你选定的标准值取数…");
          });
        }
        list.appendChild(button);
      });
      body.appendChild(list);
      node.appendChild(body);
      return node;
    },

    clarification_card: function (card) {
      var payload = card.payload || {};
      var questions = payload.questions || [];
      var options = payload.options || [];
      var node = cardShell(card, "待确认", chip("需要你补充", "warn"), null);
      var body = el("div", "card-body");

      var ask = el("div", "ask-box");
      questions.forEach(function (question) {
        ask.appendChild(el("div", "ask-line", question));
      });
      body.appendChild(ask);

      if (options.length) {
        body.appendChild(el("div", "dim-hint", "选一个口径，或自己写："));
        var chips = el("div", "chip-row");
        options.forEach(function (option) {
          var label = option.label || option.answer || "";
          var button = el("button", "quick-chip", label);
          button.type = "button";
          button.addEventListener("click", function () {
            applyArtifact(
              card.artifact_id,
              { answer: option.answer || label },
              "正在按你确认的口径取数…"
            );
          });
          chips.appendChild(button);
        });
        body.appendChild(chips);
      }

      var box = el("div", "clarify-input");
      var input = el("input", "clarify-text");
      input.value = state.ui.clarifyDraft[card.artifact_id] || "";
      input.placeholder = "或直接描述口径，例如：按 SKU 加权后再汇总";
      input.addEventListener("input", function () {
        state.ui.clarifyDraft[card.artifact_id] = input.value;
      });
      var submit = el("button", null, "按此口径取数");
      submit.type = "button";
      submit.addEventListener("click", function () {
        var text = input.value.trim();
        if (!text) {
          input.focus();
          return;
        }
        applyArtifact(card.artifact_id, { answer: text }, "正在按你确认的口径取数…");
      });
      box.appendChild(input);
      box.appendChild(submit);
      body.appendChild(box);

      if (payload.sql) {
        body.appendChild(sqlBlock(payload.sql, ""));
      }
      node.appendChild(body);
      return node;
    },

    outline_card: function (card) {
      var payload = card.payload || {};
      // 还没主题 = 还没到大纲这一步：只让用户写主题，不先替他搭页。
      if (String(payload.status || "") === "needs_topic") {
        return outlineTopicCard(card, payload);
      }
      var pages = payload.pages || [];
      var topic = String(payload.topic || "");
      var confirmed = !!payload.confirmed;
      var pending = pages.filter(function (page) {
        return page.kind === "chart" && !page.bound_artifact_id;
      }).length;

      // 主题输入框 = 出稿依据。改完点「确认大纲」才生效（不 blur 自动存：
      // 让"主题改了但没重新出稿"这种半截状态尽量少出现）。
      var topicInput = el("input", "page-title outline-topic-input");
      topicInput.value = draftOf("outline-topic", card.artifact_id, topic);
      topicInput.placeholder = "这份 PPT 要讲什么（会作为标题与出稿依据）";
      topicInput.title = "主题是出稿依据：大纲按它来讲，PPT 的标题就是它";
      topicInput.addEventListener("input", function () {
        setDraft("outline-topic", card.artifact_id, topic, topicInput.value);
      });

      // 一按钮两义：没确认过 = 确认并出稿；已确认 = 按新主题/新大纲重出。
      var confirmBtn = el("button", null, confirmed ? "按大纲重新生成" : "确认大纲，生成 PPT");
      confirmBtn.type = "button";
      confirmBtn.disabled = !pages.length;
      confirmBtn.title = confirmed
        ? "按当前主题与大纲重新渲染一份 PPT"
        : "主题和页序确认之后才出稿——现在改只动大纲，不会渲染";
      confirmBtn.addEventListener("click", function () {
        var nextTopic = (topicInput.value || "").trim();
        if (!nextTopic) {
          topicInput.focus();
          notify("先定主题：这份 PPT 要讲什么？");
          return;
        }
        mutate("POST", conversationPath("/outline/confirm"), { topic: nextTopic }, {
          busyLabel: "正在按主题和大纲装配 PPT…",
          errPrefix: "确认失败：",
          onDone: function (res) {
            notify(
              "已按《" + res.topic + "》生成 " + res.slide_count + " 页 PPT。"
                + (state.ui.pptMode ? "" : "勾选顶部「PPT 模式」查看。")
            );
            if (state.ui.pptMode) openPptEditor(false);
          }
        });
      });

      // 批量补数：逐页独立跑，一页取不到不打断其余页，最后的汇总也如实说哪几页没成。
      var fill = el("button", "ghost", pending ? "补齐待取数（" + pending + "）" : "补齐待取数");
      fill.type = "button";
      fill.disabled = !pending;
      fill.title = pending ? "把所有还没绑图的页一次跑完" : "所有分析页都已经有图表了";
      fill.addEventListener("click", function () {
        mutate("POST", conversationPath("/outline/fill-pending"), undefined, {
          busyLabel: "正在逐页补齐取数（单页失败不打断其他页）…",
          errPrefix: "补齐取数未完成：",
          skipRefresh: true,
          followUp: recompileIfDeckExists,
          onDone: function (res) {
            notify(res.message || "补齐取数已执行。");
          }
        });
      });

      var add = el("button", "ghost", "新增一页");
      add.type = "button";
      add.title = "在末尾加一页空白页（不替你编内容）";
      add.addEventListener("click", function () {
        mutate("POST", conversationPath("/outline/pages"), { title: "", objective: "" }, {
          busyLabel: "正在新增一页…",
          errPrefix: "新增页失败：",
          skipRefresh: true,
          followUp: recompileIfDeckExists,
          onDone: function (res) {
            // 新页直接展开，省得用户再点一次「编辑内容」。
            if (res && res.page_id) state.ui.pageEditor[res.page_id] = true;
          }
        });
      });

      // 版本回退：后端每次"真的改了"都会推一版（push_version），这里把它暴露出来。
      // history 是栈，可以连点一直往前退；没有历史就禁用，别让用户点了没反应。
      var undo = el("button", "ghost", "回退");
      undo.type = "button";
      var canUndo = !!(card.history && card.history.length);
      undo.disabled = !canUndo;
      undo.title = canUndo
        ? "退回上一版大纲（撤销刚才的改动，可连点继续退）"
        : "还没有可回退的改动";
      undo.addEventListener("click", function () {
        mutate("POST", artifactPath(card, "/rollback"), {}, {
          busyLabel: "正在回退到上一版大纲…",
          errPrefix: "回退失败：",
          skipRefresh: true,
          followUp: recompileIfDeckExists,
          onDone: function () {
            notify("已回退到上一版大纲。");
          }
        });
      });

      var node = cardShell(
        card,
        "汇报大纲",
        chip(
          pages.length + " 页"
            + (pending ? " · " + pending + " 页待取数" : "")
            + (confirmed ? " · 已确认" : " · 待确认"),
          confirmed ? "ok" : (pending ? "warn" : "gray")
        ),
        [add, fill, undo, confirmBtn]
      );
      var body = el("div", "card-body");

      var topicRow = el("div", "outline-topic");
      topicRow.appendChild(el("span", "outline-topic-label", "主题"));
      topicRow.appendChild(topicInput);
      body.appendChild(topicRow);

      body.appendChild(
        el(
          "div",
          "dim-hint",
          "顺序是「主题 → 大纲 → 确认 → 出稿」：这里只搭大纲，点「确认大纲」之后才在最右侧生成真正的 PPT。" +
            "拖动左侧序号可以调整页序；每页绑的是画布上的图（图1、图2…），在聊天里直接说「图2」就能指代。" +
            "确认之后继续改大纲会自动重排 PPT；改错了点「回退」。"
        )
      );

      var list = el("div", "outline-list");
      pages.forEach(function (page) {
        list.appendChild(outlinePageRow(card, page));
      });
      body.appendChild(list);
      node.appendChild(body);
      return node;
    },

    deck_card: function (card) {
      var payload = card.payload || {};
      var slides = payload.slides || [];
      var activeIndex = state.ui.deckPage[card.artifact_id] || 0;
      activeIndex = Math.min(activeIndex, Math.max(0, slides.length - 1));

      var exportLink = el("a", "ghost-link", "导出 .pptx");
      exportLink.href = state.base + payload.export_url;
      exportLink.target = "_blank";

      var copy = el("button", "ghost", "复制本页图表");
      copy.type = "button";
      copy.addEventListener("click", function () {
        var chart = state.charts.get("deck_" + card.artifact_id);
        if (!chart) {
          copy.textContent = "本页无图表";
          return;
        }
        chart.getConnectedDataURL({
          type: "png",
          pixelRatio: 2,
          backgroundColor: "#0b1c2e"
        });
        var dataUrl = chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#0b1c2e" });
        fetch(dataUrl)
          .then(function (res) { return res.blob(); })
          .then(function (blob) {
            if (!navigator.clipboard || !window.ClipboardItem) throw new Error("当前浏览器不支持剪贴板图片");
            return navigator.clipboard.write([new window.ClipboardItem({ "image/png": blob })]);
          })
          .then(function () { copy.textContent = "已复制 ✓"; })
          .catch(function (err) { copy.textContent = "复制失败：" + err.message; });
      });

      var slide = slides[activeIndex] || {};
      var pageId = slidePageId(slide);

      var recompile = el("button", "ghost", "重新装配");
      recompile.type = "button";
      recompile.title = "按当前大纲重排本页（图表与结论都取大纲里的最新内容）";
      recompile.addEventListener("click", function () {
        mutate("POST", conversationPath("/outline/compile"), undefined, {
          busyLabel: "正在重新装配 PPT 预览…",
          errPrefix: "重新装配失败："
        });
      });

      var regen = el("button", "ghost", "本页重新取数");
      regen.type = "button";
      regen.disabled = !pageId || !pageId.length;
      regen.title = "只重跑这一页的数据，其他页不动；完成后会自动重新装配预览";
      regen.addEventListener("click", function () {
        regenerateSlide(slide);
      });

      var openEditor = el("button", null, "在编辑器里打开");
      openEditor.type = "button";
      openEditor.title = "勾选 PPT 模式，在最右侧用 TrainPPTAgent 打开这份 PPT（可拖拽、可改字、可导出）";
      openEditor.addEventListener("click", function () {
        setPptMode(true);
      });

      var node = cardShell(
        card,
        "PPT 预览",
        chip(slides.length + " 页 · 16:9", "gray"),
        [openEditor, regen, recompile, copy, exportLink]
      );
      var body = el("div", "card-body");
      var layout = el("div", "deck-layout");

      var nav = el("div", "deck-nav");
      slides.forEach(function (slideItem, index) {
        var button = el("button", index === activeIndex ? "active" : "");
        button.type = "button";
        button.appendChild(el("span", "n", "P" + (index + 1)));
        button.appendChild(el("span", null, slideItem.title || "未命名"));
        button.addEventListener("click", function () {
          state.ui.deckPage[card.artifact_id] = index;
          renderCanvas();
        });
        nav.appendChild(button);
      });
      layout.appendChild(nav);

      var slideLayoutName = slideLayout(slide);
      var frame = el("div", "slide-frame");
      var slideBody = el("div", "slide-body");

      // 版式由大纲页决定（谁在左、谁在右、还是只要一半）。同一个 layout 字段
      // 既管这里也管 pptx 导出，所以在预览里换版式 = 真的改了这一页。
      var toolbar = el("div", "slide-toolbar");
      var titleNode = el("h4", "slide-editable", slide.title || "未命名页面");
      titleNode.title = "点一下就能改这一页的标题";
      toolbar.appendChild(titleNode);
      var tools = el("div", "slide-tools");
      tools.appendChild(
        layoutSelect(slideLayoutName, function (value) {
          if (value === slideLayoutName) return;
          if (!pageId) {
            reportError("这一页没有对应的大纲页，无法换版式：", new Error("slide has no page id"));
            return;
          }
          saveSlideContent(pageId, { layout: value }, "正在换版式并重新装配预览…");
        })
      );
      tools.appendChild(chip(layoutLabel(slideLayoutName), "gray"));
      toolbar.appendChild(tools);
      slideBody.appendChild(toolbar);
      if (slide.subtitle) slideBody.appendChild(el("div", "sub", slide.subtitle));

      var textPanel = el("div", "slide-panel");
      textPanel.appendChild(el("h5", null, "数据洞察"));
      var findingsList = el("ul", "slide-editable", null);
      findingsList.contentEditable = "true";
      findingsList.spellcheck = false;
      findingsList.title = "点一下就能改；每行一条";
      if (slide.findings && slide.findings.length) {
        slide.findings.slice(0, 4).forEach(function (item) {
          findingsList.appendChild(el("li", null, item));
        });
      } else {
        findingsList.appendChild(el("li", null, "本页没有图表结论，点这里补一条。"));
      }
      textPanel.appendChild(findingsList);
      textPanel.appendChild(el("h5", null, "业务解读"));
      var narrativeNode = el("p", "slide-editable", slide.narrative || "暂无业务解读，点这里补一句。");
      narrativeNode.contentEditable = "true";
      narrativeNode.spellcheck = false;
      narrativeNode.title = "点一下就能改这一页要讲的那句话";
      textPanel.appendChild(narrativeNode);

      var chartHolderId = "deckchart_" + card.artifact_id;
      var chartPanel = el("div", "slide-panel");
      chartPanel.appendChild(el("h5", null, "图表"));
      var chartHolder = el("div", "slide-chart");
      chartHolder.id = chartHolderId;
      chartPanel.appendChild(chartHolder);

      var cols = el("div", "slide-cols");
      if (slideLayoutName === "chart_only") {
        cols.className += " single";
        cols.appendChild(chartPanel);
      } else if (slideLayoutName === "text_only") {
        cols.className += " single";
        cols.appendChild(textPanel);
      } else if (slideLayoutName === "chart_left") {
        cols.appendChild(chartPanel);
        cols.appendChild(textPanel);
      } else {
        cols.appendChild(textPanel);
        cols.appendChild(chartPanel);
      }
      slideBody.appendChild(cols);

      // 预览区直改：焦点**离开整块**才提交。三个可编辑区之间来回点不该触发保存——
      // 那会因为一次保存引发整块重绘，把正在点的那个框换掉。
      slideBody.addEventListener("focusout", function (event) {
        var next = event.relatedTarget;
        if (next && slideBody.contains(next)) return;
        var patch = slidePatchFromNode(slide, titleNode, findingsList, narrativeNode);
        if (Object.keys(patch).length) {
          saveSlideContent(pageId, patch, "正在保存这一页并重新装配预览…");
        }
      });

      frame.appendChild(slideBody);
      layout.appendChild(frame);
      body.appendChild(layout);
      body.appendChild(
        el(
          "div",
          "dim-hint",
          "标题、数据洞察、业务解读都可以在预览里直接点开编辑；改完点别处即自动保存，并重排这一版预览（导出的 .pptx 就是这一版）。"
        )
      );
      node.appendChild(body);

      requestAnimationFrame(function () {
        var target = document.getElementById(chartHolderId);
        if (target && slide.chart_spec && slide.chart_spec.rows && slide.chart_spec.rows.length) {
          mountChart(target, slide.chart_spec, "deck_" + card.artifact_id);
        }
      });
      return node;
    }
  };

  /** 预览区改动 → PATCH 的 payload。只在**真的变了**的时候带上那个字段。 */
  function slidePatchFromNode(slide, titleNode, findingsList, narrativeNode) {
    var patch = {};
    var title = String(titleNode.textContent || "").trim();
    if (title && title !== String(slide.title || "")) patch.title = title;

    var findings = Array.prototype.map
      .call(findingsList.querySelectorAll("li"), function (node) {
        return String(node.textContent || "").trim();
      })
      .filter(Boolean);
    if (findings.join("\n") !== (slide.findings || []).join("\n")) patch.findings = findings;

    var narrative = String(narrativeNode.textContent || "").trim();
    if (narrative !== String(slide.narrative || "")) patch.narrative = narrative;
    return patch;
  }

  function saveSlideContent(pageId, patch, busyLabel) {
    if (!pageId) {
      reportError("这一页没有对应的大纲页，改动没能落库：", new Error("slide has no page id"));
      return;
    }
    setBusy(true, busyLabel || "正在保存这一页…");
    api("PATCH", conversationPath("/outline/pages/" + pageId), patch)
      .then(function (res) {
        if (res && res.canvas) state.canvas = res.canvas;
        // 预览是**投影**：大纲改了不会自己变。所以保存后重新装配一次，
        // 让用户立刻看到"我改的字确实进了这一版"。
        return api("POST", conversationPath("/outline/compile"));
      })
      .then(function (res) {
        if (res && res.canvas) state.canvas = res.canvas;
        return refreshDetail();
      })
      .catch(function (err) {
        reportError("保存这一页失败：", err);
      })
      .finally(function () {
        setBusy(false);
      });
  }

  function regenerateSlide(slide) {
    var pageId = slidePageId(slide);
    if (!pageId) {
      reportError("这一页没有对应的大纲页，无法重新取数：", new Error("slide has no page id"));
      return;
    }
    setBusy(true, "正在重跑这一页的取数…");
    api("POST", conversationPath("/outline/pages/" + pageId + "/regenerate"))
      .then(function (res) {
        if (res && res.canvas) state.canvas = res.canvas;
        notify(res && res.message ? res.message : "这一页已重新取数。");
        return api("POST", conversationPath("/outline/compile"));
      })
      .then(function (res) {
        if (res && res.canvas) state.canvas = res.canvas;
        return refreshDetail();
      })
      .catch(function (err) {
        reportError("重新取数未完成：", err);
      })
      .finally(function () {
        setBusy(false);
      });
  }

  /** The 高级参数 drawer: the fine-grained knobs the config UI exposed. */
  function paramDrawer(card) {
    var payload = card.payload || {};
    var isPrice = String(payload.dimension_key || "") === "price";
    var current = payload.price_band || {};
    var wrap = el("div", "param-drawer");

    var rowStrategy = el("div", "param-row");
    rowStrategy.appendChild(el("label", null, "价格带分桶策略"));
    var strategySelect = el("select");
    PRICE_STRATEGIES.forEach(function (item) {
      var option = el("option", null, item.label);
      option.value = item.value;
      if ((current.strategy || "rounded_width") === item.value) option.selected = true;
      strategySelect.appendChild(option);
    });
    strategySelect.disabled = !isPrice;
    rowStrategy.appendChild(strategySelect);
    wrap.appendChild(rowStrategy);

    var rowBuckets = el("div", "param-row");
    rowBuckets.appendChild(el("label", null, "分桶数"));
    var buckets = el("input", "param-number");
    buckets.type = "number";
    buckets.min = "2";
    buckets.max = "20";
    buckets.value = String(current.bucket_count || 10);
    buckets.disabled = !isPrice;
    rowBuckets.appendChild(buckets);
    wrap.appendChild(rowBuckets);

    var rowWindow = el("div", "param-row");
    rowWindow.appendChild(el("label", null, "时间窗口"));
    var windowSelect = el("select");
    TIME_WINDOWS.forEach(function (value) {
      var option = el("option", null, value || "不限");
      option.value = value;
      if ((payload.time_window || "") === value) option.selected = true;
      windowSelect.appendChild(option);
    });
    rowWindow.appendChild(windowSelect);
    wrap.appendChild(rowWindow);

    var actions = el("div", "param-actions");
    var apply = el("button", null, "重新取数");
    apply.type = "button";
    apply.addEventListener("click", function () {
      var params = { time_window: windowSelect.value };
      if (isPrice) {
        params.price_band = {
          strategy: strategySelect.value,
          bucket_count: Number(buckets.value) || 10,
          mode: "adaptive"
        };
      }
      applyArtifact(card.artifact_id, { params: params }, "正在按新参数重新取数…");
    });
    actions.appendChild(apply);
    actions.appendChild(
      el("span", "dim-hint", isPrice ? "参数会下推到 SQL 生成" : "该维度仅支持调整时间窗口")
    );
    wrap.appendChild(actions);
    return wrap;
  }

  function sqlBlock(sql, explanation) {
    var details = el("details", "sql");
    details.appendChild(el("summary", null, "展开底层 SQL"));
    var pre = el("pre", null, sql || "（本次没有生成可执行 SQL）");
    details.appendChild(pre);
    if (explanation) {
      details.appendChild(el("div", "dim-hint", explanation));
    }
    return details;
  }

  /** 这张图是按什么条件取的。
   *
   * 条件来自后端**校验通过**的槽位（agent 说的字段必须命中真实语义字段），
   * 所以这里显示的就是真正下推到 SQL 的那几条，不是复述用户的话。
   */
  function appliedFilters(card) {
    var filters = (card.payload && card.payload.applied_filters) || [];
    var row = el("div", "filter-chips");
    if (!filters.length) return row;
    row.appendChild(el("span", "dim-hint", "本次取数条件："));
    filters.forEach(function (item) {
      row.appendChild(chip(item.display || item.field || "", "gray"));
    });
    return row;
  }

  /** 图表卡的 SQL：能看、能改、能重跑。
   *
   * 保存与执行刻意分成两步（与配置后台的 HITL 一致）：改完先落盘看一眼，
   * 确认无误再执行。执行失败时后端保留原图，这里只把真实原因说出来。
   */
  function sqlEditor(card) {
    var payload = card.payload || {};
    var baseSql = String(payload.sql || "");
    var key = card.artifact_id;

    var details = el("details", "sql");
    details.open = !!state.ui.sqlOpen[key];
    details.addEventListener("toggle", function () {
      state.ui.sqlOpen[key] = details.open;
    });
    details.appendChild(el("summary", null, baseSql ? "底层 SQL（可改可重跑）" : "底层 SQL"));

    var area = multiLineField(
      "sql-area",
      draftOf("sql", key, baseSql),
      Math.min(16, Math.max(4, baseSql.split("\n").length + 1))
    );
    area.spellcheck = false;
    if (!baseSql) area.placeholder = "这张卡还没有 SQL。可以先按上面的方向取一次数，或直接写下你的 SELECT。";
    area.addEventListener("input", function () {
      setDraft("sql", key, baseSql, area.value);
    });
    details.appendChild(area);

    var actions = el("div", "sql-actions");
    var save = el("button", "ghost", "保存 SQL 修改");
    save.type = "button";
    save.addEventListener("click", function () {
      var text = area.value.trim();
      if (!text) {
        area.focus();
        return;
      }
      mutate("PUT", artifactPath(card, "/sql"), {
        sql: text,
        sql_explanation: String(payload.sql_explanation || "")
      }, {
        busyLabel: "正在保存 SQL…",
        errPrefix: "保存 SQL 失败：",
        onDone: function () {
          notify("SQL 已保存（还没有执行）。要生效请点「确认执行」。");
        }
      });
    });

    var run = el("button", null, "确认执行");
    run.type = "button";
    run.addEventListener("click", function () {
      var text = area.value.trim();
      if (!text) {
        area.focus();
        return;
      }
      // 输入框里还有没保存的改动：先存再跑，避免"跑的还是上一版"。
      var saveFirst = text !== baseSql
        ? api("PUT", artifactPath(card, "/sql"), { sql: text, sql_explanation: String(payload.sql_explanation || "") })
        : Promise.resolve(null);
      setBusy(true, "正在执行 SQL 取数…");
      saveFirst
        .then(function () {
          return api("POST", artifactPath(card, "/execute"));
        })
        .then(function (res) {
          if (res && res.canvas) state.canvas = res.canvas;
          // 执行成功后服务端会回填**实际执行过**的 SQL，草稿使命结束，让它对齐新基准。
          clearDraft("sql", key);
          notify("SQL 执行完成，图表已按新结果更新（" + (res && res.rows_count ? res.rows_count + " 行" : "见数据表") + "）。");
          return refreshDetail();
        })
        .catch(function (err) {
          // 失败时后端不覆盖原图，所以右边还是上一次的真实结果——如实说明。
          reportError("SQL 执行未完成（图表保持原样）：", err);
        })
        .finally(function () {
          setBusy(false);
        });
    });

    actions.appendChild(save);
    actions.appendChild(run);
    actions.appendChild(el("span", "spacer"));
    if (payload.sql_edited) actions.appendChild(chip("SQL 已手改", "warn"));
    details.appendChild(actions);
    details.appendChild(
      el("div", "dim-hint", "只允许单条 SELECT / CTE；改的是取数口径，改完记得重新编译 PPT 才会更新预览。")
    );
    if (payload.sql_explanation) {
      details.appendChild(el("div", "dim-hint", payload.sql_explanation));
    }
    return details;
  }

  function dataTable(spec) {
    var wrap = el("div", "table-wrap");
    var rows = (spec.rows || []).filter(function (row) {
      return row && typeof row === "object";
    });
    if (!rows.length) {
      wrap.appendChild(el("div", "canvas-empty", "没有数据行"));
      return wrap;
    }
    var columns = spec.columns && spec.columns.length ? spec.columns : Object.keys(rows[0]);
    var table = el("table", "data");
    var thead = el("thead");
    var headRow = el("tr");
    columns.forEach(function (column) {
      headRow.appendChild(el("th", null, column));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = el("tbody");
    rows.slice(0, 200).forEach(function (row) {
      var tr = el("tr");
      columns.forEach(function (column) {
        tr.appendChild(el("td", null, formatCell(row[column])));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // ----------------------------------------------------------------- actions

  /** Apply a decision made on a canvas card and take whatever follows.
   *
   * 这条路径会在服务端追加一条助手消息（确认/澄清的后续结论），所以必须整轮刷新，
   * 不能只重画画布。
   */
  function applyArtifact(artifactId, body, busyLabel) {
    return mutate(
      "POST",
      conversationPath("/artifacts/" + artifactId + "/resolve"),
      body,
      { busyLabel: busyLabel || "正在处理…", errPrefix: "操作未完成：" }
    );
  }

  /** 直接改大纲的一页（标题/目标/要点/结论/版式/删除）。
   *
   * 只重画画布、不刷新对话：这些改动不会产生新的助手消息，而重画对话会把
   * 左侧的阅读位置弹到底部——用户在改右边的东西，左边不该乱动。
   */
  function patchPage(pageId, payload) {
    return mutate("PATCH", conversationPath("/outline/pages/" + pageId), payload, {
      busyLabel: "正在保存对这一页的修改…",
      errPrefix: "修改大纲失败：",
      skipRefresh: true,
      followUp: recompileIfDeckExists
    });
  }

  function loadConversation(id) {
    state.cid = id;
    // 换会话必须先把编辑器内容清空：否则右边还顶着上一个会话的 PPT，
    // 而 syncPptModeWithCanvas 看到"已经载过"就不会去拉新的（会真的串台）。
    var frame = $("pptFrame");
    if (frame) frame.src = "about:blank";
    // 主题输入框同理：不清空的话，上一份会话的主题会留在框里被误提交。
    state.ui.pptEditingTopic = false;
    var topicInput = $("pptTopicInput");
    if (topicInput) topicInput.value = "";
    setPptSetupStatus("");
    return api("GET", "/api/v1/conversations/" + id).then(function (res) {
      state.messages = res.messages || [];
      state.canvas = res.canvas;
      updateSceneChip(res.conversation);
      renderMessages();
      renderCanvas();
      $("chatMeta").textContent = (res.messages || []).length + " 条消息";
      try {
        localStorage.setItem("fz_agent_cid", id);
      } catch (err) {
        /* 隐私模式下 localStorage 可能不可用 */
      }
    });
  }

  function refreshDetail() {
    return api("GET", "/api/v1/conversations/" + state.cid).then(function (res) {
      state.messages = res.messages || [];
      state.canvas = res.canvas;
      updateSceneChip(res.conversation);
      renderMessages();
      renderCanvas();
      $("chatMeta").textContent = (res.messages || []).length + " 条消息";
    });
  }

  /** 头部那颗胶囊：显示**本轮实际加载的配置**，而不是会话级的场景。
   *
   * 配置是按需加载的轮次资源——问价格带就加载价格那套，换折线图就一套都不加载，
   * 下一轮改问纤维再换成纤维那套。所以这里读 `working_memory.turn_config`
   * （后端每轮写入），而不是会话上的 `scene_name`（那只代表"上次用过什么"）。
   */
  function updateSceneChip(conversation) {
    if (!conversation) return;
    var chip = $("sceneChip");
    var tc = (conversation.working_memory || {}).turn_config || {};
    var names = (tc.names || []).filter(function (name) {
      return !!name;
    });
    chip.innerHTML = "";
    chip.appendChild(document.createTextNode("本轮配置："));

    if (!tc.source) {
      // 还没跑过任何一轮：不下结论，说明它按问题匹配。
      chip.appendChild(el("b", null, "待定"));
      chip.appendChild(el("span", "dim-hint", "（发一句话后按问题匹配）"));
      chip.title = "配置按轮加载：这一轮用到哪套，由多轮 agent 依据配置目录判断";
      return;
    }

    if (!names.length) {
      // 没加载配置有几种原因（本轮不涉及 / 没判定出该用哪套 / 服务不可用），
      // 后端每轮把原因写在 note 里，直接照它说，别在这里统一成一句话。
      chip.appendChild(el("b", null, "未加载"));
      chip.appendChild(el("span", "dim-hint", tc.note ? "（" + tc.note + "）" : "（本轮不涉及配置）"));
      chip.title = tc.note || "";
      return;
    }

    chip.appendChild(el("b", null, names.join(" + ")));
    chip.appendChild(
      el("span", "dim-hint", tc.merged ? "（本轮合并 " + names.length + " 套）" : "（本轮按需加载）")
    );
    chip.title = (tc.config_ids || []).join(", ");
  }

  function loadConversations(selectId) {
    return api("GET", "/api/v1/conversations").then(function (res) {
      var select = $("convSelect");
      select.innerHTML = "";
      var items = res.items || [];
      if (!items.length) {
        var option = el("option", null, "（暂无会话）");
        option.value = "";
        select.appendChild(option);
        return null;
      }
      items.forEach(function (item) {
        var option = el("option", null, item.title + " · " + item.conversation_id.slice(-6));
        option.value = item.conversation_id;
        select.appendChild(option);
      });
      var target = selectId || items[0].conversation_id;
      select.value = target;
      return target;
    });
  }

  function createConversation() {
    return api("POST", "/api/v1/conversations", { title: "Zara 复盘会话" })
      .then(function (res) {
        var id = res.conversation.conversation_id;
        return loadConversations(id).then(function () {
          return loadConversation(id);
        });
      })
      .catch(function (err) {
        state.messages.push({ role: "system", content: "创建会话失败：" + err.message });
        renderMessages();
      });
  }

  // -------------------------------------------------------------------- boot

  function bindEvents() {
    $("sendBtn").addEventListener("click", function () {
      send($("input").value, null);
    });
    $("input").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        send($("input").value, null);
      }
    });
    $("newConvBtn").addEventListener("click", function () {
      createConversation();
    });
    $("reloadConvBtn").addEventListener("click", function () {
      if (state.cid) refreshDetail();
    });
    $("refreshCanvasBtn").addEventListener("click", function () {
      if (state.cid) {
        api("GET", "/api/v1/conversations/" + state.cid + "/canvas").then(function (res) {
          state.canvas = res;
          renderCanvas();
        });
      }
    });
    $("convSelect").addEventListener("change", function (event) {
      if (event.target.value) loadConversation(event.target.value);
    });

    // ---- 三栏：中栏收起/展开 + 右侧 PPT 编辑器 ----
    $("collapseCanvasBtn").addEventListener("click", function () {
      setCanvasCollapsed(true);
    });
    $("expandCanvasBtn").addEventListener("click", function () {
      setCanvasCollapsed(false);
    });
    // 勾选框 = 进入/退出 PPT 模式的唯一入口（不再生成后自动打开）。
    $("pptModeCheck").addEventListener("change", function (event) {
      setPptMode(event.target.checked);
    });
    $("pptCloseBtn").addEventListener("click", function () {
      setPptMode(false);
    });
    $("pptReloadBtn").addEventListener("click", function () {
      openPptEditor(true);
    });
    $("pptSyncPullBtn").addEventListener("click", function () {
      if (!state.cid) return;
      // 回写是编辑器发起（POST .../deck/callback），这里只是把画布/大纲重新读一遍。
      refreshDetail().then(function () {
        notify("已读回编辑器的改动（标题/要点/结论）。");
      });
    });
    // ---- 右侧工作台的起步区（还没有 PPT 时） ----
    $("pptTopicBtn").addEventListener("click", submitPptTopic);
    $("pptConfirmBtn").addEventListener("click", confirmPptFromPane);
    $("pptChangeTopicBtn").addEventListener("click", changePptTopic);
    $("pptTopicInput").addEventListener("keydown", function (event) {
      // 回车提交、Shift+Enter 换行（和主题卡一致）。
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submitPptTopic();
      }
    });
    // 本页头部可直接切模型：走 /api/v1/model-selection，失败如实报错并拨回真实档位。
    $("modelSelect").addEventListener("change", onModelSelectChange);
    $("backendBase").addEventListener("change", function (event) {
      state.base = event.target.value.replace(/\/$/, "");
      try {
        localStorage.setItem("fz_agent_base", state.base);
      } catch (err) {
        /* ignore */
      }
      loadConversations().then(function (id) {
        if (id) loadConversation(id);
      });
    });

    var resizeTimer = null;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        state.charts.forEach(function (chart) {
          chart.resize();
        });
      }, 150);
    });
  }

  /* 头部「模型」胶囊：本页**可直接切换**全局模型档位。
   *
   * 为什么可切：模型是全项目**唯一一处**配置，改一次对三处调用同时生效
   * （配置中心推荐 / SQL 生成 / 对话画布），入口没必要分散到另一页去。
   * 为什么不用重启：档位由模型注册表在**调用时**解析，所以切完下一轮就生效。
   *
   * 取向与项目一致——**不兜底、不粉饰**：接口失败就把真实原因显示出来，
   * 切换失败把下拉拨回真实档位，不让它停在一个没生效的值上。
   */

  function modelOptionLabel(option) {
    var label = String((option && (option.label || option.id)) || "");
    var model = String((option && option.model) || "");
    var suffix = model && label.indexOf(model) < 0 ? " · " + model : "";
    return label + suffix;
  }

  function currentModelOption() {
    var options = (state.modelSelection && state.modelSelection.options) || [];
    var current = String((state.modelSelection && state.modelSelection.current) || "");
    for (var i = 0; i < options.length; i += 1) {
      if (String(options[i].id || "") === current) return options[i];
    }
    return null;
  }

  function renderModelSelection() {
    var select = $("modelSelect");
    if (!select) return;
    var hint = $("modelSwitchHint");
    var options = (state.modelSelection && state.modelSelection.options) || [];
    select.innerHTML = "";
    if (!options.length) {
      var placeholder = el("option", null, "不可用");
      placeholder.value = "";
      select.appendChild(placeholder);
      select.disabled = true;
      if (hint) hint.textContent = "未能读取模型清单";
      return;
    }
    options.forEach(function (option) {
      var node = el(
        "option",
        null,
        modelOptionLabel(option) + (option.available === false ? "（未配置密钥）" : "")
      );
      node.value = String(option.id || "");
      node.disabled = option.available === false;
      select.appendChild(node);
    });
    select.value = String((state.modelSelection && state.modelSelection.current) || "");
    select.disabled = false;
    var current = currentModelOption();
    if (hint) {
      hint.textContent =
        current && current.available === false ? "当前档未配置密钥，调用会失败" : "";
    }
  }

  function refreshModelSelection() {
    return api("GET", "/api/v1/model-selection")
      .then(function (res) {
        state.modelSelection = {
          current: String((res && res.current) || ""),
          options: (res && res.options) || []
        };
        renderModelSelection();
      })
      .catch(function (err) {
        // 读不到就如实说读不到（下拉禁用），不假装有值。
        var select = $("modelSelect");
        var hint = $("modelSwitchHint");
        if (select) select.disabled = true;
        if (hint) hint.textContent = "模型清单读取失败：" + (err && err.message ? err.message : err);
      });
  }

  function switchModel(modelId) {
    return api("PUT", "/api/v1/model-selection", { model_id: modelId }).then(function (res) {
      state.modelSelection = {
        current: String((res && res.current) || ""),
        options:
          (res && res.options) || (state.modelSelection && state.modelSelection.options) || []
      };
      renderModelSelection();
      var current = currentModelOption();
      var hint = $("modelSwitchHint");
      if (hint) {
        hint.textContent =
          "已切到 " + (current ? modelOptionLabel(current) : modelId) + "（下一轮调用生效）";
      }
    });
  }

  function onModelSelectChange(event) {
    var select = (event && event.target) || $("modelSelect");
    if (!select) return;
    var requested = String(select.value || "");
    switchModel(requested).catch(function (err) {
      // 切换失败要把下拉拨回真实档位，不能让它停在一个没生效的值上。
      select.value = String((state.modelSelection && state.modelSelection.current) || "");
      var hint = $("modelSwitchHint");
      if (hint) hint.textContent = "切换失败：" + (err && err.message ? err.message : err);
    });
  }

  function boot() {
    try {
      var savedBase = localStorage.getItem("fz_agent_base");
      if (savedBase) {
        state.base = savedBase;
        $("backendBase").value = savedBase;
      }
    } catch (err) {
      /* ignore */
    }
    renderQuickChips();
    renderMessages();
    renderCanvas();
    bindEvents();
    refreshModelSelection();

    var savedCid = null;
    try {
      savedCid = localStorage.getItem("fz_agent_cid");
    } catch (err) {
      /* ignore */
    }

    api("GET", "/health")
      .catch(function () {
        state.messages.push({
          role: "system",
          content: "无法连接后端 " + state.base + "。请先启动后端服务，再刷新页面。"
        });
        renderMessages();
        return null;
      })
      .then(function () {
        return loadConversations(savedCid);
      })
      .then(function (id) {
        if (id) return loadConversation(id);
        return null;
      })
      .catch(function (err) {
        state.messages.push({ role: "system", content: "初始化失败：" + err.message });
        renderMessages();
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
