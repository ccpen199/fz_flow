const UI_STATE_KEY = "fz_workflow_ui_state_v1";
const VALID_TABS = new Set(["overview", "config", "clothing", "query", "delivery"]);

const state = {
  backendBase: "http://127.0.0.1:18900",
  agentWait: {
    recommend: null,
    sqlResult: null,
  },
  agentWaitTimerId: 0,
  activeTab: "config",
  scenes: [],
  createSceneCollapsed: true,
  sceneListCollapsed: false,
  sceneConfigCollapsed: false,
  sceneFieldsCardCollapsed: false,
  sceneRelationsCardCollapsed: true,
  fieldAdvancedOpen: false,
  relationAdvancedOpen: false,
  sessions: [],
  queryHistory: [],
  currentSceneId: "",
  restoreSessionId: "",
  restoredGoalInput: "",
  restoredQueryIntentInput: "",
  currentSceneDetail: null,
  currentScenePlaybook: null,
  selectedPresetKey: "",
  selectedPresetQuestion: "",
  semanticCacheFields: [],
  semanticCacheKeyword: "",
  editingSemanticCacheId: "",
  currentLlmAgentDraft: null,
  llmDraftBySceneId: {},
  llmDraftSaveTimers: {},
  llmCacheStatus: null,
  sceneLoadError: "",
  autoRecommendedSceneIds: {},
  intentTemplatesCollapsed: true,
  currentSession: null,
  currentDeck: null,
  currentArtifact: null,
  currentReportState: null,
  pptScheme: "presenton_ai",
  pptSchemes: [
    { scheme: "presenton_ai", name: "Presenton AI PPT 生成", category: "AI PPT 生成器", description: "调用本地或配置的 presenton/presenton 服务，由大模型生成并导出 .pptx。" },
  ],
  currentSlide: null,
  clothing: {
    facets: null,
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    selectedId: null,
    detail: null,
  },
};

const el = (id) => document.getElementById(id);
const pretty = (value) => JSON.stringify(value ?? {}, null, 2);

function readStoredUiState() {
  try {
    const raw = window.localStorage?.getItem(UI_STATE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    console.warn("load ui state failed", error);
    return {};
  }
}

function loadStoredUiState() {
  const saved = readStoredUiState();
  const backendBase = String(saved.backendBase || "").trim();
  const sceneId = String(saved.currentSceneId || "").trim();
  const sessionId = String(saved.currentSessionId || "").trim();
  const activeTab = String(saved.activeTab || "").trim();
  const pptScheme = String(saved.pptScheme || "").trim();
  state.backendBase = normalizeBackendBase(backendBase || state.backendBase);
  if (sceneId) state.currentSceneId = sceneId;
  if (sessionId) state.restoreSessionId = sessionId;
  if (VALID_TABS.has(activeTab)) state.activeTab = activeTab;
  if (pptScheme) state.pptScheme = pptScheme;
  state.restoredGoalInput = String(saved.goalInput || "").trim();
  state.restoredQueryIntentInput = String(saved.queryIntentInput || "").trim();
}

function persistUiState() {
  try {
    const payload = {
      backendBase: state.backendBase,
      currentSceneId: state.currentSceneId || "",
      currentSessionId: state.currentSession?.session_id || state.restoreSessionId || "",
      activeTab: VALID_TABS.has(state.activeTab) ? state.activeTab : "config",
      pptScheme: state.pptScheme || "presenton_ai",
      goalInput: el("goalInput")?.value || state.restoredGoalInput || "",
      queryIntentInput: el("queryIntentInput")?.value || state.restoredQueryIntentInput || "",
    };
    window.localStorage?.setItem(UI_STATE_KEY, JSON.stringify(payload));
  } catch (error) {
    console.warn("save ui state failed", error);
  }
}

function restoreTextInputs() {
  if (state.restoredGoalInput && el("goalInput")) {
    el("goalInput").value = state.restoredGoalInput;
  }
  if (state.restoredQueryIntentInput && el("queryIntentInput")) {
    el("queryIntentInput").value = state.restoredQueryIntentInput;
  }
}

const SCENE_DOC_NAME_MAP = {
  "竞品分析": "竞品与价格分析",
  "上新趋势分析": "趋势与爆款分析",
};
const SCENE_INTENT_TEMPLATES = {
  default: [
    "各品牌的SKU数、平均价、最低价、最高价和价格跨度是多少，按价格跨度降序返回前20",
    "各一级类目和二级类目的SKU数、平均价和价格跨度是多少，按SKU数降序返回前30",
    "各品牌SKU丰富度排行，返回品牌、SKU数、覆盖二级类目数、覆盖叶子类目数",
    "按上架日期统计每日新增SKU数，识别上新高峰日期",
    "各场景标签下SKU数、品牌数和平均价格是多少",
  ],
  "商品价格分析": [
    "各品牌的SKU数、平均价、最低价、最高价和价格跨度是多少，按价格跨度降序返回前20",
    "各一级类目和二级类目的SKU数、平均价和价格跨度是多少，按SKU数降序返回前30",
    "按品牌统计0-99、100-199、200-399、400-799、800+价格带分布和占比",
    "最近抓取批次中各品牌平均价最高的是哪些，返回品牌、SKU数、平均价、最高价",
    "各来源站点域名的品牌覆盖、SKU数和平均价差异是什么",
    "各品牌在不同场景标签下的平均价差异是多少，注意这不是平台价差",
  ],
  "品牌平台价格分析": [
    "各品牌的SKU数、平均价、最低价、最高价和价格跨度是多少，按价格跨度降序返回前20",
    "各一级类目和二级类目的SKU数、平均价和价格跨度是多少，按SKU数降序返回前30",
    "按品牌统计0-99、100-199、200-399、400-799、800+价格带分布和占比",
    "最近抓取批次中各品牌平均价最高的是哪些，返回品牌、SKU数、平均价、最高价",
    "各来源站点域名的品牌覆盖、SKU数和平均价差异是什么",
    "各品牌在不同场景标签下的平均价差异是多少，注意这不是平台价差",
  ],
  "竞品分析": [
    "最近30天各二级类目下各品牌的价格带分布如何",
    "按二级类目分组，各品牌的价格定位差异是什么，返回SKU数、均价、价格跨度",
    "各来源站点域名下品牌SKU覆盖和平均价格差异是什么，注意这不是平台价差",
    "各二级类目中品牌SKU覆盖和平均价差异是多少，识别可对比的品牌品类组合",
    "各品牌在材质维度上的SKU覆盖和平均价格有什么差异",
    "各品牌功能标签覆盖数、SKU数和平均价格有什么差异",
    "按二级类目分组，各品牌图案和肌理结构差异是什么",
    "各品牌在织造方式和工艺类型上的覆盖差异是什么",
  ],
  "竞品与价格分析": [
    "最近30天各二级类目下各品牌的价格带分布如何",
    "按二级类目分组，各品牌的价格定位差异是什么，返回SKU数、均价、价格跨度",
    "各来源站点域名下品牌SKU覆盖和平均价格差异是什么，注意这不是平台价差",
    "各二级类目中品牌SKU覆盖和平均价差异是多少，识别可对比的品牌品类组合",
    "各品牌在材质维度上的SKU覆盖和平均价格有什么差异",
    "各品牌功能标签覆盖数、SKU数和平均价格有什么差异",
    "按二级类目分组，各品牌图案和肌理结构差异是什么",
    "各品牌在织造方式和工艺类型上的覆盖差异是什么",
  ],
  "商品结构分析": [
    "各品牌的一级类目和二级类目布局分别是什么，返回SKU数和品牌内占比",
    "各品牌SKU丰富度排行，返回品牌、SKU数、覆盖二级类目数、覆盖叶子类目数",
    "各二级类目中品牌覆盖数和SKU数是多少，识别竞争最充分的品类",
    "各品牌颜色丰富度排行，返回颜色数、SKU数、主力颜色",
    "各品牌图片主色和Pantone色号覆盖结构是什么",
    "各品牌图案、肌理、织造方式和工艺类型结构是什么",
    "最近上新商品在品牌和二级类目上的结构是什么",
    "各价格带中的品类结构是什么，返回价格带、一级类目、SKU数、占比",
    "哪些商品描述中包含尺码、尺寸或SIZE TABLE，可作为尺码抽取候选",
  ],
  "上新趋势分析": [
    "按上架日期统计每日新增SKU数，识别上新高峰日期",
    "各品牌每日上新SKU数变化趋势是什么",
    "最近一次抓取批次中，各品牌新增商品数量排行",
    "各品类在抓取日期上的SKU数变化是什么",
    "潜在高价值新品：最近上架且价格高于全量均价2倍的商品有哪些",
    "各场景标签最近上新SKU数和平均价格变化是什么",
    "最近上新商品的图案、肌理和主色趋势是什么",
    "潜在高价值新品的图案、肌理、Pantone色号和工艺特征是什么",
  ],
  "趋势与爆款分析": [
    "按上架日期统计每日新增SKU数，识别上新高峰日期",
    "各品牌每日上新SKU数变化趋势是什么",
    "最近一次抓取批次中，各品牌新增商品数量排行",
    "各品类在抓取日期上的SKU数变化是什么",
    "潜在高价值新品：最近上架且价格高于全量均价2倍的商品有哪些",
    "各场景标签最近上新SKU数和平均价格变化是什么",
    "最近上新商品的图案、肌理和主色趋势是什么",
    "潜在高价值新品的图案、肌理、Pantone色号和工艺特征是什么",
  ],
};

function formatSceneName(name) {
  const sceneName = String(name || "").trim();
  if (!sceneName) return "";
  const docName = SCENE_DOC_NAME_MAP[sceneName];
  if (!docName || docName === sceneName) return sceneName;
  return `${sceneName}（${docName}）`;
}

function parseDateTime(value) {
  if (!value) return 0;
  const ts = Date.parse(String(value));
  return Number.isFinite(ts) ? ts : 0;
}

function normalizeScene(raw, index = 0) {
  if (!raw || typeof raw !== "object") return null;
  const sceneId = String(raw.scene_id ?? raw.sceneId ?? raw.id ?? "").trim();
  const fallbackName = sceneId || `未命名场景_${index + 1}`;
  const name = String(raw.name ?? raw.scene_name ?? raw.sceneName ?? fallbackName).trim() || fallbackName;
  const versionRaw = raw.version ?? raw.scene_version ?? raw.sceneVersion ?? 1;
  const versionNum = Number(versionRaw);
  const version = Number.isFinite(versionNum) ? String(Math.trunc(versionNum)) : String(versionRaw || "1");
  const description = String(raw.description ?? raw.desc ?? "").trim();
  return {
    ...raw,
    scene_id: sceneId || `scene_fallback_${index + 1}`,
    name,
    version,
    description,
  };
}

function normalizeSceneList(payload) {
  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.scenes)
      ? payload.scenes
      : Array.isArray(payload?.items)
        ? payload.items
        : [];
  const result = [];
  const seen = new Set();
  for (let i = 0; i < rows.length; i += 1) {
    const scene = normalizeScene(rows[i], i);
    if (!scene) continue;
    if (seen.has(scene.scene_id)) continue;
    seen.add(scene.scene_id);
    result.push(scene);
  }
  return result;
}

function getCurrentSceneName() {
  const scene = state.scenes.find((item) => item.scene_id === state.currentSceneId);
  return scene?.name || "";
}

function getIntentTemplateEntriesForCurrentScene() {
  const matrix = Array.isArray(state.currentScenePlaybook?.question_matrix)
    ? state.currentScenePlaybook.question_matrix
    : [];
  if (state.currentScenePlaybook?.scene_id === state.currentSceneId && matrix.length) {
    return matrix
      .map((item, idx) => ({
        intent: String(item?.question || "").trim(),
        preset_key: String(item?.preset_key || "").trim(),
        title: String(item?.title || `问题${idx + 1}`).trim(),
      }))
      .filter((item) => item.intent);
  }

  const detailGoals = Array.isArray(state.currentSceneDetail?.sample_goals)
    ? state.currentSceneDetail.sample_goals.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  if (state.currentSceneDetail?.scene_id === state.currentSceneId && detailGoals.length) {
    return detailGoals.map((intent, idx) => ({ intent, preset_key: "", title: `问题${idx + 1}` }));
  }
  const scene = state.scenes.find((item) => item.scene_id === state.currentSceneId);
  const sceneGoals = Array.isArray(scene?.sample_goals)
    ? scene.sample_goals.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  if (sceneGoals.length) return sceneGoals.map((intent, idx) => ({ intent, preset_key: "", title: `问题${idx + 1}` }));
  const sceneName = getCurrentSceneName();
  const fallback = SCENE_INTENT_TEMPLATES[sceneName] || SCENE_INTENT_TEMPLATES.default;
  return fallback.map((intent, idx) => ({ intent, preset_key: "", title: `问题${idx + 1}` }));
}

function getIntentTemplatesForCurrentScene() {
  return getIntentTemplateEntriesForCurrentScene().map((item) => item.intent);
}

function renderIntentTemplates() {
  const btn = el("toggleIntentTemplatesBtn");
  const wrap = el("intentTemplatesWrap");
  const list = el("intentTemplateButtons");
  if (btn) btn.textContent = state.intentTemplatesCollapsed ? "预制问题" : "收起问题";
  if (wrap) wrap.hidden = state.intentTemplatesCollapsed;
  if (!list) return;
  const templates = getIntentTemplateEntriesForCurrentScene();
  list.innerHTML = templates
    .map(
      (item, idx) =>
        `<button class="secondary intent-template-btn" data-intent="${escapeHtml(item.intent)}" data-preset-key="${escapeHtml(item.preset_key)}">问题${idx + 1}：${escapeHtml(item.intent)}</button>`,
    )
    .join("");
}

function fillIntentInputs(intent) {
  const text = String(intent || "").trim();
  if (!text) return;
  if (el("queryIntentInput")) el("queryIntentInput").value = text;
  if (el("goalInput")) el("goalInput").value = text;
}

function getSceneDraft(sceneId) {
  const key = String(sceneId || "").trim();
  if (!key) return null;
  return state.llmDraftBySceneId[key] || null;
}

function persistSceneDraft(sceneId, draft) {
  const key = String(sceneId || "").trim();
  if (!key || !draft || typeof draft !== "object") return;
  if (state.llmDraftSaveTimers[key]) clearTimeout(state.llmDraftSaveTimers[key]);
  state.llmDraftSaveTimers[key] = setTimeout(() => {
    api(`/api/v1/scene-builder/scenes/${key}/draft`, {
      method: "PUT",
      body: JSON.stringify({ recommendation: draft }),
    }).catch((error) => console.warn("save scene draft failed", error));
  }, 250);
}

function setSceneDraft(sceneId, draft, options = {}) {
  const key = String(sceneId || "").trim();
  if (!key) return;
  if (draft && typeof draft === "object") {
    state.llmDraftBySceneId[key] = draft;
  } else {
    delete state.llmDraftBySceneId[key];
  }
  if (state.currentSceneId === key) {
    state.currentLlmAgentDraft = draft || null;
  }
  if (options.persist !== false) {
    persistSceneDraft(key, draft);
  }
}

function pickBestSessionForScene(sceneId) {
  const candidates = state.sessions.filter((item) => item.scene_id === sceneId);
  if (!candidates.length) return null;
  const sorted = [...candidates].sort((a, b) => {
    const aTs = parseDateTime(a.updated_at) || parseDateTime(a.created_at);
    const bTs = parseDateTime(b.updated_at) || parseDateTime(b.created_at);
    return bTs - aTs;
  });
  return sorted[0];
}

async function setCurrentSession(session, { loadThread = true } = {}) {
  state.currentSession = session || null;
  if (session?.scene_id) state.currentSceneId = session.scene_id;
  state.restoreSessionId = session?.session_id || "";
  clearReportStateViews();
  renderSessions();
  renderScenes();
  renderSessionHeader();
  persistUiState();
  if (loadThread && session?.session_id) {
    el("bridgeView").textContent = pretty(await api(`/api/v1/analysis/sessions/${session.session_id}/thread-context`));
  }
  if (session?.session_id) {
    await loadLatestSqlResultForCurrentSession({ force: true });
    await loadReportStateForCurrentSession({ silent: true }).catch((error) => {
      console.warn("load report state failed", error);
    });
  }
}

async function ensureSessionForCurrentScene({ intent = "", createIfMissing = true } = {}) {
  const sceneId = state.currentSceneId;
  if (!sceneId) return null;
  const goal = String(intent || "").trim() || el("queryIntentInput")?.value.trim() || el("goalInput")?.value.trim();
  let matched = pickBestSessionForScene(sceneId);
  if (matched?.session_id) {
    try {
      matched = await api(`/api/v1/analysis/sessions/${matched.session_id}`);
    } catch (error) {
      if (!isSessionNotFoundError(error)) throw error;
      dropSessionFromState(matched.session_id);
      matched = null;
    }
  }
  if (!matched && createIfMissing) {
    matched = await api("/api/v1/analysis/sessions", {
      method: "POST",
      body: JSON.stringify({
        scene_id: sceneId,
        global_goal: goal || "围绕当前场景进行分析",
      }),
    });
    state.sessions = [matched, ...state.sessions];
  }
  if (!matched) return null;
  await setCurrentSession(matched);
  return matched;
}

async function createSessionForCurrentScene({ intent = "" } = {}) {
  const sceneId = state.currentSceneId;
  if (!sceneId) return null;
  const goal = String(intent || "").trim() || getCurrentIntentText();
  const session = await api("/api/v1/analysis/sessions", {
    method: "POST",
    body: JSON.stringify({
      scene_id: sceneId,
      global_goal: goal || "围绕当前场景进行分析",
    }),
  });
  state.sessions = [session, ...state.sessions.filter((item) => item.session_id !== session.session_id)];
  await setCurrentSession(session, { loadThread: false });
  return session;
}

function syncBackendBase() {
  const backendInput = el("backendBase");
  if (!backendInput) return;
  const nextBase = normalizeBackendBase(backendInput.value);
  if (state.backendBase !== nextBase) {
    state.backendBase = nextBase;
    persistUiState();
  }
}

function normalizeBackendBase(rawValue) {
  let base = String(rawValue || "")
    .trim()
    .replace(/\/+$/, "");
  if (!base) return window.location.origin;
  if (base.startsWith("/")) return `${window.location.origin}${base}`.replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(base)) {
    base = `http://${base}`;
  }
  return base.replace(/\/+$/, "");
}

function setBackendBaseInput(base) {
  const backendInput = el("backendBase");
  if (backendInput) backendInput.value = base;
  state.backendBase = base;
}

function buildApiUrl(base, path) {
  return `${base}${path}`;
}

function upsertAgentWait(agentKey, label, active) {
  if (!state.agentWait[agentKey]) {
    state.agentWait[agentKey] = null;
  }
  if (active) {
    state.agentWait[agentKey] = {
      label,
      startedAt: Date.now(),
    };
  } else {
    state.agentWait[agentKey] = null;
  }
  renderAgentWaitHint();
}

function startAgentWaitTimerIfNeeded() {
  if (state.agentWaitTimerId) return;
  state.agentWaitTimerId = window.setInterval(() => {
    renderAgentWaitHint();
  }, 1000);
}

function stopAgentWaitTimerIfIdle() {
  const hasPending = Object.values(state.agentWait).some((item) => item && item.startedAt);
  if (hasPending) return;
  if (state.agentWaitTimerId) {
    window.clearInterval(state.agentWaitTimerId);
    state.agentWaitTimerId = 0;
  }
}

function renderAgentWaitHint() {
  const hintEl = el("agentWaitHint");
  const recommendBtn = el("llmRecommendBtn");
  const sqlResultBtn = el("llmSqlResultBtn");
  const runQueryBtn = el("runQueryBtn");
  const waitItems = [];
  const recommendWait = state.agentWait.recommend;
  const sqlResultWait = state.agentWait.sqlResult;
  if (recommendWait?.startedAt) {
    const sec = Math.max(1, Math.floor((Date.now() - recommendWait.startedAt) / 1000));
    waitItems.push(`推荐 Agent 正在返回，已等待 ${sec}s`);
  }
  if (sqlResultWait?.startedAt) {
    const sec = Math.max(1, Math.floor((Date.now() - sqlResultWait.startedAt) / 1000));
    waitItems.push(`SQL 结果 Agent 正在返回，已等待 ${sec}s`);
  }
  if (recommendBtn) {
    const busy = Boolean(recommendWait?.startedAt);
    recommendBtn.disabled = busy;
    recommendBtn.textContent = busy ? "推荐处理中..." : "推荐";
  }
  if (sqlResultBtn) {
    const busy = Boolean(sqlResultWait?.startedAt);
    sqlResultBtn.disabled = busy;
    sqlResultBtn.textContent = busy ? "SQL处理中..." : "SQL与结果";
  }
  if (runQueryBtn) {
    const busy = Boolean(sqlResultWait?.startedAt);
    runQueryBtn.disabled = busy;
    runQueryBtn.textContent = busy ? "处理中..." : "生成并执行";
  }
  if (!hintEl) return;
  if (!waitItems.length) {
    hintEl.hidden = true;
    hintEl.textContent = "";
    stopAgentWaitTimerIfIdle();
    return;
  }
  hintEl.hidden = false;
  hintEl.textContent = waitItems.join("；");
  startAgentWaitTimerIfNeeded();
}

async function withAgentWait(agentKey, label, fn) {
  upsertAgentWait(agentKey, label, true);
  try {
    return await fn();
  } finally {
    upsertAgentWait(agentKey, label, false);
  }
}

async function api(path, options = {}) {
  syncBackendBase();
  const requestOptions = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  };
  const primaryBase = state.backendBase;
  const primaryUrl = buildApiUrl(primaryBase, path);
  let response;
  try {
    response = await fetch(primaryUrl, requestOptions);
  } catch (error) {
    const detail = error?.message || String(error);
    throw new Error(`请求接口失败：${primaryUrl}。请检查后端服务是否可达、接口是否超时或网络是否异常。${detail}`);
  }
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      const payload = JSON.parse(text);
      detail = payload?.detail || text;
    } catch (_error) {
      detail = text;
    }
    const error = new Error(`${response.status} ${detail}`);
    error.status = response.status;
    error.detail = detail;
    error.body = text;
    error.path = path;
    error.url = primaryUrl;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) return response.json();
  return response.text();
}

function isSessionNotFoundError(error) {
  const detail = String(error?.detail || error?.body || error?.message || "");
  return Number(error?.status) === 404 && detail.includes("session not found");
}

function getCurrentIntentText() {
  return (
    (el("queryIntentInput")?.value || "").trim() ||
    (el("goalInput")?.value || "").trim() ||
    state.currentSession?.global_goal ||
    "围绕当前场景进行分析"
  );
}

function setDeliveryActionHint(message) {
  const hint = el("deliveryActionHint");
  if (hint) hint.textContent = message || "";
}

function clearReportStateViews() {
  state.currentDeck = null;
  state.currentArtifact = null;
  state.currentSlide = null;
  state.currentReportState = null;
  if (el("slideView")) el("slideView").textContent = "";
  if (el("deckView")) el("deckView").textContent = "";
  renderSlidePreview(null);
  syncArtifactDownload();
}

function clearCurrentSessionState() {
  state.currentSession = null;
  state.restoreSessionId = "";
  clearReportStateViews();
  renderSessions();
  renderSessionHeader();
  renderQueryHistory();
  persistUiState();
}

function dropSessionFromState(sessionId) {
  const id = String(sessionId || "").trim();
  if (id) {
    state.sessions = state.sessions.filter((item) => item.session_id !== id);
  }
  if (!id || state.currentSession?.session_id === id) {
    clearCurrentSessionState();
  } else {
    renderSessions();
  }
}

async function withSessionRecovery(action, { createIfMissing = false, intent = "" } = {}) {
  ensureSession();
  try {
    return await action(state.currentSession);
  } catch (error) {
    if (!isSessionNotFoundError(error)) throw error;
    const missingSessionId = state.currentSession?.session_id;
    dropSessionFromState(missingSessionId);
    await refreshSessions();
    if (!createIfMissing) {
      throw new Error("当前会话已失效，请重新选择提问历史后再操作");
    }
    let session = state.currentSession;
    if (!session) {
      session = await ensureSessionForCurrentScene({
        intent: intent || getCurrentIntentText(),
        createIfMissing: true,
      });
    }
    if (!session) {
      throw new Error("当前会话已失效，请重新创建会话后再操作");
    }
    return action(session);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function splitAliases(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatQueryPlanView(queryPlan) {
  if (!queryPlan) return "";
  const lines = [];
  lines.push(`意图: ${queryPlan.intent || "-"}`);
  lines.push(`QueryPlan ID: ${queryPlan.query_plan_id || "-"}`);
  lines.push(`指标: ${(queryPlan.metrics || []).join(", ") || "-"}`);
  lines.push(`维度: ${(queryPlan.dimensions || []).join(", ") || "-"}`);
  lines.push(`时间范围: ${queryPlan.time_window || "-"}`);
  const filters = Array.isArray(queryPlan.filters) ? queryPlan.filters : [];
  if (filters.length) {
    lines.push("过滤条件:");
    for (const item of filters) {
      lines.push(`- ${item?.field || "-"} ${item?.operator || "="} ${item?.value || "-"}`);
    }
  } else {
    lines.push("过滤条件: -");
  }
  if (Array.isArray(queryPlan.risk_notes) && queryPlan.risk_notes.length) {
    lines.push("检查提示:");
    for (const note of queryPlan.risk_notes) {
      lines.push(`- ${note}`);
    }
  }
  return lines.join("\n");
}

function formatQueryRunView(queryRun) {
  if (!queryRun) return "";
  const sql = String(queryRun.sql || "").trim();
  const lines = [];
  if (sql) {
    lines.push(sql.endsWith(";") ? sql : `${sql};`);
  } else {
    lines.push("-- 暂无可执行 SQL");
  }
  lines.push("");
  lines.push(`# 状态: ${queryRun.status || "-"}`);
  lines.push(`# 返回行数: ${Number(queryRun.rows_count || 0)}`);
  lines.push(`# 耗时(ms): ${queryRun.duration_ms ?? "-"}`);
  if (queryRun.sql_explanation) {
    lines.push(`# 说明: ${queryRun.sql_explanation}`);
  }
  if (Array.isArray(queryRun.insight_summary) && queryRun.insight_summary.length) {
    lines.push("# 结果摘要:");
    for (const summary of queryRun.insight_summary) {
      lines.push(`# - ${summary}`);
    }
  }
  return lines.join("\n");
}

function renderSimpleTable(columns, rows) {
  if (!rows?.length) return "";
  return (
    `<table class="data-table"><thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows
      .map((row) => `<tr>${columns.map((column) => `<td>${escapeHtml(row[column] ?? "")}</td>`).join("")}</tr>`)
      .join("")}</tbody></table>`
  );
}

function renderFixedTableOpen(classNames, widths) {
  const columnCount = Math.max(1, Array.isArray(widths) ? widths.length : 0);
  const percent = 100 / columnCount;
  const classes = ["data-table", ...(classNames || []).filter(Boolean)].join(" ");
  const colgroup = `<colgroup>${Array.from({ length: columnCount }, () => `<col style="width:${percent}%" />`).join("")}</colgroup>`;
  return `<table class="${classes}" style="width:100%; min-width:100%; table-layout:fixed;">${colgroup}`;
}

function renderSceneRelationsTable(relations) {
  if (!relations?.length) return "";
  const columns = ["left_table", "left_field", "right_table", "right_field", "join_type", "note"];
  const header = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("") + "<th>操作</th>";
  const body = relations
    .map((row) => {
      const relationId = escapeHtml(row.relation_id || "");
      const cells = columns.map((column) => `<td>${escapeHtml(row[column] ?? "")}</td>`).join("");
      return `<tr class="previewable-row" data-preview-title="已配置关系详情" data-preview-payload="${encodeRowPayload(row)}">${cells}<td><button class="secondary relation-delete-btn" data-relation-id="${relationId}">删除</button></td></tr>`;
    })
    .join("");
  return `${renderFixedTableOpen(["sticky-actions-1", "uniform-list-table"], [88, 88, 88, 88, 72, 88, 52])}<thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderSelectedDraftTable(kind, rows) {
  if (!rows?.length) {
    return `<div class="scene-summary-empty muted">暂无已选${kind === "field" ? "字段" : "关系"}</div>`;
  }
  if (kind === "field") {
    const columns = ["semantic", "table.field", "role", "required", "confidence", "操作"];
    const header = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
    const body = rows
      .map((item) => {
        const candidateId = escapeHtml(item.candidate_id || "");
        const requiredText = item.required ? "true" : "false";
        return `<tr class="previewable-row" data-preview-title="已选字段详情" data-preview-payload="${encodeRowPayload(item)}">
          <td>${escapeHtml(item.semantic_name || "")}</td>
          <td>${escapeHtml(`${item.table_name || ""}.${item.field_name || ""}`)}</td>
          <td>${escapeHtml(item.role || "")}</td>
          <td>${escapeHtml(requiredText)}</td>
          <td>${escapeHtml(formatConfidence(item.confidence))}</td>
          <td><button class="secondary selected-draft-remove-btn" data-kind="field" data-candidate-id="${candidateId}">剔除</button></td>
        </tr>`;
      })
      .join("");
    return `${renderFixedTableOpen(["selected-draft-table", "selected-draft-field-table", "sticky-actions-1", "uniform-list-table"], [96, 96, 72, 72, 72, 52])}<thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
  }
  const columns = ["relation", "join", "cardinality", "required", "confidence", "操作"];
  const header = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
  const body = rows
    .map((item) => {
      const candidateId = escapeHtml(item.candidate_id || "");
      const requiredText = item.required ? "true" : "false";
      return `<tr class="previewable-row" data-preview-title="已选关系详情" data-preview-payload="${encodeRowPayload(item)}">
        <td>${escapeHtml(`${item.left_table || ""}.${item.left_field || ""} = ${item.right_table || ""}.${item.right_field || ""}`)}</td>
        <td>${escapeHtml(item.join_type || "")}</td>
        <td>${escapeHtml(item.cardinality || "")}</td>
        <td>${escapeHtml(requiredText)}</td>
        <td>${escapeHtml(formatConfidence(item.confidence))}</td>
        <td><button class="secondary selected-draft-remove-btn" data-kind="relation" data-candidate-id="${candidateId}">剔除</button></td>
      </tr>`;
    })
    .join("");
  return `${renderFixedTableOpen(["selected-draft-table", "selected-draft-relation-table", "sticky-actions-1", "uniform-list-table"], [96, 72, 72, 72, 72, 52])}<thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
}

function matchesSemanticCacheKeyword(row, keyword) {
  const key = String(keyword || "").trim().toLowerCase();
  if (!key) return true;
  const aliases = Array.isArray(row?.aliases) ? row.aliases.join(",") : "";
  const source = [
    row?.semantic_name || "",
    row?.table_name || "",
    row?.field_name || "",
    aliases,
    row?.semantic_definition || "",
    row?.role || "",
  ]
    .join(" ")
    .toLowerCase();
  return source.includes(key);
}

function dedupeSemanticFields(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const deduped = [];
  const seenByCacheId = new Set();
  const seenByPhysicalField = new Set();
  let duplicateCount = 0;
  for (const row of list) {
    if (!row || typeof row !== "object") continue;
    const cacheId = String(row.cache_id || "").trim();
    if (cacheId && seenByCacheId.has(cacheId)) {
      duplicateCount += 1;
      continue;
    }
    if (cacheId) seenByCacheId.add(cacheId);
    const physicalKey = `${String(row.table_name || "").trim().toLowerCase()}|${String(row.field_name || "")
      .trim()
      .toLowerCase()}`;
    if (physicalKey !== "|" && seenByPhysicalField.has(physicalKey)) {
      duplicateCount += 1;
      continue;
    }
    if (physicalKey !== "|") seenByPhysicalField.add(physicalKey);
    deduped.push(row);
  }
  return { rows: deduped, duplicateCount };
}

function renderSemanticCacheTable(rows) {
  const filtered = (rows || []).filter((row) => matchesSemanticCacheKeyword(row, state.semanticCacheKeyword));
  if (!filtered.length) return '<div class="muted">暂无字段，请在上方表单添加。</div>';
  const header = [
    "semantic_name",
    "table_name",
    "field_name",
    "role",
    "enabled",
    "aggregation",
    "unit",
    "aliases",
    "er_path",
    "semantic_definition",
    "启用/禁用",
    "删除",
  ];
  const body = filtered
    .map((row) => {
      const cacheId = escapeHtml(row.cache_id || "");
      const aliases = escapeHtml((row.aliases || []).join(", "));
      const enabled = row.enabled !== false ? "1" : "0";
      return `<tr class="semantic-cache-row previewable-row" data-cache-id="${cacheId}" data-preview-title="字段配置详情" data-preview-payload="${encodeRowPayload(row)}">
        <td>${escapeHtml(row.semantic_name ?? "")}</td>
        <td>${escapeHtml(row.table_name ?? "")}</td>
        <td>${escapeHtml(row.field_name ?? "")}</td>
        <td>${escapeHtml(row.role ?? "")}</td>
        <td>${escapeHtml(enabled)}</td>
        <td>${escapeHtml(row.aggregation ?? "")}</td>
        <td>${escapeHtml(row.unit ?? "")}</td>
        <td>${aliases}</td>
        <td>${escapeHtml(row.er_path ?? "")}</td>
        <td>${escapeHtml(row.semantic_definition ?? "")}</td>
        <td><button class="secondary semantic-toggle-btn" data-cache-id="${cacheId}" data-enabled="${enabled}">${enabled === "1" ? "禁用" : "启用"}</button></td>
        <td><button class="secondary semantic-delete-btn" data-cache-id="${cacheId}">删除</button></td>
      </tr>`;
    })
    .join("");
  return `${renderFixedTableOpen(["semantic-cache-table", "sticky-actions-2", "uniform-list-table"], [72, 72, 72, 60, 52, 52, 52, 52, 52, 72, 52, 52])}<thead><tr>${header.map((item) => `<th>${escapeHtml(item)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`;
}

function normalizeCandidateId(prefix, item, index) {
  const raw = [
    prefix,
    item?.table_name || item?.left_table || "",
    item?.field_name || item?.left_field || "",
    item?.semantic_name || item?.right_table || "",
    item?.right_field || "",
    item?.join_type || "",
    index,
  ]
    .join("_")
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${prefix}_${raw || index}`;
}

function ensureDraftCandidateMeta(draft) {
  if (!draft || typeof draft !== "object") return draft;
  const candidates = draft.candidates;
  if (!candidates || typeof candidates !== "object") return draft;

  const fields = Array.isArray(candidates.fields) ? candidates.fields : [];
  for (let i = 0; i < fields.length; i += 1) {
    const item = fields[i];
    if (!item || typeof item !== "object") continue;
    if (!item.candidate_id) item.candidate_id = normalizeCandidateId("fld", item, i);
    if (typeof item.selected !== "boolean") item.selected = item.enabled !== false;
  }

  const relations = Array.isArray(candidates.relations) ? candidates.relations : [];
  for (let i = 0; i < relations.length; i += 1) {
    const item = relations[i];
    if (!item || typeof item !== "object") continue;
    if (!item.candidate_id) item.candidate_id = normalizeCandidateId("rel", item, i);
    if (typeof item.selected !== "boolean") item.selected = true;
  }
  return draft;
}

function formatConfidence(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(2);
}

function formatUnixSeconds(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return "-";
  return new Date(num * 1000).toLocaleString();
}

function encodeRowPayload(value) {
  return escapeHtml(JSON.stringify(value || {}));
}

function extractTableCandidateNames(rawTables) {
  if (!Array.isArray(rawTables)) return [];
  return rawTables
    .map((item) => {
      if (typeof item === "string") return item.trim();
      if (!item || typeof item !== "object") return "";
      return String(item.table_name || item.name || item.table || "").trim();
    })
    .filter(Boolean);
}

function renderLlmCacheStatus() {
  const statusNode = el("llmCacheStatus");
  if (!statusNode) return;
  const status = state.llmCacheStatus;
  if (!status || typeof status !== "object") {
    statusNode.textContent = "数据库缓存状态：未加载";
    return;
  }
  const tables = Number(status.schema_tables || 0);
  const fks = Number(status.foreign_keys || 0);
  const age = status.cache_age_seconds ?? "-";
  const fetchedAt = formatUnixSeconds(status.fetched_at);
  const refreshAt = formatUnixSeconds(status.last_refresh_at);
  const refreshError = status.last_refresh_error ? `；刷新错误：${status.last_refresh_error}` : "";
  statusNode.textContent = `数据库缓存：表 ${tables}，外键 ${fks}，缓存龄 ${age}s，缓存时间 ${fetchedAt}，最近刷新 ${refreshAt}${refreshError}`;
}

function renderLlmCandidateTable(kind, rows) {
  if (!rows?.length) return `<div class="scene-summary-empty muted">暂无${kind === "field" ? "字段" : "关系"}候选</div>`;
  if (kind === "field") {
    return (
      `${renderFixedTableOpen(["llm-candidate-table", "llm-candidate-field-table", "uniform-list-table"], [42, 96, 96, 72, 72, 72, 96])}<thead><tr><th>导入</th><th>semantic</th><th>table.field</th><th>role</th><th>required</th><th>confidence</th><th>reason</th></tr></thead><tbody>` +
      rows
        .map((item) => {
          const checked = item.selected !== false ? "checked" : "";
          const requiredText = item.required ? "true" : "false";
          return `<tr class="previewable-row" data-preview-title="字段候选详情" data-preview-payload="${encodeRowPayload(item)}">
            <td><input class="llm-candidate-check" type="checkbox" data-kind="field" data-candidate-id="${escapeHtml(item.candidate_id || "")}" ${checked} /></td>
            <td>${escapeHtml(item.semantic_name || "")}</td>
            <td>${escapeHtml(`${item.table_name || ""}.${item.field_name || ""}`)}</td>
            <td>${escapeHtml(item.role || "")}</td>
            <td>${escapeHtml(requiredText)}</td>
            <td>${escapeHtml(formatConfidence(item.confidence))}</td>
            <td>${escapeHtml(item.reason || item.description || "")}</td>
          </tr>`;
        })
        .join("") +
      "</tbody></table>"
    );
  }
  return (
    `${renderFixedTableOpen(["llm-candidate-table", "llm-candidate-relation-table", "uniform-list-table"], [42, 96, 72, 72, 72, 72, 96])}<thead><tr><th>导入</th><th>relation</th><th>join</th><th>cardinality</th><th>required</th><th>confidence</th><th>reason</th></tr></thead><tbody>` +
    rows
      .map((item) => {
        const checked = item.selected !== false ? "checked" : "";
        const requiredText = item.required ? "true" : "false";
        return `<tr class="previewable-row" data-preview-title="关系候选详情" data-preview-payload="${encodeRowPayload(item)}">
          <td><input class="llm-candidate-check" type="checkbox" data-kind="relation" data-candidate-id="${escapeHtml(item.candidate_id || "")}" ${checked} /></td>
          <td>${escapeHtml(`${item.left_table || ""}.${item.left_field || ""} = ${item.right_table || ""}.${item.right_field || ""}`)}</td>
          <td>${escapeHtml(item.join_type || "")}</td>
          <td>${escapeHtml(item.cardinality || "")}</td>
          <td>${escapeHtml(requiredText)}</td>
          <td>${escapeHtml(formatConfidence(item.confidence))}</td>
          <td>${escapeHtml(item.reason || item.note || "")}</td>
        </tr>`;
      })
      .join("") +
    "</tbody></table>"
  );
}

function renderLlmCandidateSelector() {
  const summary = el("llmCandidateSummary");
  const fieldsWrap = el("llmCandidateFieldsWrap");
  const relationsWrap = el("llmCandidateRelationsWrap");
  const tableCandidates = el("llmTableCandidates");
  if (!summary || !fieldsWrap || !relationsWrap || !tableCandidates) return;

  const draft = ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  const candidates = draft?.candidates;
  if (!candidates || typeof candidates !== "object") {
    summary.textContent = "暂无候选，请先点击“推荐”生成候选列表。";
    tableCandidates.textContent = "";
    fieldsWrap.innerHTML = "";
    relationsWrap.innerHTML = "";
    return;
  }

  const tables = extractTableCandidateNames(candidates.tables);
  const fields = Array.isArray(candidates.fields) ? candidates.fields : [];
  const relations = Array.isArray(candidates.relations) ? candidates.relations : [];
  const selectedFields = fields.filter((item) => item?.selected !== false).length;
  const selectedRelations = relations.filter((item) => item?.selected !== false).length;
  tableCandidates.textContent = tables.length ? `LLM 表候选：${tables.join("、")}` : "LLM 表候选：未返回";
  summary.textContent = `候选导入状态：字段 ${selectedFields}/${fields.length}，关系 ${selectedRelations}/${relations.length}。应用时仅导入已勾选项。`;
  fieldsWrap.innerHTML = renderLlmCandidateTable("field", fields);
  relationsWrap.innerHTML = renderLlmCandidateTable("relation", relations);
}

async function refreshLlmCacheStatus() {
  state.llmCacheStatus = await api("/api/v1/llm-agent/cache");
  renderLlmCacheStatus();
}

async function refreshDbCacheFromMysql() {
  state.llmCacheStatus = await api("/api/v1/llm-agent/cache/refresh", { method: "POST" });
  renderLlmCacheStatus();
}

function setLlmCandidateSelected(kind, candidateId, selected) {
  const draft = requireCurrentRecommendation();
  const listKey = kind === "field" ? "fields" : "relations";
  const list = Array.isArray(draft?.candidates?.[listKey]) ? draft.candidates[listKey] : [];
  let changed = false;
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i];
    if (!item || typeof item !== "object") continue;
    if (!item.candidate_id) item.candidate_id = normalizeCandidateId(kind === "field" ? "fld" : "rel", item, i);
    if (item.candidate_id === candidateId) {
      item.selected = selected;
      changed = true;
      break;
    }
  }
  if (!changed) return;
  state.currentLlmAgentDraft = draft;
  setSceneDraft(state.currentSceneId, draft);
  renderSceneConfig();
}

function setAllLlmCandidates(kind, selected) {
  const draft = requireCurrentRecommendation();
  const listKey = kind === "field" ? "fields" : "relations";
  const list = Array.isArray(draft?.candidates?.[listKey]) ? draft.candidates[listKey] : [];
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i];
    if (!item || typeof item !== "object") continue;
    if (!item.candidate_id) item.candidate_id = normalizeCandidateId(kind === "field" ? "fld" : "rel", item, i);
    item.selected = selected;
  }
  state.currentLlmAgentDraft = draft;
  setSceneDraft(state.currentSceneId, draft);
  renderSceneConfig();
}

function renderScenes() {
  const pickerList = el("scenePickerList");
  const overviewList = el("scenesList");
  const currentHint = el("currentSceneHint");
  const createSessionHint = el("createSessionSceneHint");
  if (pickerList) pickerList.innerHTML = "";
  if (overviewList) overviewList.innerHTML = "";

  const currentScene = state.scenes.find((scene) => scene.scene_id === state.currentSceneId) || null;
  if (currentHint) {
    currentHint.textContent = currentScene
      ? `当前：${formatSceneName(currentScene.name)} · v${currentScene.version}`
      : "";
  }
  if (createSessionHint) {
    createSessionHint.textContent = currentScene
      ? `当前场景：${formatSceneName(currentScene.name)} · v${currentScene.version}`
      : "当前场景：未选择";
  }

  const buildSceneItem = (scene) => {
    const sceneId = String(scene?.scene_id || "").trim();
    const sceneName = String(scene?.name || "").trim();
    const sceneVersion = String(scene?.version ?? "-");
    const sceneDesc = String(scene?.description || "").trim();
    const row = document.createElement("div");
    row.className = `scene-row ${sceneId === state.currentSceneId ? "active" : ""}`;

    const mainBtn = document.createElement("button");
    mainBtn.className = "scene-main-btn";
    mainBtn.innerHTML =
      `<span class="scene-title">${escapeHtml(formatSceneName(sceneName))}</span>` +
      `<span class="scene-meta">v${escapeHtml(sceneVersion)}${sceneDesc ? ` · ${escapeHtml(sceneDesc)}` : ""}</span>`;
    mainBtn.onclick = () => run(async () => {
      if (!sceneId) return;
      state.currentSceneId = sceneId;
      state.selectedPresetKey = "";
      state.selectedPresetQuestion = "";
      clearCurrentSessionState();
      clearQueryResultViews();
      renderScenes();
      await refreshSceneDetail();
      await ensureSessionForCurrentScene({ createIfMissing: false });
      await refreshQueryHistory();
    });
    row.appendChild(mainBtn);

    const deleteBtn = document.createElement("button");
    const isPreset = sceneId.startsWith("scene_prd_");
    deleteBtn.className = "scene-delete-btn";
    deleteBtn.textContent = "删除";
    deleteBtn.disabled = isPreset || !sceneId;
    if (isPreset) deleteBtn.title = "预置场景不可删除";
    deleteBtn.onclick = (event) => {
      event.stopPropagation();
      run(async () => {
        await deleteScene(scene);
      });
    };
    row.appendChild(deleteBtn);
    return row;
  };

  for (const scene of state.scenes) {
    if (pickerList) pickerList.appendChild(buildSceneItem(scene));
    if (overviewList) overviewList.appendChild(buildSceneItem(scene));
  }
  if (!state.scenes.length) {
    const hint = document.createElement("div");
    hint.className = "scene-empty muted";
    hint.textContent = state.sceneLoadError
      ? `场景加载失败：${state.sceneLoadError}`
      : "暂无场景，请先创建场景。";
    if (pickerList) pickerList.appendChild(hint.cloneNode(true));
    if (overviewList) overviewList.appendChild(hint);
  }

  renderSceneListCollapse();
}

function renderSceneListCollapse() {
  const collapsed = state.sceneListCollapsed;
  const pickerWrap = el("scenePickerWrap");
  const overviewWrap = el("scenesOverviewWrap");
  const sidebarBtn = el("toggleScenesBtn");
  const overviewBtn = el("toggleScenesBtnOverview");
  if (pickerWrap) pickerWrap.hidden = collapsed;
  if (overviewWrap) overviewWrap.hidden = collapsed;
  if (sidebarBtn) sidebarBtn.textContent = collapsed ? "展开" : "收起";
  if (overviewBtn) overviewBtn.textContent = collapsed ? "展开" : "收起";
  renderIntentTemplates();
}

function renderCreateSceneCollapse() {
  const collapsed = state.createSceneCollapsed;
  const wrap = el("createSceneWrap");
  const btn = el("toggleCreateSceneBtn");
  if (wrap) wrap.hidden = collapsed;
  if (btn) btn.textContent = collapsed ? "展开" : "收起";
}

function renderSceneConfigCollapse() {
  const collapsed = state.sceneConfigCollapsed;
  const wrap = el("sceneConfigWrap");
  const btn = el("toggleSceneConfigBtn");
  if (wrap) wrap.hidden = collapsed;
  if (btn) btn.textContent = collapsed ? "展开" : "收起";
}

function renderSceneFieldsCardCollapse() {
  const collapsed = state.sceneFieldsCardCollapsed;
  const wrap = el("sceneFieldsCardWrap");
  const btn = el("toggleSceneFieldsCardBtn");
  if (wrap) wrap.hidden = collapsed;
  if (btn) btn.textContent = collapsed ? "展开" : "收起";
}

function renderSceneRelationsCardCollapse() {
  const collapsed = state.sceneRelationsCardCollapsed;
  const wrap = el("sceneRelationsCardWrap");
  const btn = el("toggleSceneRelationsCardBtn");
  if (wrap) wrap.hidden = collapsed;
  if (btn) btn.textContent = collapsed ? "展开" : "收起";
}

function syncSceneAdvancedFieldState() {
  const fieldAdvancedIds = ["fieldSemanticDefinition", "fieldUnit", "fieldAggregation", "fieldErPath"];
  const relationAdvancedIds = ["relationNote"];

  const apply = (ids, open, btnId, openText, closedText) => {
    for (const id of ids) {
      const node = el(id);
      if (node instanceof HTMLInputElement || node instanceof HTMLSelectElement || node instanceof HTMLTextAreaElement) {
        node.disabled = !open || !state.currentSceneDetail;
        const wrapper = node.closest(".form-field-advanced");
        if (wrapper instanceof HTMLElement) {
          wrapper.classList.toggle("is-disabled", !open || !state.currentSceneDetail);
        }
      }
    }
    const btn = el(btnId);
    if (btn) btn.textContent = open ? openText : closedText;
  };

  apply(fieldAdvancedIds, state.fieldAdvancedOpen, "toggleFieldAdvancedBtn", "收起增强字段", "展开增强字段");
  apply(
    relationAdvancedIds,
    state.relationAdvancedOpen,
    "toggleRelationAdvancedBtn",
    "收起关系说明",
    "展开关系说明",
  );
}

async function deleteScene(scene) {
  const ok = window.confirm(`确认删除场景“${formatSceneName(scene.name)}”？`);
  if (!ok) return;
  await api(`/api/v1/scenes/${scene.scene_id}`, { method: "DELETE" });
  if (state.currentSceneId === scene.scene_id) state.currentSceneId = "";
  await refreshScenes();
  await refreshSessions();
}

function renderSceneConfig() {
  const scene = state.currentSceneDetail;
  const summaryWrap = el("sceneSummaryWrap");
  const configView = el("sceneConfigView");
  const fieldsWrap = el("sceneFieldsWrap");
  const relationsWrap = el("sceneRelationsWrap");
  const sceneConfigListSummary = el("sceneConfigListSummary");
  const sceneConfigFieldsWrap = el("sceneConfigFieldsWrap");
  const sceneConfigRelationsWrap = el("sceneConfigRelationsWrap");
  const semanticCacheKeyword = el("semanticCacheKeyword");
  const semanticCacheFormHint = el("semanticCacheFormHint");
  const addFieldBtn = el("addFieldBtn");
  const cancelEditFieldBtn = el("cancelEditFieldBtn");
  if (
    !summaryWrap ||
    !configView ||
    !fieldsWrap ||
    !relationsWrap ||
    !sceneConfigListSummary ||
    !sceneConfigFieldsWrap ||
    !sceneConfigRelationsWrap
  ) {
    return;
  }

  if (!scene) {
    summaryWrap.innerHTML = "";
    configView.textContent = pretty({});
    sceneConfigListSummary.textContent = "尚未选择场景。";
    sceneConfigFieldsWrap.innerHTML = "";
    sceneConfigRelationsWrap.innerHTML = "";
    fieldsWrap.innerHTML = "";
    relationsWrap.innerHTML = "";
    if (semanticCacheKeyword) semanticCacheKeyword.value = state.semanticCacheKeyword;
    if (semanticCacheFormHint) semanticCacheFormHint.textContent = "请选择场景后新增字段。";
    syncSceneAdvancedFieldState();
    renderLlmCandidateSelector();
    renderLlmCacheStatus();
    return;
  }

  const semanticFields = Array.isArray(state.semanticCacheFields) ? state.semanticCacheFields : [];
  const dedupedSemantic = dedupeSemanticFields(semanticFields);
  const sceneFields = dedupedSemantic.rows;
  const queryableFields = semanticFields.filter((row) => row?.enabled !== false);
  const queryableDedupedFields = sceneFields.filter((row) => row?.enabled !== false);
  const summaryItems = [
    { label: "场景名称", value: formatSceneName(scene.name) || "-" },
    { label: "场景 ID", value: scene.scene_id || "-" },
    { label: "版本", value: `v${scene.version ?? "-"}` },
    { label: "缓存字段数", value: String(sceneFields.length) },
    { label: "可执行字段数", value: String(queryableDedupedFields.length) },
    { label: "关系数", value: String(scene.relations?.length || 0) },
    { label: "描述", value: scene.description || "暂无描述" },
  ];
  summaryWrap.innerHTML = summaryItems
    .map(
      (item) =>
        `<div class="scene-summary-card"><div class="scene-summary-label">${escapeHtml(item.label)}</div><div class="scene-summary-value">${escapeHtml(item.value)}</div></div>`,
    )
    .join("");

  configView.textContent = pretty({
    scene_id: scene.scene_id,
    name: formatSceneName(scene.name),
    description: scene.description,
    version: scene.version,
    fields_count: sceneFields.length,
    queryable_fields_count: queryableDedupedFields.length,
    relations_count: scene.relations?.length || 0,
    duplicate_fields_hidden: dedupedSemantic.duplicateCount,
  });
  if (state.currentLlmAgentDraft) ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  renderLlmCandidateSelector();
  renderLlmCacheStatus();

  const sceneRelations = Array.isArray(scene.relations) ? scene.relations : [];
  const recommendation = state.currentLlmAgentDraft ? ensureDraftCandidateMeta(state.currentLlmAgentDraft) : null;
  const selectedDraftFields = Array.isArray(recommendation?.candidates?.fields)
    ? recommendation.candidates.fields.filter((item) => item?.selected !== false)
    : [];
  const selectedDraftRelations = Array.isArray(recommendation?.candidates?.relations)
    ? recommendation.candidates.relations.filter((item) => item?.selected !== false)
    : [];
  if (recommendation) {
    sceneConfigListSummary.textContent =
      `当前已选字段 ${selectedDraftFields.length}，已选关系 ${selectedDraftRelations.length}。点击“剔除”可从本次已选择列表中移除。`;
  } else {
    sceneConfigListSummary.textContent =
      dedupedSemantic.duplicateCount > 0
        ? `尚未生成推荐结果。当前场景字段 ${sceneFields.length}（已隐藏重复 ${dedupedSemantic.duplicateCount}），启用字段 ${queryableDedupedFields.length}，关系 ${sceneRelations.length}。`
        : `尚未生成推荐结果。当前场景字段 ${sceneFields.length}，启用字段 ${queryableDedupedFields.length}，关系 ${sceneRelations.length}。`;
  }
  sceneConfigFieldsWrap.innerHTML = renderSelectedDraftTable("field", selectedDraftFields);
  sceneConfigRelationsWrap.innerHTML = renderSelectedDraftTable("relation", selectedDraftRelations);

  if (semanticCacheKeyword) semanticCacheKeyword.value = state.semanticCacheKeyword;
  fieldsWrap.innerHTML = renderSemanticCacheTable(sceneFields);
  relationsWrap.innerHTML = renderSceneRelationsTable(sceneRelations);
  if (addFieldBtn) addFieldBtn.textContent = state.editingSemanticCacheId ? "保存字段" : "新增字段";
  if (cancelEditFieldBtn) cancelEditFieldBtn.hidden = !state.editingSemanticCacheId;
  syncSceneAdvancedFieldState();
  if (semanticCacheFormHint) {
    const modeText = state.editingSemanticCacheId
      ? `编辑模式：当前 cache_id=${state.editingSemanticCacheId}，点击“保存字段”更新。`
      : "新增模式：填写后点击“新增字段”。";
    semanticCacheFormHint.textContent =
      dedupedSemantic.duplicateCount > 0
        ? `${modeText} 检测到重复物理字段（table_name + field_name）${dedupedSemantic.duplicateCount} 条，列表仅展示最新一条。`
        : modeText;
  }
}

function requireCurrentRecommendation() {
  const recommendation = ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  if (!recommendation || typeof recommendation !== "object") {
    throw new Error("推荐结果为空：请先点击“推荐”生成候选列表");
  }
  return recommendation;
}

function removeSelectedDraftCandidate(kind, candidateId) {
  if (!candidateId) return;
  setLlmCandidateSelected(kind, candidateId, false);
}

function formatHistoryTime(value) {
  const ts = parseDateTime(value);
  if (!ts) return "-";
  const date = new Date(ts);
  const pad = (num) => String(num).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function getHistorySession(entry) {
  return entry?.session || entry || null;
}

function getHistoryEntryBySessionId(sessionId) {
  const id = String(sessionId || "").trim();
  if (!id) return null;
  return state.queryHistory.find((entry) => getHistorySession(entry)?.session_id === id) || null;
}

function clearQueryResultViews() {
  if (el("queryPlanView")) el("queryPlanView").textContent = "";
  if (el("queryRunView")) el("queryRunView").textContent = "";
  if (el("querySaveHint")) el("querySaveHint").textContent = "";
  renderQueryTable([]);
}

function renderSessions() {
  const list = el("sessionsList");
  if (!list) return;
  list.innerHTML = "";
  for (const session of state.sessions) {
    const row = document.createElement("div");
    row.className = `history-row ${state.currentSession?.session_id === session.session_id ? "active" : ""}`;
    const item = document.createElement("button");
    item.className = "list-item history-main-btn";
    item.innerHTML =
      `<strong>${escapeHtml(session.global_goal || "未命名问题")}</strong>` +
      `<span class="muted">${escapeHtml(formatHistoryTime(session.updated_at || session.created_at))} · ${escapeHtml(session.status || "-")}</span>`;
    item.onclick = () => run(() => selectQueryHistory(session.session_id));
    row.appendChild(item);

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "history-delete-btn";
    deleteBtn.textContent = "删除";
    deleteBtn.onclick = (event) => {
      event.stopPropagation();
      run(() => deleteQueryHistory(session.session_id));
    };
    row.appendChild(deleteBtn);
    list.appendChild(row);
  }
  if (!state.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "scene-empty muted";
    empty.textContent = "暂无提问历史。";
    list.appendChild(empty);
  }
}

function renderQueryHistory() {
  const list = el("queryHistoryList");
  if (list) list.innerHTML = "";
  const entries = state.queryHistory;
  for (const entry of entries) {
    const session = getHistorySession(entry);
    if (!session?.session_id) continue;
    const queryRun = entry.query_run || null;
    const row = document.createElement("div");
    row.className = `history-row query-history-row ${state.currentSession?.session_id === session.session_id ? "active" : ""}`;

    const main = document.createElement("button");
    main.className = "list-item history-main-btn";
    const statusText = queryRun
      ? `${queryRun.status || "-"} · ${Number(queryRun.rows_count || 0)} rows`
      : `${session.status || "-"} · 未执行`;
    main.innerHTML =
      `<strong>${escapeHtml(session.global_goal || "未命名问题")}</strong>` +
      `<span class="muted">${escapeHtml(formatHistoryTime(session.updated_at || session.created_at))} · ${escapeHtml(statusText)}</span>`;
    main.onclick = () => run(() => selectQueryHistory(session.session_id));
    row.appendChild(main);

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "history-delete-btn";
    deleteBtn.textContent = "删除";
    deleteBtn.onclick = (event) => {
      event.stopPropagation();
      run(() => deleteQueryHistory(session.session_id));
    };
    row.appendChild(deleteBtn);
    if (list) list.appendChild(row);
  }
  if (list && !entries.length) {
    const empty = document.createElement("div");
    empty.className = "scene-empty muted";
    empty.textContent = "当前场景暂无提问历史。点击“生成并执行”后会自动保存。";
    list.appendChild(empty);
  }
  renderDeliveryHistory();
}

function renderDeliveryHistory() {
  const list = el("deliveryHistoryList");
  const hint = el("deliverySessionHint");
  if (!list) return;
  list.innerHTML = "";
  const entries = state.queryHistory;
  for (const entry of entries) {
    const session = getHistorySession(entry);
    if (!session?.session_id) continue;
    const queryRun = entry.query_run || null;
    const row = document.createElement("div");
    row.className = `history-row query-history-row ${state.currentSession?.session_id === session.session_id ? "active" : ""}`;

    const main = document.createElement("button");
    main.className = "list-item history-main-btn";
    const statusText = queryRun
      ? `${queryRun.status || "-"} · ${Number(queryRun.rows_count || 0)} rows`
      : `${session.status || "-"} · 未执行`;
    main.innerHTML =
      `<strong>${escapeHtml(session.global_goal || "未命名问题")}</strong>` +
      `<span class="muted">${escapeHtml(formatHistoryTime(session.updated_at || session.created_at))} · ${escapeHtml(statusText)}</span>`;
    main.onclick = () => run(async () => {
      await selectQueryHistory(session.session_id);
      switchToTab("delivery");
    });
    row.appendChild(main);

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "history-delete-btn";
    deleteBtn.textContent = "删除";
    deleteBtn.onclick = (event) => {
      event.stopPropagation();
      run(() => deleteQueryHistory(session.session_id));
    };
    row.appendChild(deleteBtn);
    list.appendChild(row);
  }
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "scene-empty muted";
    empty.textContent = "暂无可生成汇报的提问历史。请先在“查询执行”里生成并执行。";
    list.appendChild(empty);
  }
  if (hint) {
    hint.textContent = state.currentSession
      ? `当前汇报目标：${state.currentSession.global_goal} · session=${state.currentSession.session_id}`
      : "请选择一条已执行的提问历史后生成汇报产物。";
  }
}

function renderSessionHeader() {
  const topbar = el("topbar");
  const topbarMeta = el("topbarMeta");
  if (!state.currentSession) {
    el("sessionTitle").textContent = "";
    el("sessionMeta").textContent = "";
    if (topbarMeta) topbarMeta.hidden = true;
    if (topbar) topbar.classList.add("meta-hidden");
    return;
  }
  if (topbarMeta) topbarMeta.hidden = false;
  if (topbar) topbar.classList.remove("meta-hidden");
  el("sessionTitle").textContent = state.currentSession.global_goal;
  el("sessionMeta").textContent =
    `session=${state.currentSession.session_id} · scene=${state.currentSession.scene_id} · thread=${state.currentSession.deerflow_thread_id || "-"}`;
  if (el("queryIntentInput") && !el("queryIntentInput").value.trim()) {
    el("queryIntentInput").value = state.currentSession.global_goal || "";
  }
}

function renderQueryTable(rows) {
  const wrap = el("queryTableWrap");
  if (!rows?.length) {
    wrap.innerHTML = "";
    return;
  }
  const columns = Object.keys(rows[0]);
  const formatCell = (value) => {
    if (value === null || value === undefined || value === "") return "（空）";
    return String(value);
  };
  wrap.innerHTML =
    `<table class="data-table"><thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows
      .map((row) => `<tr>${columns.map((column) => `<td>${escapeHtml(formatCell(row[column]))}</td>`).join("")}</tr>`)
      .join("")}</tbody></table>`;
}

function selectedValue(id) {
  const node = el(id);
  if (!node) return "";
  return (node.value || "").trim();
}

function readNumber(id) {
  const text = selectedValue(id);
  if (!text) return null;
  const num = Number(text);
  if (!Number.isFinite(num)) return null;
  return num;
}

function buildClothingQuery(overrides = {}) {
  const params = new URLSearchParams();
  const payload = {
    brand: selectedValue("clothingBrand"),
    category: selectedValue("clothingCategory"),
    sub_category: selectedValue("clothingSubCategory"),
    scene: selectedValue("clothingScene"),
    fiber: selectedValue("clothingFiber"),
    min_price: readNumber("clothingMinPrice"),
    max_price: readNumber("clothingMaxPrice"),
    ...overrides,
  };
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  return params;
}

function fillFacetSelect(id, rows, placeholder, currentValue) {
  const select = el(id);
  if (!select) return;
  select.innerHTML = "";
  const emptyOption = document.createElement("option");
  emptyOption.value = "";
  emptyOption.textContent = placeholder;
  select.appendChild(emptyOption);
  for (const row of rows || []) {
    const option = document.createElement("option");
    option.value = row.value;
    option.textContent = `${row.value} (${row.count})`;
    if (row.value === currentValue) option.selected = true;
    select.appendChild(option);
  }
}

function renderClothingItems() {
  const list = el("clothingList");
  const detail = el("clothingDetail");
  const meta = el("clothingMeta");
  if (!list || !detail || !meta) return;

  const start = state.clothing.total === 0 ? 0 : state.clothing.offset + 1;
  const end = Math.min(state.clothing.offset + state.clothing.limit, state.clothing.total);
  meta.textContent = `共 ${state.clothing.total} 条，当前 ${start}-${end}`;

  list.innerHTML = "";
  for (const item of state.clothing.items) {
    const button = document.createElement("button");
    button.className = `list-item ${state.clothing.selectedId === item.Id ? "active" : ""}`;
    button.innerHTML =
      `<strong>${escapeHtml(item.Name || "-")}</strong><br>` +
      `<span class="muted">${escapeHtml(item.BrandName || "-")} · ${escapeHtml(item.Category || "-")} · ${escapeHtml(item.Price ?? "-")}</span>`;
    button.onclick = () => run(async () => {
      state.clothing.selectedId = item.Id;
      await fetchClothingDetail(item.Id);
      renderClothingItems();
    });
    list.appendChild(button);
  }

  if (!state.clothing.items.length) {
    detail.textContent = pretty({ message: "无匹配结果" });
  } else if (state.clothing.detail) {
    detail.textContent = pretty(state.clothing.detail);
  } else {
    detail.textContent = pretty({ message: "请选择左侧商品查看详情" });
  }

  const prev = el("clothingPrevBtn");
  const next = el("clothingNextBtn");
  if (prev) prev.disabled = state.clothing.offset <= 0;
  if (next) next.disabled = state.clothing.offset + state.clothing.limit >= state.clothing.total;
}

async function refreshClothingFacets() {
  const params = buildClothingQuery();
  const data = await api(`/api/v1/clothing/facets?${params.toString()}`);
  state.clothing.facets = data;
  fillFacetSelect("clothingBrand", data.brand, "全部品牌", selectedValue("clothingBrand"));
  fillFacetSelect("clothingCategory", data.category, "全部一级类目", selectedValue("clothingCategory"));
  fillFacetSelect("clothingSubCategory", data.sub_category, "全部二级类目", selectedValue("clothingSubCategory"));
  fillFacetSelect("clothingScene", data.scene, "全部场景", selectedValue("clothingScene"));
  fillFacetSelect("clothingFiber", data.fiber, "全部材质", selectedValue("clothingFiber"));
}

async function fetchClothingDetail(id) {
  state.clothing.detail = await api(`/api/v1/clothing/items/${id}`);
}

async function refreshClothingItems({ keepPage = false } = {}) {
  if (!keepPage) state.clothing.offset = 0;
  const params = buildClothingQuery({
    limit: state.clothing.limit,
    offset: state.clothing.offset,
  });
  const data = await api(`/api/v1/clothing/items?${params.toString()}`);
  state.clothing.total = data.total || 0;
  state.clothing.items = data.items || [];
  state.clothing.selectedId = state.clothing.items[0]?.Id || null;
  state.clothing.detail = null;
  if (state.clothing.selectedId) {
    await fetchClothingDetail(state.clothing.selectedId);
  }
  renderClothingItems();
}

async function refreshClothingAll({ keepPage = false } = {}) {
  await refreshClothingFacets();
  await refreshClothingItems({ keepPage });
}

function resetClothingFilters() {
  for (const id of ["clothingBrand", "clothingCategory", "clothingSubCategory", "clothingScene", "clothingFiber"]) {
    if (el(id)) el(id).value = "";
  }
  if (el("clothingMinPrice")) el("clothingMinPrice").value = "";
  if (el("clothingMaxPrice")) el("clothingMaxPrice").value = "";
}

function syncArtifactDownload() {
  const link = el("downloadArtifactBtn");
  if (!link) return;
  if (!state.currentArtifact?.download_url) {
    link.classList.add("disabled");
    link.href = "#";
    link.removeAttribute("download");
    return;
  }
  link.classList.remove("disabled");
  link.href = `${state.backendBase}${state.currentArtifact.download_url}`;
  link.setAttribute("download", state.currentArtifact.file_name || "deck.pptx");
}

function renderReportState() {
  const slideView = el("slideView");
  const deckView = el("deckView");
  if (slideView) slideView.textContent = state.currentSlide ? pretty(state.currentSlide) : "";
  renderSlidePreview(state.currentSlide || null);
  if (deckView) {
    if (state.currentDeck || state.currentArtifact) {
      deckView.textContent = pretty({
        deck: state.currentDeck || null,
        artifact: state.currentArtifact || null,
      });
    } else {
      deckView.textContent = "";
    }
  }
  syncArtifactDownload();
  renderDeliveryHistory();
}

async function loadReportStateForCurrentSession({ silent = false } = {}) {
  if (!state.currentSession?.session_id) {
    clearReportStateViews();
    return null;
  }
  const payload = await api(`/api/v1/analysis/sessions/${state.currentSession.session_id}/report-state`);
  state.currentReportState = payload || null;
  if (payload?.session) {
    state.currentSession = payload.session;
    if (payload.session.scene_id) state.currentSceneId = payload.session.scene_id;
    state.restoreSessionId = payload.session.session_id || state.restoreSessionId;
  }
  state.currentSlide = payload?.slide || null;
  state.currentDeck = payload?.deck || null;
  state.currentArtifact = payload?.artifact || null;
  renderReportState();
  renderSessions();
  renderScenes();
  renderSessionHeader();
  persistUiState();
  if (!silent) {
    if (state.currentArtifact?.file_name) {
      setDeliveryActionHint(`已恢复已保存 PPT：${state.currentArtifact.file_name}`);
    } else if (state.currentDeck?.deck_id) {
      setDeliveryActionHint(`已恢复 Deck：${state.currentDeck.deck_id}`);
    } else if (state.currentSlide?.slide_id) {
      setDeliveryActionHint(`已恢复 Slide：${state.currentSlide.slide_id}`);
    } else {
      setDeliveryActionHint("当前会话还没有保存的汇报产物。");
    }
  }
  return payload;
}

function renderTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  const panels = document.querySelectorAll(".tab-panel");
  for (const button of buttons) {
    const isActive = button.dataset.tabTarget === state.activeTab;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
  }
  for (const panel of panels) {
    const isActive = panel.dataset.tabPanel === state.activeTab;
    panel.classList.toggle("active", isActive);
    panel.hidden = !isActive;
  }
}

function bindTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  for (const button of buttons) {
    button.onclick = () => {
      switchToTab(button.dataset.tabTarget || "overview");
    };
  }
}

function switchToTab(tabKey) {
  const nextTab = VALID_TABS.has(tabKey) ? tabKey : "overview";
  state.activeTab = nextTab;
  renderTabs();
  persistUiState();
  if (state.activeTab === "delivery" && state.currentSession?.session_id) {
    loadReportStateForCurrentSession({ silent: true }).catch((error) => {
      console.warn("load report state failed", error);
    });
  }
  maybeAutoRecommendOnce();
}

function maybeAutoRecommendOnce() {
  if (state.activeTab !== "config") return;
  const sceneId = String(state.currentSceneId || "").trim();
  if (!sceneId) return;
  if (getSceneDraft(sceneId)) return;
  if (state.autoRecommendedSceneIds[sceneId]) return;
  state.autoRecommendedSceneIds[sceneId] = true;
  withAgentWait("recommend", "推荐 Agent", recommendSceneByLlm).catch((error) => {
    delete state.autoRecommendedSceneIds[sceneId];
    console.error(error);
  });
}

async function refreshScenes() {
  try {
    const payload = await api("/api/v1/scenes");
    state.scenes = normalizeSceneList(payload);
    state.sceneLoadError = "";
  } catch (error) {
    state.sceneLoadError = error?.message || String(error);
    console.error(error);
  }
  if (!state.currentSceneId && state.scenes.length > 0) {
    state.currentSceneId = state.scenes[0].scene_id;
  }
  if (state.currentSceneId && !state.scenes.find((scene) => scene.scene_id === state.currentSceneId)) {
    state.currentSceneId = "";
  }
  renderScenes();
  await refreshSceneDetail();
  renderIntentTemplates();
  await refreshQueryHistory().catch((error) => console.warn("refresh query history failed", error));
  persistUiState();
  maybeAutoRecommendOnce();
}

async function refreshSessions() {
  state.sessions = await api("/api/v1/analysis/sessions");
  const desiredSessionId = String(state.restoreSessionId || state.currentSession?.session_id || "").trim();
  let nextSession = null;
  if (desiredSessionId) {
    nextSession = state.sessions.find((item) => item.session_id === desiredSessionId) || null;
  }
  if (!nextSession && state.currentSceneId) {
    nextSession = pickBestSessionForScene(state.currentSceneId);
  }
  state.currentSession = nextSession;
  state.restoreSessionId = nextSession?.session_id || desiredSessionId || "";
  if (nextSession?.scene_id) {
    state.currentSceneId = nextSession.scene_id;
  } else {
    clearReportStateViews();
  }
  renderSessions();
  renderSessionHeader();
  renderQueryHistory();
  if (state.currentSession?.session_id) {
    await loadLatestSqlResultForCurrentSession({ force: true });
    await loadReportStateForCurrentSession({ silent: true }).catch((error) => {
      console.warn("load report state failed", error);
    });
  }
  persistUiState();
}

async function refreshQueryHistory() {
  const query = state.currentSceneId ? `?scene_id=${encodeURIComponent(state.currentSceneId)}` : "";
  const payload = await api(`/api/v1/sql-result-agent/history${query}`);
  state.queryHistory = Array.isArray(payload?.items) ? payload.items : [];
  const historySessions = state.queryHistory.map((entry) => getHistorySession(entry)).filter((session) => session?.session_id);
  if (historySessions.length) {
    const seen = new Set(state.sessions.map((session) => session.session_id));
    for (const session of historySessions) {
      if (seen.has(session.session_id)) continue;
      seen.add(session.session_id);
      state.sessions.push(session);
    }
  }
  renderQueryHistory();
  if (!state.currentSession && historySessions.length) {
    const desiredSessionId = String(state.restoreSessionId || "").trim();
    const restoredSession = desiredSessionId
      ? historySessions.find((session) => session.session_id === desiredSessionId)
      : null;
    await selectQueryHistory((restoredSession || historySessions[0]).session_id);
  }
}

async function selectQueryHistory(sessionId) {
  const id = String(sessionId || "").trim();
  if (!id) return;
  let entry = getHistoryEntryBySessionId(id);
  let session = getHistorySession(entry);
  if (!session) {
    session = await api(`/api/v1/analysis/sessions/${id}`);
  }

  await setCurrentSession(session, { loadThread: false });
  fillIntentInputs(session.global_goal || "");

  if (!entry?.query_run && !entry?.query_plan) {
    try {
      const latest = await api(`/api/v1/sql-result-agent/sessions/${id}/latest`);
      entry = {
        session,
        query_plan: latest?.query_plan || null,
        query_run: latest?.query_run || null,
      };
    } catch (error) {
      if (!isSessionNotFoundError(error)) throw error;
    }
  }

  if (entry?.query_plan) {
    el("queryPlanView").textContent = formatQueryPlanView(entry.query_plan);
  } else if (el("queryPlanView")) {
    el("queryPlanView").textContent = "";
  }
  if (entry?.query_run) {
    el("queryRunView").textContent = formatQueryRunView(entry.query_run);
    renderQueryTable(entry.query_run.result_preview || []);
    if (el("querySaveHint")) {
      el("querySaveHint").textContent = `已加载历史：${id}`;
    }
  } else {
    if (el("queryRunView")) el("queryRunView").textContent = "";
    renderQueryTable([]);
    if (el("querySaveHint")) {
      el("querySaveHint").textContent = "该历史尚无 SQL 执行结果。";
    }
  }
  await loadReportStateForCurrentSession({ silent: true });
  renderSessions();
  renderQueryHistory();
  persistUiState();
}

async function deleteQueryHistory(sessionId) {
  const id = String(sessionId || "").trim();
  if (!id) return;
  const entry = getHistoryEntryBySessionId(id);
  const session = getHistorySession(entry) || state.sessions.find((item) => item.session_id === id);
  const title = session?.global_goal ? `“${session.global_goal}”` : id;
  const ok = window.confirm(`确认删除提问历史 ${title}？相关 SQL、Slide、Deck 记录会一起删除。`);
  if (!ok) return;
  await api(`/api/v1/analysis/sessions/${id}`, { method: "DELETE" });
  if (state.currentSession?.session_id === id) {
    clearCurrentSessionState();
    clearQueryResultViews();
  }
  state.sessions = state.sessions.filter((item) => item.session_id !== id);
  state.queryHistory = state.queryHistory.filter((item) => getHistorySession(item)?.session_id !== id);
  renderSessions();
  renderQueryHistory();
  await refreshSessions();
  await refreshQueryHistory();
}

async function loadDeckForCurrentSession() {
  if (!state.currentSession?.session_id) return null;
  await loadReportStateForCurrentSession({ silent: true });
  return state.currentDeck;
}

async function loadLatestSqlResultForCurrentSession({ force = false } = {}) {
  if (!state.currentSession?.session_id) return;
  const queryPlanView = el("queryPlanView");
  const queryRunView = el("queryRunView");
  const hasVisibleSqlResult = Boolean(
    (queryPlanView?.textContent || "").trim() ||
      (queryRunView?.textContent || "").trim() ||
      (el("queryTableWrap")?.innerHTML || "").trim(),
  );
  if (hasVisibleSqlResult && !force) return;
  let latest;
  try {
    latest = await api(`/api/v1/sql-result-agent/sessions/${state.currentSession.session_id}/latest`);
  } catch (error) {
    console.warn("load latest sql result failed", error);
    return;
  }
  if (latest?.query_plan && el("queryPlanView")) {
    el("queryPlanView").textContent = formatQueryPlanView(latest.query_plan);
  }
  if (latest?.query_run) {
    if (el("queryRunView")) el("queryRunView").textContent = formatQueryRunView(latest.query_run);
    renderQueryTable(latest.query_run.result_preview || []);
  }
}

async function createScene() {
  const name = el("sceneName").value.trim();
  if (!name) throw new Error("创建场景失败：请填写场景名称");
  const scene = await api("/api/v1/scenes", {
    method: "POST",
    body: JSON.stringify({
      name,
      description: el("sceneDesc").value.trim(),
    }),
  });
  await refreshScenes();
  state.currentSceneId = scene.scene_id;
  renderScenes();
  await refreshSceneDetail();
}

async function refreshSceneDetail() {
  if (!state.currentSceneId) {
    state.currentSceneDetail = null;
    state.currentScenePlaybook = null;
    state.selectedPresetKey = "";
    state.selectedPresetQuestion = "";
    state.semanticCacheFields = [];
    state.editingSemanticCacheId = "";
    state.currentLlmAgentDraft = null;
    renderSceneConfig();
    return;
  }
  state.currentSceneDetail = await api(`/api/v1/scenes/${state.currentSceneId}`);
  try {
    state.currentScenePlaybook = await api(`/api/v1/scenes/${state.currentSceneId}/playbook`);
  } catch (error) {
    console.warn("load scene playbook failed", error);
    state.currentScenePlaybook = null;
  }
  const currentPresetStillExists = Array.isArray(state.currentScenePlaybook?.question_matrix)
    ? state.currentScenePlaybook.question_matrix.some((item) => item?.preset_key === state.selectedPresetKey)
    : false;
  if (!currentPresetStillExists) {
    state.selectedPresetKey = "";
    state.selectedPresetQuestion = "";
  }
  const semanticCache = await api(`/api/v1/semantic-cache/scenes/${state.currentSceneId}/fields?include_disabled=true`);
  state.semanticCacheFields = Array.isArray(semanticCache?.fields) ? semanticCache.fields : [];
  if (
    state.editingSemanticCacheId &&
    !state.semanticCacheFields.find((item) => item.cache_id === state.editingSemanticCacheId)
  ) {
    state.editingSemanticCacheId = "";
  }
  let draft = getSceneDraft(state.currentSceneId);
  if (!draft) {
    try {
      const draftPayload = await api(`/api/v1/scene-builder/scenes/${state.currentSceneId}/draft`);
      draft = draftPayload?.draft || null;
      if (draft) {
        ensureDraftCandidateMeta(draft);
        setSceneDraft(state.currentSceneId, draft, { persist: false });
      }
    } catch (error) {
      console.warn("load scene draft failed", error);
    }
  }
  state.currentLlmAgentDraft = draft;
  renderSceneConfig();
}

async function recommendSceneByLlm() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const goal = (el("llmGoal")?.value || "").trim();
  const result = await api(`/api/v1/scene-builder/scenes/${state.currentSceneId}/candidates`, {
    method: "POST",
    body: JSON.stringify({
      goal,
      max_tables: 4,
      max_fields_per_table: 12,
    }),
  });
  state.currentLlmAgentDraft = {
    recommendation_id: result.recommendation_id,
    scene_id: result.scene_id,
    scene_version: result.scene_version,
    provider: result?.meta?.provider || "heuristic",
    mode: result?.meta?.mode || "local",
    goal: result.goal || "",
    notes: Array.isArray(result.notes) ? result.notes : [],
    field_type_list: Array.isArray(result?.meta?.field_type_list) ? result.meta.field_type_list : [],
    candidates: {
      tables: Array.isArray(result?.meta?.table_candidates) ? result.meta.table_candidates : [],
      fields: Array.isArray(result.field_candidates) ? result.field_candidates : [],
      relations: Array.isArray(result.relation_candidates) ? result.relation_candidates : [],
      metric_templates: [],
      regression_questions: [],
    },
  };
  ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  setSceneDraft(state.currentSceneId, state.currentLlmAgentDraft);
  renderSceneConfig();
}

async function validateSceneDraftFromLlm() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const recommendation = requireCurrentRecommendation();
  const result = await api(`/api/v1/llm-agent/scenes/${state.currentSceneId}/validate`, {
    method: "POST",
    body: JSON.stringify({ recommendation }),
  });
  state.currentLlmAgentDraft = result.draft || {
    ...(recommendation || {}),
    last_validate_result: {
      ok: result.ok,
      error_count: result.error_count,
      warning_count: result.warning_count,
      issues: result.issues || [],
    },
  };
  ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  setSceneDraft(state.currentSceneId, state.currentLlmAgentDraft);
  renderSceneConfig();
}

async function applySceneDraftFromLlm() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const mergeMode = "append";
  const recommendation = requireCurrentRecommendation();
  const fields = Array.isArray(recommendation?.candidates?.fields)
    ? recommendation.candidates.fields.filter((item) => item?.selected !== false)
    : [];
  const relations = Array.isArray(recommendation?.candidates?.relations)
    ? recommendation.candidates.relations.filter((item) => item?.selected !== false)
    : [];
  const selectedFields = fields.map((item) => ({
    table_name: item.table_name || "",
    field_name: item.field_name || "",
    semantic_name: item.semantic_name || "",
    role: item.role || "dimension",
    description: item.description || "",
    required: Boolean(item.required),
    confidence: Number.isFinite(Number(item.confidence)) ? Number(item.confidence) : 0.5,
    field_type: item.field_type || "",
    enabled: item.enabled !== false,
  }));
  const selectedRelations = relations.map((item) => ({
    left_table: item.left_table || "",
    left_field: item.left_field || "",
    right_table: item.right_table || "",
    right_field: item.right_field || "",
    join_type: item.join_type || "LEFT",
    cardinality: item.cardinality || "1:N",
    required: Boolean(item.required),
    confidence: Number.isFinite(Number(item.confidence)) ? Number(item.confidence) : 0.5,
    note: item.note || item.reason || "",
  }));
  const result = await api(`/api/v1/scene-builder/scenes/${state.currentSceneId}/imports`, {
    method: "POST",
    body: JSON.stringify({
      recommendation_id: recommendation.recommendation_id,
      merge_mode: mergeMode,
      selected_fields: selectedFields,
      selected_relations: selectedRelations,
    }),
  });
  state.currentLlmAgentDraft = {
    ...(recommendation || {}),
    validate_result: result?.validate_result || null,
    last_apply_result: result,
  };
  ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  setSceneDraft(state.currentSceneId, state.currentLlmAgentDraft);
  await refreshScenes();
}

async function publishSceneFromLlm() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const recommendation = requireCurrentRecommendation();
  const result = await api(`/api/v1/llm-agent/scenes/${state.currentSceneId}/publish`, {
    method: "POST",
    body: JSON.stringify({ recommendation }),
  });
  state.currentLlmAgentDraft = {
    ...(recommendation || {}),
    last_publish_result: result,
  };
  ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  setSceneDraft(state.currentSceneId, state.currentLlmAgentDraft);
  await refreshScenes();
}

async function addSceneField() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const payload = {
    semantic_name: el("fieldSemanticName").value.trim(),
    semantic_definition: el("fieldSemanticDefinition").value.trim(),
    unit: el("fieldUnit").value.trim(),
    aggregation: el("fieldAggregation").value.trim(),
    table_name: el("fieldTableName").value.trim(),
    field_name: el("fieldName").value.trim(),
    aliases: splitAliases(el("fieldAliases").value),
    er_path: el("fieldErPath").value.trim(),
    role: el("fieldRole").value,
    enabled: el("fieldEnabled").checked,
  };
  if (!payload.table_name || !payload.field_name || !payload.semantic_name) {
    throw new Error("新增字段失败：请至少填写 table_name / field_name / semantic_name");
  }
  if (state.editingSemanticCacheId) {
    await api(`/api/v1/semantic-cache/scenes/${state.currentSceneId}/fields/${state.editingSemanticCacheId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  } else {
    await api(`/api/v1/semantic-cache/scenes/${state.currentSceneId}/fields`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }
  clearSemanticFieldForm();
  await refreshSceneDetail();
}

function fillSemanticFieldForm(row) {
  if (!row || typeof row !== "object") return;
  el("fieldSemanticName").value = row.semantic_name || "";
  el("fieldSemanticDefinition").value = row.semantic_definition || "";
  el("fieldUnit").value = row.unit || "";
  el("fieldAggregation").value = row.aggregation || "";
  el("fieldTableName").value = row.table_name || "";
  el("fieldName").value = row.field_name || "";
  el("fieldAliases").value = Array.isArray(row.aliases) ? row.aliases.join(", ") : "";
  el("fieldErPath").value = row.er_path || "";
  el("fieldRole").value = row.role || "dimension";
  el("fieldEnabled").checked = row.enabled !== false;
}

function clearSemanticFieldForm() {
  state.editingSemanticCacheId = "";
  el("fieldSemanticName").value = "";
  el("fieldSemanticDefinition").value = "";
  el("fieldUnit").value = "";
  el("fieldAggregation").value = "";
  el("fieldTableName").value = "";
  el("fieldName").value = "";
  el("fieldAliases").value = "";
  el("fieldErPath").value = "";
  el("fieldRole").value = "metric";
  el("fieldEnabled").checked = true;
  renderSceneConfig();
}

async function editSemanticCacheField(cacheId) {
  const target = (state.semanticCacheFields || []).find((item) => item.cache_id === cacheId);
  if (!target) throw new Error("未找到待编辑字段");
  state.editingSemanticCacheId = cacheId;
  fillSemanticFieldForm(target);
  renderSceneConfig();
}

async function deleteSemanticCacheField(cacheId) {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const ok = window.confirm("确认删除该缓存字段？删除后将不可用于查询。");
  if (!ok) return;
  await api(`/api/v1/semantic-cache/scenes/${state.currentSceneId}/fields/${cacheId}`, {
    method: "DELETE",
  });
  if (state.editingSemanticCacheId === cacheId) clearSemanticFieldForm();
  await refreshSceneDetail();
}

async function toggleSemanticCacheField(cacheId, currentEnabled) {
  if (!state.currentSceneId) throw new Error("未选择场景");
  await api(`/api/v1/semantic-cache/scenes/${state.currentSceneId}/fields/${cacheId}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled: !currentEnabled }),
  });
  await refreshSceneDetail();
}

async function addSceneRelation() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const payload = {
    left_table: el("relationLeftTable").value.trim(),
    left_field: el("relationLeftField").value.trim(),
    right_table: el("relationRightTable").value.trim(),
    right_field: el("relationRightField").value.trim(),
    join_type: el("relationJoinType").value,
    note: el("relationNote").value.trim(),
  };
  if (!payload.left_table || !payload.left_field || !payload.right_table || !payload.right_field) {
    throw new Error("新增关系失败：请至少填写四个连接字段");
  }
  await api(`/api/v1/scenes/${state.currentSceneId}/relations`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  await refreshScenes();
}

async function deleteSceneRelation(relationId) {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const ok = window.confirm("确认删除该 ER 关系？删除后跨表查询可能受影响。");
  if (!ok) return;
  await api(`/api/v1/scenes/${state.currentSceneId}/relations/${relationId}`, {
    method: "DELETE",
  });
  await refreshScenes();
}

async function publishCurrentScene() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  await api(`/api/v1/scenes/${state.currentSceneId}/publish`, { method: "POST" });
  await refreshScenes();
}

async function createSession() {
  const goal = el("goalInput").value.trim();
  const session = await createSessionForCurrentScene({ intent: goal });
  if (!session) throw new Error("请先选择场景");
  await refreshSessions();
  await refreshQueryHistory();
  fillIntentInputs(goal || state.currentSession?.global_goal || "");
  syncArtifactDownload();
}

async function generateSqlFromIntent() {
  const intent = (el("queryIntentInput")?.value || "").trim();
  if (!intent) throw new Error("请先输入业务问题");
  fillIntentInputs(intent);
  const session = await createSessionForCurrentScene({ intent });
  if (!session) throw new Error("请先选择场景");
  await refreshSessions();
  await refreshQueryHistory();
  await loadPlan();
}

async function loadPlan() {
  ensureSession();
  await api(`/api/v1/analysis/sessions/${state.currentSession.session_id}/plan`, { method: "POST" });
  const queryPlan = await api(`/api/v1/analysis/sessions/${state.currentSession.session_id}/current-query-plan`);
  el("queryPlanView").textContent = formatQueryPlanView(queryPlan);
}

function normalizeIntent(text) {
  return String(text || "").trim().replace(/\s+/g, " ");
}

async function syncIntentPlanIfNeeded() {
  ensureSession();
  const intent = normalizeIntent(el("queryIntentInput")?.value || "");
  if (!intent) return;
  const currentGoal = normalizeIntent(state.currentSession?.global_goal || "");
  if (intent === currentGoal) return;
  fillIntentInputs(intent);
  state.currentSession = await api(`/api/v1/analysis/sessions/${state.currentSession.session_id}/goal`, {
    method: "POST",
    body: JSON.stringify({ global_goal: intent }),
  });
  await refreshSessions();
  await loadPlan();
}

async function executeQuery() {
  ensureSession();
  await syncIntentPlanIfNeeded();
  const queryRun = await api(`/api/v1/analysis/sessions/${state.currentSession.session_id}/current-query/execute`, { method: "POST" });
  el("queryRunView").textContent = formatQueryRunView(queryRun);
  renderQueryTable(queryRun.result_preview || []);
  if (el("querySaveHint")) el("querySaveHint").textContent = `已保存到提问历史：${state.currentSession.session_id}`;
  await refreshSessions();
  await refreshQueryHistory();
}

async function runSqlResultAgentFromConfig() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const intent =
    (el("queryIntentInput")?.value || "").trim() ||
    (el("goalInput")?.value || "").trim() ||
    (el("llmGoal")?.value || "").trim();
  if (!intent) throw new Error("请先输入业务问题或分析目标");
  fillIntentInputs(intent);
  const session = await createSessionForCurrentScene({ intent });
  if (!session) throw new Error("请先选择场景");
  const recommendation = state.currentLlmAgentDraft?.candidates || {};
  const selectedFieldCount = Array.isArray(recommendation.fields)
    ? recommendation.fields.filter((item) => item?.selected !== false).length
    : 0;
  const selectedRelationCount = Array.isArray(recommendation.relations)
    ? recommendation.relations.filter((item) => item?.selected !== false).length
    : 0;
  switchToTab("query");
  clearQueryResultViews();
  if (el("querySaveHint")) el("querySaveHint").textContent = "执行中，完成后会自动保存到提问历史。";
  const result = await api(`/api/v1/sql-result-agent/sessions/${session.session_id}/generate-and-run`, {
    method: "POST",
    body: JSON.stringify({
      intent,
      agent_prompt: (el("llmGoal")?.value || "").trim(),
      execute: true,
      context: {
        source: "query_tab",
        scene_id: state.currentSceneId,
        selected_preset_key: state.selectedPresetKey || undefined,
        selected_preset_question: state.selectedPresetQuestion || undefined,
        intent_edited_from_preset: Boolean(
          state.selectedPresetKey && normalizeIntent(intent) !== normalizeIntent(state.selectedPresetQuestion),
        ),
        selected_field_count: selectedFieldCount,
        selected_relation_count: selectedRelationCount,
      },
    }),
  });
  if (result?.query_plan) {
    el("queryPlanView").textContent = formatQueryPlanView(result.query_plan);
  }
  if (result?.query_run) {
    el("queryRunView").textContent = formatQueryRunView(result.query_run);
    renderQueryTable(result.query_run.result_preview || []);
    if (el("querySaveHint")) {
      el("querySaveHint").textContent = result.saved
        ? `已保存到提问历史：${session.session_id}`
        : "执行完成，但未返回可保存的 SQL 结果。";
    }
  }
  await refreshSessions();
  await refreshQueryHistory();
}

async function withButtonBusy(buttonId, busyText, fn) {
  const button = el(buttonId);
  const originalText = button?.textContent || "";
  if (button) {
    button.disabled = true;
    button.textContent = busyText;
  }
  try {
    return await fn();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function loadSlide() {
  return withButtonBusy("loadSlideBtn", "生成中...", async () => {
    ensureReportSession();
    const scheme = getSelectedPptScheme();
    setDeliveryActionHint("正在生成 Slide 预览...");
    const slide = await withSessionRecovery(
      (session) => api(`/api/v1/analysis/sessions/${session.session_id}/current-slide?scheme=${encodeURIComponent(scheme)}`),
      { createIfMissing: false, intent: getCurrentIntentText() },
    );
    state.currentSlide = slide;
    renderReportState();
    setDeliveryActionHint(`已生成 Slide 预览：${slide.slide_id}`);
    persistUiState();
    renderDeliveryHistory();
  });
}

async function regenerateSlide() {
  return withButtonBusy("regenerateSlideBtn", "重生成中...", async () => {
    ensureReportSession();
    const scheme = getSelectedPptScheme();
    setDeliveryActionHint("正在重生成 Slide...");
    const slide = await withSessionRecovery(
      (session) => api(`/api/v1/analysis/sessions/${session.session_id}/current-slide/regenerate`, {
        method: "POST",
        body: JSON.stringify({ scheme }),
      }),
      { createIfMissing: false, intent: getCurrentIntentText() },
    );
    state.currentSlide = slide;
    renderReportState();
    setDeliveryActionHint(`已重生成 Slide：${slide.slide_id}`);
    renderDeliveryHistory();
  });
}

async function approveSlide() {
  return withButtonBusy("approveSlideBtn", "入Deck中...", async () => {
    ensureReportSession();
    setDeliveryActionHint("正在批准当前 Slide 入 Deck...");
    state.currentDeck = await withSessionRecovery(
      (session) => api(`/api/v1/analysis/sessions/${session.session_id}/current-slide/approve`, { method: "POST" }),
      { createIfMissing: false, intent: getCurrentIntentText() },
    );
    await refreshSessions();
    await refreshQueryHistory();
    await loadReportStateForCurrentSession({ silent: true });
    setDeliveryActionHint(`已批准入 Deck：${state.currentDeck?.deck_id || "-"}`);
  });
}

async function exportDeck() {
  return withButtonBusy("exportDeckBtn", "导出中...", async () => {
    ensureReportSession();
    setDeliveryActionHint("正在导出 PPT...");
    if (!state.currentDeck) {
      await loadDeckForCurrentSession();
    }
    ensureDeck();
    state.currentArtifact = await api(`/api/v1/decks/${state.currentDeck.deck_id}/export`, { method: "POST" });
    await refreshSessions();
    await refreshQueryHistory();
    await loadReportStateForCurrentSession({ silent: true });
    const fileName = state.currentArtifact?.file_name || "PPT";
    setDeliveryActionHint(`已导出 PPT：${fileName}。可点击“下载PPT”。`);
  });
}

function splitSlideLines(value) {
  return String(value || "")
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function fillSlideEditor(slide) {
  const titleInput = el("slideTitleInput");
  const subtitleInput = el("slideSubtitleInput");
  const chartTypeSelect = el("slideChartTypeSelect");
  const findingsInput = el("slideFindingsInput");
  const narrativeInput = el("slideNarrativeInput");
  const recommendationsInput = el("slideRecommendationsInput");
  const hint = el("slideEditHint");
  if (!titleInput || !subtitleInput || !chartTypeSelect || !findingsInput || !narrativeInput || !recommendationsInput) return;

  const hasSlide = Boolean(slide);
  titleInput.disabled = !hasSlide;
  subtitleInput.disabled = !hasSlide;
  chartTypeSelect.disabled = !hasSlide;
  findingsInput.disabled = !hasSlide;
  narrativeInput.disabled = !hasSlide;
  recommendationsInput.disabled = !hasSlide;

  titleInput.value = slide?.title || "";
  subtitleInput.value = slide?.subtitle || "";
  chartTypeSelect.value = slide?.chart_spec?.chart_type || "table";
  findingsInput.value = Array.isArray(slide?.findings) ? slide.findings.join("\n") : "";
  narrativeInput.value = slide?.narrative || "";
  recommendationsInput.value = Array.isArray(slide?.recommendations) ? slide.recommendations.join("\n") : "";
  if (hint) {
    hint.textContent = hasSlide
      ? `当前稿：${slide.slide_id} · version=${slide.version || 1}`
      : "生成预览后可编辑标题、结论、说明、建议和图表类型。";
  }
}

function readSlideEditorPayload() {
  if (!state.currentSlide) throw new Error("请先生成 Slide 预览");
  const chartSpec = {
    ...(state.currentSlide.chart_spec || {}),
    chart_type: String(el("slideChartTypeSelect")?.value || state.currentSlide.chart_spec?.chart_type || "table"),
  };
  return {
    title: String(el("slideTitleInput")?.value || "").trim() || state.currentSlide.title,
    subtitle: String(el("slideSubtitleInput")?.value || "").trim(),
    findings: splitSlideLines(el("slideFindingsInput")?.value || ""),
    narrative: String(el("slideNarrativeInput")?.value || "").trim(),
    recommendations: splitSlideLines(el("slideRecommendationsInput")?.value || ""),
    chart_spec: chartSpec,
  };
}

async function saveSlideEdits() {
  return withButtonBusy("saveSlideEditBtn", "保存中...", async () => {
    ensureReportSession();
    const payload = readSlideEditorPayload();
    setDeliveryActionHint("正在保存 Slide 编辑...");
    const slide = await api(`/api/v1/analysis/sessions/${state.currentSession.session_id}/current-slide`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    state.currentSlide = slide;
    renderReportState();
    setDeliveryActionHint(`已保存 Slide 编辑：${slide.slide_id} · version=${slide.version || 1}`);
  });
}

function resetSlideEditor() {
  fillSlideEditor(state.currentSlide);
}

function ensureSession() {
  if (!state.currentSession) throw new Error("请先创建会话");
}

function ensureReportSession() {
  ensureSession();
  const entry = getHistoryEntryBySessionId(state.currentSession.session_id);
  if (entry && !entry.query_run) {
    throw new Error("当前历史尚无 SQL 执行结果，请先在查询执行区生成并执行");
  }
  return state.currentSession;
}

function ensureDeck() {
  if (!state.currentDeck) throw new Error("请先批准 Slide 入 Deck");
}

function getSelectedPptScheme() {
  const selected = String(el("pptSchemeSelect")?.value || state.pptScheme || "presenton_ai").trim();
  state.pptScheme = selected || "presenton_ai";
  persistUiState();
  return state.pptScheme;
}

function renderPptSchemeOptions() {
  const select = el("pptSchemeSelect");
  if (!select) return;
  const grouped = new Map();
  for (const item of state.pptSchemes) {
    const category = item.category || "其他方案";
    if (!grouped.has(category)) grouped.set(category, []);
    grouped.get(category).push(item);
  }
  select.innerHTML = Array.from(grouped.entries())
    .map(([category, items]) => {
      const options = items
        .map((item) => `<option value="${escapeHtml(item.scheme)}">${escapeHtml(item.name || item.scheme)}</option>`)
        .join("");
      return `<optgroup label="${escapeHtml(category)}">${options}</optgroup>`;
    })
    .join("");
  if (!state.pptSchemes.find((item) => item.scheme === state.pptScheme)) {
    state.pptScheme = state.pptSchemes[0]?.scheme || "presenton_ai";
  }
  select.value = state.pptScheme;
  renderPptSchemeHint();
}

function renderPptSchemeHint() {
  const hint = el("pptSchemeHint");
  if (!hint) return;
  const scheme = state.pptSchemes.find((item) => item.scheme === state.pptScheme);
  hint.textContent = scheme
    ? `${scheme.category ? `${scheme.category} / ` : ""}${scheme.name}：${scheme.description}${scheme.reference ? `（参考：${scheme.reference}）` : ""}`
    : "选择不同方案后生成预览，可对比标题、结论、关键数据点和建议。";
}

async function loadPptSchemes() {
  try {
    const schemes = await api("/api/v1/analysis/sessions/-/ppt-schemes");
    if (Array.isArray(schemes) && schemes.length) {
      state.pptSchemes = schemes.map((item) => ({
        scheme: String(item.scheme || "").trim(),
        name: String(item.name || item.scheme || "").trim(),
        description: String(item.description || "").trim(),
        category: String(item.category || "").trim(),
        reference: String(item.reference || "").trim(),
      })).filter((item) => item.scheme);
    }
  } catch (error) {
    console.warn("load ppt schemes failed", error);
  }
  renderPptSchemeOptions();
}

function renderSlidePreview(slide) {
  const wrap = el("slidePreview");
  if (!wrap) return;
  if (!slide) {
    wrap.innerHTML = `<div class="slide-preview-empty">暂无 Slide。请选择方案后点击生成预览。</div>`;
    fillSlideEditor(null);
    return;
  }
  fillSlideEditor(slide);
  const schemeName = slide.lineage_summary?.ppt_scheme_name || slide.chart_spec?.ppt_scheme_name || "PPT方案";
  const keyMetrics = Array.isArray(slide.lineage_summary?.key_metrics) ? slide.lineage_summary.key_metrics : [];
  const findings = Array.isArray(slide.findings) ? slide.findings : [];
  const recommendations = Array.isArray(slide.recommendations) ? slide.recommendations : [];
  const rows = Array.isArray(slide.chart_spec?.rows) ? slide.chart_spec.rows.slice(0, 5) : [];
  const columns = rows.length ? Object.keys(rows[0]).slice(0, 5) : [];
  const metricsHtml = keyMetrics.length
    ? keyMetrics
        .map(
          (item) =>
            `<div class="slide-preview-metric"><strong>${escapeHtml(item.value ?? "-")}</strong><span>${escapeHtml(item.label ?? "-")}</span></div>`,
        )
        .join("")
    : `<div class="slide-preview-metric"><strong>-</strong><span>暂无关键数据点</span></div>`;
  const chartHtml = rows.length
    ? `<table><thead><tr>${columns.map((col) => `<th>${escapeHtml(col)}</th>`).join("")}</tr></thead><tbody>${rows
        .map((row) => `<tr>${columns.map((col) => `<td>${escapeHtml(row[col] ?? "")}</td>`).join("")}</tr>`)
        .join("")}</tbody></table>`
    : `<p class="muted">暂无可预览数据，导出时会保留文字结论。</p>`;
  wrap.innerHTML = `
    <div class="slide-preview-head">
      <div class="slide-preview-kicker">${escapeHtml(schemeName)} · ${escapeHtml(slide.page_type || "-")}</div>
      <h3 class="slide-preview-title">${escapeHtml(slide.title || "未命名页面")}</h3>
      <div class="slide-preview-subtitle">${escapeHtml(slide.subtitle || "")}</div>
    </div>
    <div class="slide-preview-metrics">${metricsHtml}</div>
    <div class="slide-preview-body">
      <section class="slide-preview-section">
        <h4>核心结论</h4>
        <ul>${findings.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>暂无结论</li>"}</ul>
        <h4>业务解释</h4>
        <p>${escapeHtml(slide.narrative || "暂无说明")}</p>
      </section>
      <section class="slide-preview-section">
        <h4>图表 / 数据预览：${escapeHtml(slide.chart_spec?.chart_type || "-")}</h4>
        <div class="slide-preview-chart">${chartHtml}</div>
        <h4>下一步建议</h4>
        <ul>${recommendations.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>暂无建议</li>"}</ul>
      </section>
    </div>
  `;
}

function bindTableWrapWheelScroll() {
  document.addEventListener(
    "wheel",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const wrap = target.closest(".table-wrap");
      if (!(wrap instanceof HTMLElement)) return;
      const hasHorizontalOverflow = wrap.scrollWidth - wrap.clientWidth > 1;
      if (!hasHorizontalOverflow) return;

      const canScrollVertically = wrap.scrollHeight - wrap.clientHeight > 1;
      const hasHorizontalIntent = event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY);
      const shouldUseWheelAsHorizontal = hasHorizontalIntent || !canScrollVertically;
      if (!shouldUseWheelAsHorizontal) return;

      const delta = event.deltaX !== 0 ? event.deltaX : event.deltaY;
      if (delta === 0) return;
      const prev = wrap.scrollLeft;
      wrap.scrollLeft += delta;
      if (wrap.scrollLeft !== prev) event.preventDefault();
    },
    { passive: false }
  );
}

function bind() {
  bindTabs();
  bindTableWrapWheelScroll();
  setBackendBaseInput(normalizeBackendBase(state.backendBase));
  restoreTextInputs();
  renderTabs();
  renderSceneConfig();
  renderCreateSceneCollapse();
  renderSceneConfigCollapse();
  renderSceneFieldsCardCollapse();
  renderSceneRelationsCardCollapse();
  renderIntentTemplates();
  renderPptSchemeOptions();
  renderSlidePreview(null);
  loadPptSchemes();
  el("createSceneBtn").onclick = () => run(createScene);
  el("toggleCreateSceneBtn").onclick = () => {
    state.createSceneCollapsed = !state.createSceneCollapsed;
    renderCreateSceneCollapse();
  };
  el("createSessionBtn").onclick = () => run(createSession);
  el("runQueryBtn").onclick = () =>
    run(() => withAgentWait("sqlResult", "SQL 结果 Agent", runSqlResultAgentFromConfig));
  el("toggleIntentTemplatesBtn").onclick = () => {
    state.intentTemplatesCollapsed = !state.intentTemplatesCollapsed;
    renderIntentTemplates();
  };
  el("intentTemplateButtons").addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const btn = target.closest(".intent-template-btn");
    if (!(btn instanceof HTMLElement)) return;
    const intent = btn.dataset.intent || "";
    state.selectedPresetKey = btn.dataset.presetKey || "";
    state.selectedPresetQuestion = intent;
    fillIntentInputs(intent);
    persistUiState();
    if (el("queryIntentInput")) {
      el("queryIntentInput").focus();
      el("queryIntentInput").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });
  if (el("loadPlanBtn")) el("loadPlanBtn").onclick = () => run(loadPlan);
  if (el("executeQueryBtn")) el("executeQueryBtn").onclick = () => run(executeQuery);
  if (el("refreshQueryHistoryBtn")) el("refreshQueryHistoryBtn").onclick = () => run(refreshQueryHistory);
  if (el("refreshDeliveryHistoryBtn")) {
    el("refreshDeliveryHistoryBtn").onclick = () => run(async () => {
      await refreshQueryHistory();
      await loadReportStateForCurrentSession({ silent: false });
    });
  }
  if (el("loadSlideBtn")) el("loadSlideBtn").onclick = () => run(loadSlide);
  if (el("regenerateSlideBtn")) el("regenerateSlideBtn").onclick = () => run(regenerateSlide);
  if (el("approveSlideBtn")) el("approveSlideBtn").onclick = () => run(approveSlide);
  if (el("exportDeckBtn")) el("exportDeckBtn").onclick = () => run(exportDeck);
  if (el("saveSlideEditBtn")) el("saveSlideEditBtn").onclick = () => run(saveSlideEdits);
  if (el("resetSlideEditBtn")) el("resetSlideEditBtn").onclick = () => resetSlideEditor();
  if (el("pptSchemeSelect")) {
    el("pptSchemeSelect").onchange = () => {
      state.pptScheme = getSelectedPptScheme();
      renderPptSchemeHint();
      if (state.currentSession) run(loadSlide);
    };
  }
  if (el("backendBase")) el("backendBase").addEventListener("change", () => persistUiState());
  if (el("goalInput")) el("goalInput").addEventListener("input", () => persistUiState());
  if (el("queryIntentInput")) el("queryIntentInput").addEventListener("input", () => persistUiState());
  if (el("guideBtn")) el("guideBtn").onclick = () => el("guideDialog").showModal();
  if (el("closeGuideBtn")) el("closeGuideBtn").onclick = () => el("guideDialog").close();
  if (el("fieldRoleHelpBtn")) el("fieldRoleHelpBtn").onclick = () => el("fieldRoleHelpDialog").showModal();
  if (el("closeFieldRoleHelpBtn")) el("closeFieldRoleHelpBtn").onclick = () => el("fieldRoleHelpDialog").close();
  el("refreshConfigBtn").onclick = () => run(refreshSceneDetail);
  el("refreshDbCacheBtn").onclick = () => run(refreshDbCacheFromMysql);
  el("addFieldBtn").onclick = () => run(addSceneField);
  el("cancelEditFieldBtn").onclick = () => clearSemanticFieldForm();
  el("semanticCacheSearchBtn").onclick = () => {
    state.semanticCacheKeyword = el("semanticCacheKeyword").value.trim();
    renderSceneConfig();
  };
  el("semanticCacheSearchClearBtn").onclick = () => {
    state.semanticCacheKeyword = "";
    el("semanticCacheKeyword").value = "";
    renderSceneConfig();
  };
  el("semanticCacheJumpToFormBtn").onclick = () => {
    el("fieldSemanticName").focus();
    el("fieldSemanticName").scrollIntoView({ behavior: "smooth", block: "center" });
  };
  el("semanticCacheKeyword").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    state.semanticCacheKeyword = el("semanticCacheKeyword").value.trim();
    renderSceneConfig();
  });
  el("sceneFieldsWrap").addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const toggleBtn = target.closest(".semantic-toggle-btn");
    if (toggleBtn instanceof HTMLElement) {
      event.stopPropagation();
      const cacheId = toggleBtn.dataset.cacheId || "";
      const enabled = (toggleBtn.dataset.enabled || "1") === "1";
      if (cacheId) run(() => toggleSemanticCacheField(cacheId, enabled));
      return;
    }
    const deleteBtn = target.closest(".semantic-delete-btn");
    if (deleteBtn instanceof HTMLElement) {
      event.stopPropagation();
      const cacheId = deleteBtn.dataset.cacheId || "";
      if (cacheId) run(() => deleteSemanticCacheField(cacheId));
      return;
    }
    const row = target.closest(".semantic-cache-row");
    if (row instanceof HTMLElement) {
      event.stopPropagation();
      const cacheId = row.dataset.cacheId || "";
      if (cacheId) run(() => editSemanticCacheField(cacheId));
    }
  });
  el("addRelationBtn").onclick = () => run(addSceneRelation);
  el("sceneRelationsWrap").addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const deleteBtn = target.closest(".relation-delete-btn");
    if (deleteBtn instanceof HTMLElement) {
      const relationId = deleteBtn.dataset.relationId || "";
      if (relationId) run(() => deleteSceneRelation(relationId));
    }
  });
  el("llmRecommendBtn").onclick = () => run(() => withAgentWait("recommend", "推荐 Agent", recommendSceneByLlm));
  el("llmImportBtn").onclick = () => run(applySceneDraftFromLlm);
  if (el("llmSqlResultBtn")) {
    el("llmSqlResultBtn").onclick = () =>
      run(() => withAgentWait("sqlResult", "SQL 结果 Agent", runSqlResultAgentFromConfig));
  }
  el("llmFieldsSelectAllBtn").onclick = () => run(() => setAllLlmCandidates("field", true));
  el("llmFieldsSelectNoneBtn").onclick = () => run(() => setAllLlmCandidates("field", false));
  el("llmRelationsSelectAllBtn").onclick = () => run(() => setAllLlmCandidates("relation", true));
  el("llmRelationsSelectNoneBtn").onclick = () => run(() => setAllLlmCandidates("relation", false));
  el("llmCandidateFieldsWrap").addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.classList.contains("llm-candidate-check")) return;
    const candidateId = target.dataset.candidateId || "";
    if (!candidateId) return;
    run(() => setLlmCandidateSelected(target.dataset.kind || "field", candidateId, target.checked));
  });
  el("llmCandidateRelationsWrap").addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.classList.contains("llm-candidate-check")) return;
    const candidateId = target.dataset.candidateId || "";
    if (!candidateId) return;
    run(() => setLlmCandidateSelected(target.dataset.kind || "relation", candidateId, target.checked));
  });
  el("sceneConfigWrap").addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const removeBtn = target.closest(".selected-draft-remove-btn");
    if (!(removeBtn instanceof HTMLElement)) return;
    const candidateId = removeBtn.dataset.candidateId || "";
    const kind = removeBtn.dataset.kind || "field";
    if (!candidateId) return;
    run(() => removeSelectedDraftCandidate(kind, candidateId));
  });
  el("toggleScenesBtn").onclick = () => {
    state.sceneListCollapsed = !state.sceneListCollapsed;
    renderSceneListCollapse();
  };
  el("toggleScenesBtnOverview").onclick = () => {
    state.sceneListCollapsed = !state.sceneListCollapsed;
    renderSceneListCollapse();
  };
  el("toggleSceneConfigBtn").onclick = () => {
    state.sceneConfigCollapsed = !state.sceneConfigCollapsed;
    renderSceneConfigCollapse();
  };
  el("toggleSceneFieldsCardBtn").onclick = () => {
    state.sceneFieldsCardCollapsed = !state.sceneFieldsCardCollapsed;
    renderSceneFieldsCardCollapse();
  };
  el("toggleFieldAdvancedBtn").onclick = () => {
    state.fieldAdvancedOpen = !state.fieldAdvancedOpen;
    syncSceneAdvancedFieldState();
  };
  el("toggleSceneRelationsCardBtn").onclick = () => {
    state.sceneRelationsCardCollapsed = !state.sceneRelationsCardCollapsed;
    renderSceneRelationsCardCollapse();
  };
  el("toggleRelationAdvancedBtn").onclick = () => {
    state.relationAdvancedOpen = !state.relationAdvancedOpen;
    syncSceneAdvancedFieldState();
  };
  el("clothingSearchBtn").onclick = () => run(() => refreshClothingAll({ keepPage: false }));
  el("clothingResetBtn").onclick = () => run(async () => {
    resetClothingFilters();
    await refreshClothingAll({ keepPage: false });
  });
  el("clothingPrevBtn").onclick = () => run(async () => {
    state.clothing.offset = Math.max(0, state.clothing.offset - state.clothing.limit);
    await refreshClothingItems({ keepPage: true });
  });
  el("clothingNextBtn").onclick = () => run(async () => {
    state.clothing.offset += state.clothing.limit;
    await refreshClothingItems({ keepPage: true });
  });
  renderAgentWaitHint();
}

async function run(fn) {
  try {
    await fn();
  } catch (error) {
    alert(error.message || String(error));
  }
}

async function bootstrap() {
  await refreshScenes();
  await refreshSessions();
  await refreshQueryHistory();
  if (state.currentSession?.session_id) {
    await loadReportStateForCurrentSession({ silent: true });
  }
  refreshClothingAll().catch(console.error);
  refreshLlmCacheStatus().catch(console.error);
}

loadStoredUiState();
bind();
bootstrap().catch(console.error);
