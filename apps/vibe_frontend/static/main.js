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
  priceBandMode: "adaptive",
  priceBandPolicy: {
    mode: "adaptive",
    bucket_count: 10,
    strategy: "equal_width",
    boundary: {
      enabled: true,
      rounding: "auto",
      open_ended: true,
      custom_boundaries: [],
    },
  },
  currentSceneDetail: null,
  currentScenePlaybook: null,
  currentSceneSchemaSnapshot: null,
  currentSceneSchemaIndex: {
    tables: [],
    tableMap: new Map(),
    fieldsByTable: new Map(),
  },
  currentSceneSchemaLoadError: "",
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
  fieldResolution: {
    analysis: null,
    intent: "",
    selections: {},
  },
  fieldResolutionAutoTimerId: 0,
  fieldResolutionRequestId: 0,
  fieldResolutionExecutionIntent: "",
  inputCorrectionLexicon: [],
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

function normalizeSchemaKey(value) {
  return String(value || "").trim().toLowerCase();
}

function buildSceneSchemaIndex(snapshot) {
  const tables = Array.isArray(snapshot?.tables) ? snapshot.tables : [];
  const tableMap = new Map();
  const fieldsByTable = new Map();
  const normalizedTables = [];

  tables.forEach((table) => {
    if (!table || typeof table !== "object") return;
    const tableName = String(table.table_name || "").trim();
    if (!tableName) return;
    const tableKey = normalizeSchemaKey(tableName);
    const fieldNames = [];
    const fieldMap = new Map();
    const fields = Array.isArray(table.fields) ? table.fields : [];
    fields.forEach((field) => {
      if (!field || typeof field !== "object") return;
      const fieldName = String(field.field_name || "").trim();
      if (!fieldName) return;
      const fieldKey = normalizeSchemaKey(fieldName);
      if (!fieldKey || fieldMap.has(fieldKey)) return;
      fieldMap.set(fieldKey, fieldName);
      fieldNames.push(fieldName);
    });
    normalizedTables.push({
      table_name: tableName,
      table_comment: String(table.table_comment || "").trim(),
      fields: fieldNames,
    });
    tableMap.set(tableKey, {
      table_name: tableName,
      table_comment: String(table.table_comment || "").trim(),
      fields: fieldNames,
      fieldMap,
    });
    fieldsByTable.set(tableKey, fieldMap);
  });

  return {
    tables: normalizedTables,
    tableMap,
    fieldsByTable,
  };
}

function renderDatalistOptions(id, values) {
  const target = el(id);
  if (!target) return;
  const uniqueValues = [];
  const seen = new Set();
  (Array.isArray(values) ? values : []).forEach((value) => {
    const text = String(value || "").trim();
    if (!text) return;
    const key = text.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    uniqueValues.push(text);
  });
  target.innerHTML = uniqueValues.map((value) => `<option value="${escapeHtml(value)}"></option>`).join("");
}

function resolveSceneSchemaTable(tableName) {
  const index = state.currentSceneSchemaIndex;
  if (!index || typeof index !== "object") return null;
  const tableKey = normalizeSchemaKey(tableName);
  if (!tableKey) return null;
  return index.tableMap?.get(tableKey) || null;
}

function resolveSceneSchemaField(tableName, fieldName) {
  const table = resolveSceneSchemaTable(tableName);
  if (!table) return null;
  const fieldKey = normalizeSchemaKey(fieldName);
  if (!fieldKey) return null;
  return table.fieldMap?.get(fieldKey) || null;
}

function syncSceneSchemaInputLists() {
  const index = state.currentSceneSchemaIndex;
  const tableNames = Array.isArray(index?.tables) ? index.tables.map((item) => item.table_name) : [];
  renderDatalistOptions("sceneTableOptions", tableNames);

  const tableInputs = ["fieldTableName", "relationLeftTable", "relationRightTable"];
  tableInputs.forEach((id) => {
    const input = el(id);
    if (input) input.setAttribute("list", "sceneTableOptions");
  });

  const fieldTargets = [
    ["fieldName", "fieldTableName", "fieldNameOptions"],
    ["relationLeftField", "relationLeftTable", "relationLeftFieldOptions"],
    ["relationRightField", "relationRightTable", "relationRightFieldOptions"],
  ];
  fieldTargets.forEach(([fieldInputId, tableInputId, datalistId]) => {
    const fieldInput = el(fieldInputId);
    if (fieldInput) fieldInput.setAttribute("list", datalistId);
    const tableValue = el(tableInputId)?.value || "";
    const fields = resolveSceneSchemaTable(tableValue)?.fields || [];
    renderDatalistOptions(datalistId, fields);
  });
}

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
  const rawPriceBandPolicy = saved.priceBandPolicy && typeof saved.priceBandPolicy === "object" ? saved.priceBandPolicy : {};
  const priceBandMode = String(rawPriceBandPolicy.mode || saved.priceBandMode || "").trim();
  const hasStoredPriceBandBoundary =
    Boolean(rawPriceBandPolicy.boundary) ||
    Object.prototype.hasOwnProperty.call(rawPriceBandPolicy, "boundary_enabled") ||
    Object.prototype.hasOwnProperty.call(rawPriceBandPolicy, "boundary_rounding") ||
    Object.prototype.hasOwnProperty.call(rawPriceBandPolicy, "custom_boundaries") ||
    Object.prototype.hasOwnProperty.call(rawPriceBandPolicy, "boundaries");
  state.backendBase = normalizeBackendBase(backendBase || state.backendBase);
  if (sceneId) state.currentSceneId = sceneId;
  if (sessionId) state.restoreSessionId = sessionId;
  if (VALID_TABS.has(activeTab)) state.activeTab = activeTab;
  if (pptScheme) state.pptScheme = pptScheme;
  state.priceBandPolicy = normalizePriceBandPolicyRaw({
    mode: priceBandMode || state.priceBandPolicy.mode,
    bucket_count: rawPriceBandPolicy.bucket_count ?? rawPriceBandPolicy.adaptive_bucket_count ?? state.priceBandPolicy.bucket_count,
    strategy: rawPriceBandPolicy.strategy || state.priceBandPolicy.strategy,
    boundary: hasStoredPriceBandBoundary ? rawPriceBandPolicy.boundary || {} : state.priceBandPolicy.boundary,
    boundary_enabled: rawPriceBandPolicy.boundary_enabled,
    boundary_rounding: rawPriceBandPolicy.boundary_rounding,
    custom_boundaries: rawPriceBandPolicy.custom_boundaries || rawPriceBandPolicy.boundaries || [],
  });
  state.priceBandMode = state.priceBandPolicy.mode;
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
      priceBandMode: normalizePriceBandMode(state.priceBandPolicy?.mode || state.priceBandMode),
      priceBandPolicy: normalizePriceBandPolicyRaw(state.priceBandPolicy),
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

function normalizePriceBandMode(value) {
  return "adaptive";
}

function normalizePriceBandBucketCount(value, fallback = 5) {
  const fallbackCount = Number.isFinite(Number(fallback)) ? Number.parseInt(String(fallback), 10) : 5;
  const safeFallback = Number.isFinite(fallbackCount) && fallbackCount > 1 ? fallbackCount : 5;
  const raw = String(value ?? "").trim();
  if (!raw) return safeFallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 1) return safeFallback;
  return Math.min(Math.max(parsed, 2), 20);
}

function normalizePriceBandStrategy(value) {
  const strategy = String(value || "").trim().toLowerCase();
  if (strategy === "equal_width" || strategy === "rounded_width") return "equal_width";
  return "quantile";
}

function parsePriceBandCustomBoundaries(value) {
  const rawValues = Array.isArray(value)
    ? value
    : String(value || "")
        .split(/[,，、\s]+/)
        .filter(Boolean);
  const values = rawValues
    .map((item) => Number.parseFloat(String(item).trim()))
    .filter((item) => Number.isFinite(item));
  return Array.from(new Set(values)).sort((a, b) => a - b);
}

function normalizePriceBandBoundaryRounding(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "100" || raw === "hundred" || raw === "整百") return "hundred";
  if (raw === "1000" || raw === "thousand" || raw === "整千") return "thousand";
  return "auto";
}

function normalizePriceBandBoolean(value, fallback = false) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  const raw = String(value ?? "").trim().toLowerCase();
  if (!raw) return fallback;
  if (["1", "true", "yes", "on", "开启", "启用", "是"].includes(raw)) return true;
  if (["0", "false", "no", "off", "关闭", "禁用", "否"].includes(raw)) return false;
  return fallback;
}

function normalizePriceBandBoundary(rawBoundary = {}, fallbackBoundary = {}) {
  const boundary = rawBoundary && typeof rawBoundary === "object" ? rawBoundary : {};
  const enabledValue = boundary.enabled ?? rawBoundary?.boundary_enabled ?? fallbackBoundary.enabled ?? false;
  return {
    enabled: normalizePriceBandBoolean(enabledValue, Boolean(fallbackBoundary.enabled)),
    rounding: normalizePriceBandBoundaryRounding(boundary.rounding ?? rawBoundary?.boundary_rounding ?? fallbackBoundary.rounding ?? "auto"),
    open_ended: normalizePriceBandBoolean(boundary.open_ended ?? fallbackBoundary.open_ended ?? true, true),
    custom_boundaries: parsePriceBandCustomBoundaries(
      boundary.custom_boundaries ?? rawBoundary?.custom_boundaries ?? rawBoundary?.boundaries ?? fallbackBoundary.custom_boundaries ?? [],
    ),
  };
}

function formatPriceBandCustomBoundaries(boundaries) {
  return parsePriceBandCustomBoundaries(boundaries)
    .map((item) => (Number.isInteger(item) ? String(item) : String(Number(item.toFixed(2)))))
    .join(", ");
}

function normalizePriceBandPolicyRaw(policy = {}) {
  const rawStrategy = String(policy.strategy || "").trim().toLowerCase();
  const migratedRoundedBoundary = rawStrategy === "rounded_width";
  const strategy = migratedRoundedBoundary ? "equal_width" : normalizePriceBandStrategy(policy.strategy || "equal_width");
  const boundary = normalizePriceBandBoundary(
    {
      ...(policy.boundary && typeof policy.boundary === "object" ? policy.boundary : {}),
      enabled: policy.boundary?.enabled ?? policy.boundary_enabled ?? migratedRoundedBoundary,
      rounding: policy.boundary?.rounding ?? policy.boundary_rounding,
      custom_boundaries: policy.boundary?.custom_boundaries ?? policy.custom_boundaries ?? policy.boundaries,
    },
    { enabled: false, rounding: "auto", open_ended: true, custom_boundaries: [] },
  );
  if (strategy !== "equal_width") boundary.enabled = false;
  return {
    mode: normalizePriceBandMode(policy.mode || "adaptive"),
    bucket_count: normalizePriceBandBucketCount(policy.bucket_count ?? policy.adaptive_bucket_count ?? 10, 10),
    strategy,
    boundary,
  };
}

function getPriceBandPolicyDefaults() {
  const policy = state.currentScenePlaybook?.price_band_policy || {};
  const rawTemplate = Array.isArray(state.currentScenePlaybook?.price_band_template)
    ? state.currentScenePlaybook.price_band_template.filter((item) => item && typeof item === "object")
    : [];
  const modeOptions = Array.isArray(policy.mode_options)
    ? policy.mode_options
        .map((item) => String(item || "").trim().toLowerCase())
        .filter((item) => item === "adaptive")
    : [];
  return {
    defaultMode: normalizePriceBandMode(policy.default_mode || "adaptive"),
    defaultBucketCount: normalizePriceBandBucketCount(policy.adaptive_bucket_count || 10, 10),
    defaultStrategy: normalizePriceBandStrategy(policy.strategy || "equal_width"),
    defaultBoundary: normalizePriceBandBoundary(policy.boundary || {}, {
      enabled: false,
      rounding: "auto",
      open_ended: true,
      custom_boundaries: [],
    }),
    fixedTemplate: rawTemplate,
    modeOptions: modeOptions.length ? modeOptions : ["adaptive"],
  };
}

function normalizePriceBandPolicy(policy = {}) {
  const defaults = getPriceBandPolicyDefaults();
  const next = normalizePriceBandPolicyRaw({
    mode: policy.mode || defaults.defaultMode,
    bucket_count: policy.bucket_count ?? policy.adaptive_bucket_count ?? defaults.defaultBucketCount,
    strategy: policy.strategy || defaults.defaultStrategy,
    boundary: normalizePriceBandBoundary(policy.boundary || {}, defaults.defaultBoundary),
  });
  if (!defaults.modeOptions.includes(next.mode)) {
    next.mode = defaults.modeOptions.includes(defaults.defaultMode) ? defaults.defaultMode : defaults.modeOptions[0] || "adaptive";
  }
  return next;
}

function formatPriceBandModeLabel(mode) {
  const raw = String(mode || "").trim().toLowerCase();
  return raw === "fixed" ? "固定模板（历史兼容）" : "自定义分桶";
}

function formatPriceBandStrategyLabel(strategy) {
  const normalized = normalizePriceBandStrategy(strategy);
  if (normalized === "equal_width") return "等宽";
  return "分位数";
}

function describePriceBandStrategy(strategy) {
  const normalized = normalizePriceBandStrategy(strategy);
  if (normalized === "equal_width") {
    return "按当前分组内的最低价到最高价等宽切分，价格区间宽度一致，但每个区间的SKU数可能差异很大。";
  }
  if (normalized === "quantile") {
    return "按当前分组内的不同价格点排序做分位切分，同一个价格不会被拆到不同价格带；桶数越大越细，但每桶SKU数可能不同。";
  }
  return "";
}

function getSelectedPriceBandPolicy() {
  const defaults = getPriceBandPolicyDefaults();
  const activeButton = document.querySelector("#priceBandModeToggle .price-band-mode-btn.is-active");
  const activeStrategyButton = document.querySelector("#priceBandStrategyToggle .price-band-strategy-btn.is-active");
  const bucketInput = el("priceBandBucketCountInput");
  const policy = {
    mode: activeButton?.dataset?.priceBandMode || state.priceBandPolicy?.mode || defaults.defaultMode,
    bucket_count: bucketInput?.value || state.priceBandPolicy?.bucket_count || defaults.defaultBucketCount,
    strategy: activeStrategyButton?.dataset?.priceBandStrategy || state.priceBandPolicy?.strategy || defaults.defaultStrategy,
    boundary: {
      enabled: (document.querySelector("#priceBandBoundaryToggle .price-band-boundary-btn.is-active")?.dataset?.priceBandBoundary || "") === "rounded",
      rounding: document.querySelector("#priceBandRoundToggle .price-band-round-btn.is-active")?.dataset?.priceBandRounding || state.priceBandPolicy?.boundary?.rounding || defaults.defaultBoundary.rounding,
      open_ended: true,
      custom_boundaries: parsePriceBandCustomBoundaries(el("priceBandBoundariesInput")?.value || state.priceBandPolicy?.boundary?.custom_boundaries || []),
    },
  };
  return state.currentScenePlaybook ? normalizePriceBandPolicy(policy) : normalizePriceBandPolicyRaw(policy);
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
    "按品牌统计价格带分布和占比，默认自定义分桶，可设置桶数、策略和边界",
    "最近抓取批次中各品牌平均价最高的是哪些，返回品牌、SKU数、平均价、最高价",
    "各来源站点域名的品牌覆盖、SKU数和平均价差异是什么",
    "各品牌在不同场景标签下的平均价差异是多少，注意这不是平台价差",
  ],
  "品牌平台价格分析": [
    "各品牌的SKU数、平均价、最低价、最高价和价格跨度是多少，按价格跨度降序返回前20",
    "各一级类目和二级类目的SKU数、平均价和价格跨度是多少，按SKU数降序返回前30",
    "按品牌统计价格带分布和占比，默认自定义分桶，可设置桶数、策略和边界",
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

async function restoreLatestQueryResultFocus() {
  try {
    const payload = await api("/api/v1/sql-result-agent/history");
    const items = Array.isArray(payload?.items) ? payload.items : [];
    const latest = items.find((entry) => {
      const session = getHistorySession(entry);
      return Boolean(session?.session_id && session?.scene_id && entry?.query_run);
    });
    if (!latest) return false;
    const session = getHistorySession(latest);
    state.currentSceneId = String(session.scene_id || "").trim();
    state.restoreSessionId = String(session.session_id || "").trim();
    state.activeTab = "query";
    return true;
  } catch (error) {
    console.warn("restore latest query result focus failed", error);
    return false;
  }
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
  renderPriceBandModeControl();
}

function getSelectedPriceBandMode() {
  return getSelectedPriceBandPolicy().mode;
}

function setPriceBandMode(mode) {
  setPriceBandPolicy({ mode });
}

function setPriceBandPolicy(partial = {}) {
  const nextPolicy = { ...state.priceBandPolicy, ...partial };
  state.priceBandPolicy = state.currentScenePlaybook ? normalizePriceBandPolicy(nextPolicy) : normalizePriceBandPolicyRaw(nextPolicy);
  state.priceBandMode = state.priceBandPolicy.mode;
  renderPriceBandModeControl();
}

function currentQueryIntentText() {
  return String(el("queryIntentInput")?.value || "").trim();
}

function isPriceBandIntent(intent) {
  const text = String(intent || "").trim();
  if (!text) return false;
  const compact = normalizeIntent(text);
  const compactEnglish = text.toLowerCase().replace(/[\s_\-]+/g, "");
  return (
    text.includes("价格带") ||
    text.includes("分桶") ||
    text.includes("价格区间") ||
    text.includes("价格段") ||
    text.includes("价位段") ||
    text.includes("价位带") ||
    text.includes("价位区间") ||
    text.includes("价格分层") ||
    text.includes("价格层次") ||
    compact.includes("价格分布") ||
    compactEnglish.includes("priceband") ||
    compactEnglish.includes("pricebucket") ||
    compactEnglish.includes("pricerange") ||
    compactEnglish.includes("pricegroup")
  );
}

function shouldShowPriceBandControls(intent = currentQueryIntentText()) {
  const text = String(intent || "").trim();
  if (!text) return false;
  if (isPriceBandIntent(text)) return true;
  return Boolean(
    state.selectedPresetKey &&
      state.selectedPresetQuestion &&
      normalizeIntent(text) === normalizeIntent(state.selectedPresetQuestion) &&
      isPriceBandIntent(state.selectedPresetQuestion),
  );
}

function renderPriceBandModeControl() {
  const toolbar = document.querySelector(".query-band-toolbar");
  const visible = shouldShowPriceBandControls();
  if (toolbar) toolbar.hidden = !visible;
  const modeButtons = Array.from(document.querySelectorAll("#priceBandModeToggle .price-band-mode-btn"));
  const strategyButtons = Array.from(document.querySelectorAll("#priceBandStrategyToggle .price-band-strategy-btn"));
  const boundaryButtons = Array.from(document.querySelectorAll("#priceBandBoundaryToggle .price-band-boundary-btn"));
  const roundButtons = Array.from(document.querySelectorAll("#priceBandRoundToggle .price-band-round-btn"));
  const bucketInput = el("priceBandBucketCountInput");
  const boundariesInput = el("priceBandBoundariesInput");
  const bucketControl = el("priceBandBucketCountControl");
  const strategyControl = el("priceBandStrategyControl");
  const boundaryControl = el("priceBandBoundaryControl");
  const roundUnitControl = el("priceBandRoundUnitControl");
  const boundariesControl = el("priceBandBoundariesControl");
  const hint = el("priceBandModeHint");
  const summary = el("priceBandPolicySummary");
  if (!visible) {
    if (hint) hint.textContent = "";
    if (summary) summary.innerHTML = "";
  }
  const nextPolicy = state.currentScenePlaybook ? normalizePriceBandPolicy(state.priceBandPolicy) : normalizePriceBandPolicyRaw(state.priceBandPolicy);
  for (const button of modeButtons) {
    const mode = button.dataset.priceBandMode || "";
    const active = mode === nextPolicy.mode;
    button.classList.toggle("is-active", active);
    button.disabled = mode !== "adaptive";
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.title = "通过桶数、策略和边界配置价格带；固定区间可用手动边界表达";
  }
  if (bucketInput) bucketInput.value = String(nextPolicy.bucket_count);
  if (boundariesInput) boundariesInput.value = formatPriceBandCustomBoundaries(nextPolicy.boundary?.custom_boundaries || []);
  if (bucketControl) bucketControl.hidden = false;
  if (strategyControl) strategyControl.hidden = false;
  const boundaryAvailable = nextPolicy.strategy === "equal_width";
  const boundaryEnabled = Boolean(nextPolicy.boundary?.enabled) && boundaryAvailable;
  if (boundaryControl) boundaryControl.hidden = !boundaryAvailable;
  if (roundUnitControl) roundUnitControl.hidden = !boundaryEnabled;
  if (boundariesControl) boundariesControl.hidden = !boundaryEnabled;
  for (const button of strategyButtons) {
    const strategy = normalizePriceBandStrategy(button.dataset.priceBandStrategy || "quantile");
    const active = strategy === nextPolicy.strategy;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.title =
      strategy === "equal_width"
        ? "按每组最低价到最高价均分价格范围"
        : "按每组价格排序做分位切分";
  }
  for (const button of boundaryButtons) {
    const mode = button.dataset.priceBandBoundary || "raw";
    const active = boundaryEnabled ? mode === "rounded" : mode === "raw";
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.title =
      mode === "rounded"
        ? "按整百/整千或手动边界输出价格带，首尾使用以下/以上"
        : "按等宽计算出的原始小数边界输出";
  }
  for (const button of roundButtons) {
    const rounding = normalizePriceBandBoundaryRounding(button.dataset.priceBandRounding || "auto");
    const active = rounding === normalizePriceBandBoundaryRounding(nextPolicy.boundary?.rounding || "auto");
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }
  state.priceBandPolicy = nextPolicy;
  state.priceBandMode = nextPolicy.mode;
  if (!visible) {
    persistUiState();
    return;
  }
  if (hint) {
    hint.textContent = "当前生效的是自定义分桶；改完桶数、策略或边界后直接生成并执行，这次设置会一起保存到历史。";
  }
  if (summary) {
    const strategyLabel = formatPriceBandStrategyLabel(nextPolicy.strategy);
    const strategyDescription = describePriceBandStrategy(nextPolicy.strategy);
    const customBoundaries = formatPriceBandCustomBoundaries(nextPolicy.boundary?.custom_boundaries || []);
    const boundarySummary = boundaryEnabled
      ? `边界处理已开启：系统会按 ${nextPolicy.boundary?.rounding === "hundred" ? "整百" : nextPolicy.boundary?.rounding === "thousand" ? "整千" : "自动整百/整千"} 修正区间；${
          customBoundaries ? `当前中间边界为 ${customBoundaries}，` : "未填写中间边界时自动生成，"
        }首尾输出 XX元以下 / XX元以上。`
      : "边界处理未开启：等宽策略会直接展示计算出的原始区间边界。";
    const boundaryUsage =
      nextPolicy.strategy === "equal_width"
        ? boundarySummary
        : "分位数策略不使用手动边界；需要固定区间或边界输入时请切换到等宽，并开启边界处理。";
    summary.innerHTML = `
      <div class="query-band-detail is-active">
        <strong>当前生效：自定义分桶</strong>
        <span>SQL 会基于当前查询过滤后的数据，在每个分组内最多生成 ${nextPolicy.bucket_count} 个价格带；桶数越大，价格层次越细，返回行数通常也会增加。</span>
        <span>当前策略：${escapeHtml(strategyLabel)}。${escapeHtml(strategyDescription)}</span>
        <span>${escapeHtml(boundaryUsage)}</span>
        <span>需要固定区间时，不再切换模式，直接在“中间边界”输入区间边界，例如 100,200,400,800。</span>
      </div>`;
  }
  persistUiState();
}

function resetFieldResolutionState() {
  state.fieldResolutionRequestId += 1;
  if (state.fieldResolutionAutoTimerId) {
    window.clearTimeout(state.fieldResolutionAutoTimerId);
    state.fieldResolutionAutoTimerId = 0;
  }
  state.fieldResolution = {
    analysis: null,
    intent: "",
    selections: {},
  };
  state.fieldResolutionExecutionIntent = "";
  renderFieldResolutionPanel();
}

function getIntentCorrections() {
  const corrections = state.fieldResolution.analysis?.corrections;
  return Array.isArray(corrections)
    ? corrections.filter(
        (item) =>
          item &&
          String(item.term || "").trim() &&
          String(item.suggested_word || "").trim(),
      )
    : [];
}

function normalizeCorrectionComparableText(value) {
  return String(value || "").normalize("NFKC").toLocaleLowerCase();
}

function findCorrectionRanges(intent, corrections) {
  const text = String(intent || "");
  const normalizedText = normalizeCorrectionComparableText(text);
  const ranges = [];
  for (const correction of corrections) {
    const rawTerm = String(correction?.term || "").trim();
    if (!rawTerm) continue;
    const normalizedTerm = normalizeCorrectionComparableText(rawTerm);
    if (!normalizedTerm) continue;
    let start = 0;
    while (start < normalizedText.length) {
      const index = normalizedText.indexOf(normalizedTerm, start);
      if (index < 0) break;
      ranges.push({ start: index, end: index + normalizedTerm.length });
      start = index + normalizedTerm.length;
    }
  }
  ranges.sort((left, right) => left.start - right.start || right.end - right.start - (left.end - left.start));
  const accepted = [];
  for (const range of ranges) {
    const last = accepted[accepted.length - 1];
    if (last && range.start < last.end) continue;
    accepted.push(range);
  }
  return accepted;
}

function renderQueryIntentHighlight() {
  const input = el("queryIntentInput");
  const overlay = el("queryIntentHighlight");
  const wrap = el("queryIntentInputWrap");
  if (!input || !overlay || !wrap) return;
  const intent = String(input.value || "");
  const corrections = getIntentCorrections();
  const ranges = findCorrectionRanges(intent, corrections);
  if (!ranges.length) {
    overlay.textContent = "";
    wrap.classList.remove("has-correction-highlights");
    return;
  }
  let cursor = 0;
  const markup = [];
  for (const range of ranges) {
    markup.push(escapeHtml(intent.slice(cursor, range.start)));
    markup.push(`<mark>${escapeHtml(intent.slice(range.start, range.end))}</mark>`);
    cursor = range.end;
  }
  markup.push(escapeHtml(intent.slice(cursor)));
  overlay.innerHTML = markup.join("");
  overlay.scrollTop = input.scrollTop;
  overlay.scrollLeft = input.scrollLeft;
  wrap.classList.add("has-correction-highlights");
}

function renderIntentCorrectionPanel() {
  const panel = el("intentCorrectionPanel");
  const summary = el("intentCorrectionSummary");
  const list = el("intentCorrectionList");
  if (!panel || !summary || !list) return;
  const corrections = getIntentCorrections();
  renderQueryIntentHighlight();
  if (!corrections.length) {
    panel.hidden = true;
    summary.textContent = "";
    list.innerHTML = "";
    return;
  }
  panel.hidden = false;
  summary.textContent = `发现 ${corrections.length} 处建议，勾选后才会改写问题。`;
  list.innerHTML = corrections
    .map((item, index) => {
      const original = String(item.term || "").trim();
      const suggested = String(item.suggested_word || "").trim();
      const confidence = Number(item.confidence || item.score || 0);
      const detail = [
        `原词：${original}`,
        `建议：${suggested}`,
        `原因：${String(item.reason || "匹配人工纠错词库")}`,
        `置信度：${confidence.toFixed(2)}`,
      ].join("\n");
      return `
        <button
          type="button"
          class="intent-correction-apply"
          data-apply-intent-correction="${index}"
          data-tooltip="${escapeHtml(detail)}"
          title="${escapeHtml(detail)}"
          aria-label="使用纠正：${escapeHtml(original)} 改为 ${escapeHtml(suggested)}"
        >
          <span class="intent-correction-check" aria-hidden="true">☐</span>
          <span class="intent-correction-original">${escapeHtml(original)}</span>
          <span class="intent-correction-arrow" aria-hidden="true">→</span>
          <span class="intent-correction-target">${escapeHtml(suggested)}</span>
        </button>
      `;
    })
    .join("");
}

function applyIntentCorrection(correctionIndex) {
  const corrections = getIntentCorrections();
  const correction = corrections[Number(correctionIndex)];
  const input = el("queryIntentInput");
  if (!correction || !input) return;
  const rawTerm = String(correction.term || "").trim();
  const suggestedWord = String(correction.suggested_word || "").trim();
  const currentIntent = String(input.value || "");
  if (!rawTerm || !suggestedWord) {
    return;
  }
  const ranges = findCorrectionRanges(currentIntent, [correction]);
  if (!ranges.length) {
    if (el("querySaveHint")) el("querySaveHint").textContent = "原词已发生变化，请等待系统重新识别后再应用纠正。";
    return;
  }
  let correctedIntent = currentIntent;
  for (let index = ranges.length - 1; index >= 0; index -= 1) {
    const range = ranges[index];
    correctedIntent = `${correctedIntent.slice(0, range.start)}${suggestedWord}${correctedIntent.slice(range.end)}`;
  }
  input.value = correctedIntent;
  input.focus();
  renderPriceBandModeControl();
  persistUiState();
  scheduleAutoFieldResolutionAnalysis(input.value, { immediate: true });
}

function renderInputCorrectionLexicon() {
  const list = el("inputCorrectionLexiconList");
  const hint = el("inputCorrectionLexiconHint");
  if (!list || !hint) return;
  const items = Array.isArray(state.inputCorrectionLexicon) ? state.inputCorrectionLexicon : [];
  hint.textContent = items.length
    ? `已维护 ${items.length} 个正确写法。重复追加会自动合并并重新启用。`
    : "追加正确写法后，系统会在执行前把它作为纠错目标进行匹配。";
  list.innerHTML = items.length
    ? items
        .map((item) => {
          const id = String(item?.correction_id || "").trim();
          const word = String(item?.correct_word || "").trim();
          const enabled = item?.enabled !== false;
          if (!id || !word) return "";
          return `
            <div class="input-correction-lexicon-row${enabled ? "" : " is-disabled"}">
              <span class="input-correction-lexicon-word" title="${escapeHtml(word)}">${escapeHtml(word)}</span>
              <button
                type="button"
                class="secondary compact-btn"
                data-input-correction-toggle="${escapeHtml(id)}"
                data-input-correction-enabled="${enabled ? "false" : "true"}"
              >${enabled ? "停用" : "启用"}</button>
              <button type="button" class="danger compact-btn" data-input-correction-delete="${escapeHtml(id)}">删除</button>
            </div>
          `;
        })
        .join("")
    : `<p class="muted">暂无人工追加的正确写法。</p>`;
}

async function loadInputCorrectionLexicon() {
  const payload = await api("/api/v1/input-corrections?include_disabled=true");
  state.inputCorrectionLexicon = Array.isArray(payload?.items) ? payload.items : [];
  renderInputCorrectionLexicon();
  return state.inputCorrectionLexicon;
}

async function openInputCorrectionLexicon() {
  await loadInputCorrectionLexicon();
  const dialog = el("inputCorrectionLexiconDialog");
  if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
  el("inputCorrectionWord")?.focus();
}

async function addInputCorrectionWord() {
  const input = el("inputCorrectionWord");
  const word = String(input?.value || "").trim();
  if (!word) throw new Error("请输入纠错后的正确写法");
  const payload = await api("/api/v1/input-corrections", {
    method: "POST",
    body: JSON.stringify({ correct_word: word }),
  });
  const item = payload?.item;
  if (item && typeof item === "object") {
    const next = state.inputCorrectionLexicon.filter(
      (row) => String(row?.correction_id || "") !== String(item.correction_id || ""),
    );
    next.unshift(item);
    state.inputCorrectionLexicon = next;
  } else {
    await loadInputCorrectionLexicon();
  }
  if (input) input.value = "";
  renderInputCorrectionLexicon();
  if (el("inputCorrectionLexiconHint")) {
    el("inputCorrectionLexiconHint").textContent = `已追加正确写法：${String(item?.correct_word || word)}`;
  }
  scheduleAutoFieldResolutionAnalysis(currentFieldResolutionIntent(), { immediate: true });
}

async function updateInputCorrectionEnabled(correctionId, enabled) {
  await api(`/api/v1/input-corrections/${encodeURIComponent(correctionId)}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled: Boolean(enabled) }),
  });
  await loadInputCorrectionLexicon();
  scheduleAutoFieldResolutionAnalysis(currentFieldResolutionIntent(), { immediate: true });
}

async function deleteInputCorrectionWord(correctionId) {
  await api(`/api/v1/input-corrections/${encodeURIComponent(correctionId)}`, {
    method: "DELETE",
  });
  state.inputCorrectionLexicon = state.inputCorrectionLexicon.filter(
    (item) => String(item?.correction_id || "") !== String(correctionId || ""),
  );
  renderInputCorrectionLexicon();
  scheduleAutoFieldResolutionAnalysis(currentFieldResolutionIntent(), { immediate: true });
}

function normalizeFieldResolutionCandidateIndex(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  return raw;
}

function escapeRegexText(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function buildFieldResolutionIntentText(intent, analysis, selections) {
  let resolvedIntent = String(intent || "").trim();
  const terms = Array.isArray(analysis?.terms) ? analysis.terms : [];
  const replacements = [];
  for (const term of terms) {
    const termId = String(term?.term_id || "").trim();
    const selection = normalizeFieldResolutionCandidateIndex(selections?.[termId]);
    const matchIndex = Number.parseInt(selection, 10);
    const matches = Array.isArray(term?.matches) ? term.matches : [];
    const match = Number.isInteger(matchIndex) ? matches[matchIndex] : null;
    const canonicalValue = String(match?.canonical_value || "").trim();
    const termText = String(term?.text || "").trim();
    if (!canonicalValue || !termText || selection === "__ignore__") continue;
    let pattern = escapeRegexText(termText)
      .replace(/\\\(/g, "[（(]")
      .replace(/\\\)/g, "[）)]")
      .replace(/\\\[/g, "[【\\[]")
      .replace(/\\\]/g, "[】\\]]");
    if (!/[（(]/.test(termText) && !/[【\[]/.test(termText)) {
      pattern += "(?:\\s*[（(]\\s*[）)])?";
    }
    replacements.push({
      pattern: new RegExp(pattern, "gi"),
      canonicalValue,
      length: termText.length,
    });
  }
  replacements
    .sort((left, right) => right.length - left.length)
    .forEach((item) => {
      resolvedIntent = resolvedIntent.replace(item.pattern, item.canonicalValue);
    });
  return resolvedIntent;
}

function recommendedFieldResolutionCandidateIndex(term) {
  const matches = Array.isArray(term?.matches) ? term.matches : [];
  if (!matches.length) return "";
  const recommended = Number.parseInt(term?.recommended_match_index, 10);
  if (Number.isInteger(recommended) && recommended >= 0 && recommended < matches.length) {
    return String(recommended);
  }
  return "0";
}

function buildDefaultFieldResolutionSelections(analysis) {
  const selections = {};
  const terms = Array.isArray(analysis?.terms) ? analysis.terms : [];
  for (const term of terms) {
    const termId = String(term?.term_id || "").trim();
    if (!termId) continue;
    selections[termId] = recommendedFieldResolutionCandidateIndex(term);
  }
  return selections;
}

function fieldResolutionCandidateKey(match) {
  if (!match || typeof match !== "object") return "";
  return [
    match.semantic_name,
    match.table_name,
    match.field_name,
    match.canonical_value,
  ]
    .map((value) => normalizeIntent(value).toLowerCase())
    .join("|");
}

function captureFieldResolutionSelectionKeys(analysis, selections) {
  const result = {};
  const terms = Array.isArray(analysis?.terms) ? analysis.terms : [];
  for (const term of terms) {
    const termId = String(term?.term_id || "").trim();
    if (!termId) continue;
    const selection = normalizeFieldResolutionCandidateIndex(selections?.[termId]);
    if (selection === "__ignore__") {
      result[termId] = "__ignore__";
      continue;
    }
    const matchIndex = Number.parseInt(selection, 10);
    const matches = Array.isArray(term?.matches) ? term.matches : [];
    if (!Number.isInteger(matchIndex) || !matches[matchIndex]) continue;
    const key = fieldResolutionCandidateKey(matches[matchIndex]);
    if (key) result[termId] = key;
  }
  return result;
}

function setFieldResolutionAnalysis(analysis, intent) {
  state.fieldResolution = {
    analysis: analysis || null,
    intent: String(intent || "").trim(),
    selections: buildDefaultFieldResolutionSelections(analysis),
  };
  renderFieldResolutionPanel();
}

function appendFieldResolutionAnalysis(analysis, intent) {
  if (!analysis) return;
  const previousAnalysis =
    state.fieldResolution.analysis &&
    normalizeIntent(state.fieldResolution.intent) === normalizeIntent(intent)
      ? state.fieldResolution.analysis
      : null;
  const previousSelectionKeys = previousAnalysis
    ? captureFieldResolutionSelectionKeys(previousAnalysis, state.fieldResolution.selections)
    : {};
  const nextSelections = buildDefaultFieldResolutionSelections(analysis);
  for (const term of Array.isArray(analysis?.terms) ? analysis.terms : []) {
    const termId = String(term?.term_id || "").trim();
    const previousKey = previousSelectionKeys[termId];
    if (!termId || !previousKey) continue;
    if (previousKey === "__ignore__") {
      nextSelections[termId] = "__ignore__";
      continue;
    }
    const matches = Array.isArray(term?.matches) ? term.matches : [];
    const nextIndex = matches.findIndex((match) => fieldResolutionCandidateKey(match) === previousKey);
    if (nextIndex >= 0) nextSelections[termId] = String(nextIndex);
  }
  state.fieldResolution = {
    analysis,
    intent: String(intent || "").trim(),
    selections: nextSelections,
  };
  renderFieldResolutionPanel();
}

function syncFieldResolutionSelectionsFromDom() {
  const analysis = state.fieldResolution.analysis;
  const terms = Array.isArray(analysis?.terms) ? analysis.terms : [];
  const nextSelections = { ...state.fieldResolution.selections };
  for (const term of terms) {
    const termId = String(term?.term_id || "").trim();
    if (!termId) continue;
    const select = el(`fieldResolutionSelect_${termId}`);
    if (select) {
      nextSelections[termId] = normalizeFieldResolutionCandidateIndex(select.value);
      continue;
    }
    const picker = document.querySelector(`.field-resolution-picker[data-term-id="${CSS.escape(termId)}"]`);
    if (!picker) continue;
    nextSelections[termId] = normalizeFieldResolutionCandidateIndex(state.fieldResolution.selections[termId]);
  }
  state.fieldResolution.selections = nextSelections;
  persistUiState();
}

function closeFieldResolutionMenus(exceptPicker = null) {
  document.querySelectorAll(".field-resolution-picker").forEach((picker) => {
    if (exceptPicker && picker === exceptPicker) return;
    picker.classList.remove("is-open");
    const trigger = picker.querySelector("[data-field-resolution-trigger]");
    const menu = picker.querySelector(".field-resolution-menu");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (menu) menu.hidden = true;
  });
}

function buildFieldResolutionPayload() {
  const analysis = state.fieldResolution.analysis;
  const terms = Array.isArray(analysis?.terms) ? analysis.terms : [];
  if (!terms.length) {
    return {
      field_resolution: {
        intent: state.fieldResolution.intent || "",
        confirmed_resolutions: [],
        ignored_terms: [],
      },
      has_unresolved_required_terms: false,
    };
  }

  const confirmed_resolutions = [];
  const ignored_terms = [];
  let has_unresolved_required_terms = false;
  for (const term of terms) {
    const termId = String(term?.term_id || "").trim();
    const termText = String(term?.text || "").trim();
    const status = String(term?.status || "").trim();
    const selection = normalizeFieldResolutionCandidateIndex(state.fieldResolution.selections[termId]);
    if (!selection) {
      if (status === "ambiguous") {
        has_unresolved_required_terms = true;
      }
      continue;
    }
    if (selection === "__ignore__") {
      ignored_terms.push(termText);
      continue;
    }
    const matchIndex = Number.parseInt(selection, 10);
    const matches = Array.isArray(term?.matches) ? term.matches : [];
    const match = Number.isInteger(matchIndex) ? matches[matchIndex] : null;
    if (!match) {
      if (status === "ambiguous") {
        has_unresolved_required_terms = true;
      }
      continue;
    }
    confirmed_resolutions.push({
      term: termText,
      term_id: termId,
      semantic_name: match.semantic_name || "",
      table_name: match.table_name || "",
      field_name: match.field_name || "",
      canonical_value: match.canonical_value || "",
      score: match.score ?? 0,
      confidence: match.confidence ?? match.score ?? 0,
      strategy: match.strategy || "",
      raw_value: termText,
    });
  }
  return {
    field_resolution: {
      intent: state.fieldResolution.intent || "",
      confirmed_resolutions,
      ignored_terms,
    },
    has_unresolved_required_terms,
  };
}

function renderFieldResolutionPanel() {
  const panel = el("fieldResolutionPanel");
  const summary = el("fieldResolutionSummary");
  const list = el("fieldResolutionList");
  if (!panel || !summary || !list) return;
  const analysis = state.fieldResolution.analysis;
  const terms = Array.isArray(analysis?.terms) ? analysis.terms : [];
  renderIntentCorrectionPanel();
  if (!analysis || !terms.length) {
    panel.hidden = true;
    summary.textContent = "";
    list.innerHTML = "";
    return;
  }

  panel.hidden = false;
  const ambiguousCount = Array.isArray(analysis.ambiguous_terms) ? analysis.ambiguous_terms.length : 0;
  const resolvedCount = Array.isArray(analysis.resolved_terms) ? analysis.resolved_terms.length : 0;
  const inferredIntent = buildFieldResolutionIntentText(
    state.fieldResolution.intent,
    analysis,
    state.fieldResolution.selections,
  );
  const inferredIntentText =
    inferredIntent && normalizeIntent(inferredIntent) !== normalizeIntent(state.fieldResolution.intent)
      ? `识别意图：${inferredIntent}。`
      : "";
  summary.textContent = ambiguousCount
    ? `${inferredIntentText}字段已自动识别 ${analysis.matched_term_count || 0} 个候选词，其中 ${ambiguousCount} 个有多个候选，已默认选择第一项，可在下拉框中调整；${resolvedCount} 个已自动识别。`
    : `${inferredIntentText}字段已自动识别 ${analysis.matched_term_count || 0} 个候选词，未发现歧义，请点击“确认并生成SQL”继续。`;

  list.innerHTML = terms
    .map((term) => {
      const termId = String(term?.term_id || "").trim();
      const matches = Array.isArray(term?.matches) ? term.matches : [];
      const currentValue = normalizeFieldResolutionCandidateIndex(
        state.fieldResolution.selections[termId] ?? recommendedFieldResolutionCandidateIndex(term),
      );
      const choices = [
        {
          value: "",
          label: "请选择字段",
          detail: "先确认这个词对应哪个标准字段",
        },
        {
          value: "__ignore__",
          label: "不作为过滤条件",
          detail: "这个词只保留在问题文本中，不写入 WHERE",
        },
      ];
      matches.forEach((match, idx) => {
        const value = String(idx);
        choices.push({
          value,
          label: `${match.semantic_name || "-"} = ${match.canonical_value || "-"}`,
          detail: `${match.table_name || "-"}.${match.field_name || "-"} · 置信度 ${Number(match.confidence ?? match.score ?? 0).toFixed(2)}${Number(match.count || 0) ? ` · ${Number(match.count || 0)}条` : ""}`,
        });
      });
      const activeChoice = choices.find((choice) => choice.value === currentValue) || choices[0];
      const options = choices
        .map((choice) => {
          const active = choice.value === currentValue ? " is-active" : "";
          return `
            <button type="button" class="field-resolution-option${active}" data-field-resolution-value="${escapeHtml(choice.value)}">
              <span class="field-resolution-option-label">${escapeHtml(choice.label)}</span>
              <span class="field-resolution-option-detail">${escapeHtml(choice.detail)}</span>
            </button>
          `;
        })
        .join("");
      const reason = String(term?.ambiguity_reason || "").trim();
      const statusText =
        term?.status === "ambiguous"
          ? reason === "multiple_values"
            ? "同字段多个标准值命中，已默认选第一项"
            : "多个字段命中，已默认选第一项"
          : "已自动识别";
      return `
        <div class="field-resolution-row" data-term-id="${escapeHtml(termId)}">
          <div class="field-resolution-term">
            <strong>${escapeHtml(term?.text || "-")}</strong>
            <span>${escapeHtml(statusText)} · ${escapeHtml(term?.source || "-")}</span>
          </div>
          <div class="field-resolution-picker" data-term-id="${escapeHtml(termId)}">
            <button type="button" class="field-resolution-picker-trigger" data-field-resolution-trigger aria-haspopup="listbox" aria-expanded="false">
              <span class="field-resolution-picker-label">${escapeHtml(activeChoice.label)}</span>
              <span class="field-resolution-picker-detail">${escapeHtml(activeChoice.detail)}</span>
            </button>
            <div class="field-resolution-menu" role="listbox" hidden>
              ${options}
            </div>
          </div>
        </div>
      `;
    })
    .join("");
}

async function analyzeFieldResolutionForIntent(intent, requestId = state.fieldResolutionRequestId) {
  const sceneId = state.currentSceneId;
  if (!sceneId) return null;
  if (!String(intent || "").trim()) return null;
  const result = await apiStream(`/api/v1/sql-result-agent/scenes/${sceneId}/analyze-intent-stream`, {
    method: "POST",
    timeoutMs: 25000,
    body: JSON.stringify({
      intent,
      context: {
        source: "query_tab",
        scene_id: sceneId,
      },
    }),
  }, async (event) => {
    const isCurrentRequest =
      requestId === state.fieldResolutionRequestId &&
      normalizeIntent(currentFieldResolutionIntent()) === normalizeIntent(intent);
    if (!isCurrentRequest) return;
    if (event?.analysis) {
      appendFieldResolutionAnalysis(event.analysis, intent);
    }
    const executionStarted =
      state.fieldResolutionExecutionIntent === normalizeIntent(intent);
    if (el("querySaveHint") && event?.message && !executionStarted) {
      el("querySaveHint").textContent = event.message;
    }
    if (event?.type === "error") {
      const error = new Error(String(event.detail || "字段自动识别失败"));
      error.detail = event.detail || error.message;
      error.status = event.status;
      throw error;
    }
  });
  const analysis = result?.analysis || result;
  if (
    requestId !== state.fieldResolutionRequestId ||
    normalizeIntent(currentFieldResolutionIntent()) !== normalizeIntent(intent)
  ) {
    return null;
  }
  appendFieldResolutionAnalysis(analysis, intent);
  syncFieldResolutionSelectionsFromDom();
  return analysis;
}

function currentFieldResolutionIntent() {
  return normalizeIntent(
    (el("queryIntentInput")?.value || "").trim() ||
    (el("goalInput")?.value || "").trim() ||
    (el("llmGoal")?.value || "").trim(),
  );
}

async function autoAnalyzeFieldResolutionForIntent(intent, requestId) {
  const normalizedIntent = normalizeIntent(intent);
  if (!normalizedIntent || !state.currentSceneId || requestId !== state.fieldResolutionRequestId) {
    return null;
  }
  if (el("querySaveHint")) el("querySaveHint").textContent = "正在自动识别字段...";
  try {
    const analysis = await analyzeFieldResolutionForIntent(normalizedIntent, requestId);
    if (
      requestId !== state.fieldResolutionRequestId ||
      normalizeIntent(currentFieldResolutionIntent()) !== normalizedIntent
    ) {
      return null;
    }
    const terms = Array.isArray(analysis?.terms) ? analysis.terms : [];
    if (el("querySaveHint") && state.fieldResolutionExecutionIntent !== normalizedIntent) {
      el("querySaveHint").textContent = terms.length
        ? "字段已自动识别并默认选中，请确认后生成SQL。"
        : "字段已自动识别，未发现需要转换的标准值，可确认后生成SQL。";
    }
    return analysis;
  } catch (error) {
    if (requestId === state.fieldResolutionRequestId && el("querySaveHint")) {
      el("querySaveHint").textContent = `字段自动识别失败：${formatErrorDetail(error?.detail || error?.message || error)}`;
    }
    return null;
  }
}

function scheduleAutoFieldResolutionAnalysis(intent, { immediate = false } = {}) {
  const normalizedIntent = normalizeIntent(intent);
  resetFieldResolutionState();
  if (!normalizedIntent || !state.currentSceneId) {
    if (el("querySaveHint")) el("querySaveHint").textContent = "";
    return;
  }
  const requestId = state.fieldResolutionRequestId;
  const delay = immediate ? 0 : 350;
  state.fieldResolutionAutoTimerId = window.setTimeout(() => {
    state.fieldResolutionAutoTimerId = 0;
    autoAnalyzeFieldResolutionForIntent(normalizedIntent, requestId);
  }, delay);
}

async function ensureFieldResolutionAnalysisForCurrentIntent() {
  const intent = currentFieldResolutionIntent();
  if (!intent || !state.currentSceneId) return null;
  const existingAnalysis =
    state.fieldResolution.analysis &&
    normalizeIntent(state.fieldResolution.intent) === intent;
  if (existingAnalysis) return state.fieldResolution.analysis;

  resetFieldResolutionState();
  const requestId = state.fieldResolutionRequestId;
  return autoAnalyzeFieldResolutionForIntent(intent, requestId);
}

async function confirmAndGenerateSqlFromFieldResolution() {
  const intent = currentFieldResolutionIntent();
  if (!intent) throw new Error("请先输入业务问题或分析目标");
  const analysis = await ensureFieldResolutionAnalysisForCurrentIntent();
  if (!analysis) throw new Error("字段自动识别失败，请稍后重试");
  return runSqlResultAgentFromConfig({ skipFieldResolution: true });
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
    syncDraftSelectionState(draft);
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

function filterSessionsForKnownScenes(sessions) {
  const items = Array.isArray(sessions) ? sessions : [];
  const knownSceneIds = new Set(state.scenes.map((scene) => scene.scene_id).filter(Boolean));
  if (!knownSceneIds.size) return items;
  return items.filter((session) => !session?.scene_id || knownSceneIds.has(session.scene_id));
}

async function setCurrentSession(session, { loadThread = true } = {}) {
  state.currentSession = session || null;
  if (session?.scene_id) state.currentSceneId = session.scene_id;
  state.restoreSessionId = session?.session_id || "";
  resetFieldResolutionState();
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
    const label = sqlResultWait.label || "SQL 结果 Agent";
    waitItems.push(`${label}正在返回，已等待 ${sec}s`);
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
    runQueryBtn.textContent = busy ? "执行中..." : "确认并生成SQL";
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

function formatErrorDetail(detail) {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (typeof detail === "object") {
    if (typeof detail.detail === "string") return detail.detail;
    if (typeof detail.message === "string") return detail.message;
    try {
      return JSON.stringify(detail);
    } catch (_error) {
      return String(detail);
    }
  }
  return String(detail);
}

async function api(path, options = {}) {
  syncBackendBase();
  const timeoutMs = Number(options.timeoutMs || 0);
  const { timeoutMs: _timeoutMs, ...fetchOptions } = options;
  const requestOptions = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...fetchOptions,
  };
  const primaryBase = state.backendBase;
  const primaryUrl = buildApiUrl(primaryBase, path);
  let response;
  let timeoutId = 0;
  let controller = null;
  if (timeoutMs > 0 && typeof AbortController !== "undefined" && !requestOptions.signal) {
    controller = new AbortController();
    requestOptions.signal = controller.signal;
    timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  }
  try {
    response = await fetch(primaryUrl, requestOptions);
  } catch (error) {
    if (timeoutId) window.clearTimeout(timeoutId);
    if (error?.name === "AbortError") {
      const timeoutError = new Error(`请求超时（${Math.ceil(timeoutMs / 1000)}秒）：${path}`);
      timeoutError.detail = "字段自动识别超时，请检查后端日志、数据库索引或稍后重试。";
      timeoutError.path = path;
      timeoutError.url = primaryUrl;
      throw timeoutError;
    }
    const detail = error?.message || String(error);
    throw new Error(`请求接口失败：${primaryUrl}。请检查后端服务是否可达、接口是否超时或网络是否异常。${detail}`);
  }
  if (timeoutId) window.clearTimeout(timeoutId);
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      const payload = JSON.parse(text);
      detail = formatErrorDetail(payload?.detail || text);
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

async function apiStream(path, options = {}, onEvent = async () => {}) {
  syncBackendBase();
  const timeoutMs = Number(options.timeoutMs || 0);
  const { timeoutMs: _timeoutMs, ...fetchOptions } = options;
  const requestOptions = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...fetchOptions,
  };
  const primaryUrl = buildApiUrl(state.backendBase, path);
  let timeoutId = 0;
  let controller = null;
  if (timeoutMs > 0 && typeof AbortController !== "undefined" && !requestOptions.signal) {
    controller = new AbortController();
    requestOptions.signal = controller.signal;
    timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  }

  let response;
  try {
    response = await fetch(primaryUrl, requestOptions);
  } catch (error) {
    if (timeoutId) window.clearTimeout(timeoutId);
    if (error?.name === "AbortError") {
      const timeoutError = new Error(`请求超时（${Math.ceil(timeoutMs / 1000)}秒）：${path}`);
      timeoutError.detail = "字段自动识别超时，已显示的候选仍保留，可先确认已有字段。";
      throw timeoutError;
    }
    throw error;
  }
  if (!response.ok) {
    if (timeoutId) window.clearTimeout(timeoutId);
    const text = await response.text();
    let detail = text;
    try {
      const payload = JSON.parse(text);
      detail = formatErrorDetail(payload?.detail || text);
    } catch (_error) {
      detail = text;
    }
    const error = new Error(`${response.status} ${detail}`);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  if (!response.body || typeof response.body.getReader !== "function") {
    if (timeoutId) window.clearTimeout(timeoutId);
    throw new Error("浏览器不支持流式字段识别");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let completed = null;
  const consumeLines = async (flush = false) => {
    if (flush) buffer += decoder.decode();
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      const raw = line.slice(5).trim();
      if (!raw) continue;
      const event = JSON.parse(raw);
      await onEvent(event);
      if (event?.type === "complete") completed = event;
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      await consumeLines();
    }
    await consumeLines(true);
  } finally {
    if (timeoutId) window.clearTimeout(timeoutId);
    reader.releaseLock();
  }
  if (!completed) throw new Error("字段识别未返回完整结果");
  return completed;
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

function setCreateSceneHint(message) {
  const hint = el("createSceneHint");
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
  resetFieldResolutionState();
  clearReportStateViews();
  renderSessions();
  renderSessionHeader();
  renderQueryHistory();
  persistUiState();
}

function clearCurrentSceneDetailState() {
  state.currentSceneDetail = null;
  state.currentScenePlaybook = null;
  state.currentSceneSchemaSnapshot = null;
  state.currentSceneSchemaIndex = {
    tables: [],
    tableMap: new Map(),
    fieldsByTable: new Map(),
  };
  state.currentSceneSchemaLoadError = "";
  state.selectedPresetKey = "";
  state.selectedPresetQuestion = "";
  state.semanticCacheFields = [];
  state.editingSemanticCacheId = "";
  state.currentLlmAgentDraft = null;
  state.priceBandPolicy = {
    mode: "adaptive",
    bucket_count: 10,
    strategy: "equal_width",
    boundary: {
      enabled: true,
      rounding: "auto",
      open_ended: true,
      custom_boundaries: [],
    },
  };
  state.priceBandMode = state.priceBandPolicy.mode;
}

function setCurrentSceneDetailPlaceholder() {
  clearCurrentSceneDetailState();
  resetFieldResolutionState();
  if (!state.currentSceneId) return;
  state.currentSceneDetail = state.scenes.find((scene) => scene.scene_id === state.currentSceneId) || null;
  state.currentLlmAgentDraft = getSceneDraft(state.currentSceneId);
}

function ensureCurrentSceneFromList() {
  const currentId = String(state.currentSceneId || "").trim();
  if (currentId && state.scenes.some((scene) => scene.scene_id === currentId)) return currentId;
  state.currentSceneId = state.scenes[0]?.scene_id || "";
  return state.currentSceneId;
}

function renderSceneWorkspaceState() {
  renderScenes();
  renderSceneConfig();
  renderIntentTemplates();
  renderSessions();
  renderSessionHeader();
  renderQueryHistory();
  renderReportState();
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

function renderTitleCell(value, className = "", titleValue = value) {
  const text = String(value ?? "");
  const titleText = String(titleValue ?? text);
  const classes = className ? ` class="${className}"` : "";
  const tip = titleText ? ` data-hover-tip="${escapeHtml(titleText)}"` : "";
  return `<td${classes}${tip}>${escapeHtml(text)}</td>`;
}

const hoverTipState = {
  node: null,
  target: null,
  rafId: 0,
};

function ensureHoverTipNode() {
  if (hoverTipState.node) return hoverTipState.node;
  const node = document.createElement("div");
  node.className = "cell-hover-tip";
  node.hidden = true;
  document.body.appendChild(node);
  hoverTipState.node = node;
  return node;
}

function hideHoverTip() {
  if (hoverTipState.rafId) {
    cancelAnimationFrame(hoverTipState.rafId);
    hoverTipState.rafId = 0;
  }
  hoverTipState.target = null;
  if (!hoverTipState.node) return;
  hoverTipState.node.hidden = true;
  hoverTipState.node.style.opacity = "0";
}

function positionHoverTip(event) {
  const node = hoverTipState.node;
  if (!node || node.hidden) return;
  const gap = 14;
  const margin = 12;
  const rect = node.getBoundingClientRect();
  let left = event.clientX + gap;
  let top = event.clientY + gap;
  if (left + rect.width + margin > window.innerWidth) {
    left = Math.max(margin, event.clientX - rect.width - gap);
  }
  if (top + rect.height + margin > window.innerHeight) {
    top = Math.max(margin, event.clientY - rect.height - gap);
  }
  node.style.left = `${Math.max(margin, left)}px`;
  node.style.top = `${Math.max(margin, top)}px`;
}

function showHoverTip(target, event) {
  if (!(target instanceof HTMLElement)) return;
  const tipText = String(target.dataset.hoverTip || "").trim();
  if (!tipText) {
    hideHoverTip();
    return;
  }
  const node = ensureHoverTipNode();
  hoverTipState.target = target;
  node.textContent = tipText;
  node.hidden = false;
  node.style.opacity = "0";
  if (hoverTipState.rafId) cancelAnimationFrame(hoverTipState.rafId);
  hoverTipState.rafId = requestAnimationFrame(() => {
    hoverTipState.rafId = 0;
    if (hoverTipState.target !== target) return;
    positionHoverTip(event);
    node.style.opacity = "1";
  });
}

function bindHoverTips() {
  const updateFromEvent = (event) => {
    const target = event.target instanceof HTMLElement ? event.target.closest("[data-hover-tip]") : null;
    if (!target) {
      hideHoverTip();
      return;
    }
    if (hoverTipState.target !== target) {
      showHoverTip(target, event);
      return;
    }
    positionHoverTip(event);
  };

  document.addEventListener(
    "mouseover",
    (event) => {
      updateFromEvent(event);
    },
    true,
  );
  document.addEventListener(
    "mousemove",
    (event) => {
      if (!hoverTipState.target) return;
      const target = event.target instanceof HTMLElement ? event.target.closest("[data-hover-tip]") : null;
      if (!target || target !== hoverTipState.target) return;
      positionHoverTip(event);
    },
    true,
  );
  document.addEventListener(
    "mouseout",
    (event) => {
      const target = event.target instanceof HTMLElement ? event.target.closest("[data-hover-tip]") : null;
      if (!target || target !== hoverTipState.target) return;
      const related = event.relatedTarget instanceof HTMLElement ? event.relatedTarget.closest("[data-hover-tip]") : null;
      if (related === target) return;
      if (!related) hideHoverTip();
    },
    true,
  );
  document.addEventListener(
    "scroll",
    () => {
      if (hoverTipState.target) hideHoverTip();
    },
    true,
  );
  window.addEventListener("blur", hideHoverTip);
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
  const lineage = queryRun.lineage && typeof queryRun.lineage === "object" ? queryRun.lineage : {};
  const priceBandPolicy = lineage.price_band_policy && typeof lineage.price_band_policy === "object" ? lineage.price_band_policy : null;
  if (priceBandPolicy) {
    const template = Array.isArray(priceBandPolicy.fixed_template)
      ? priceBandPolicy.fixed_template.map((item) => String(item?.band || "").trim()).filter(Boolean)
      : [];
    const rawMode = String(priceBandPolicy.mode || "").trim().toLowerCase();
    lines.push(
      `# 价格带策略: ${formatPriceBandModeLabel(rawMode)} · ${Number(priceBandPolicy.bucket_count || 0) || "-"}桶 · ${formatPriceBandStrategyLabel(priceBandPolicy.strategy || "equal_width")}`,
    );
    if (priceBandPolicy.boundary && typeof priceBandPolicy.boundary === "object") {
      const boundary = priceBandPolicy.boundary;
      const boundaryLabel = boundary.enabled ? "开启" : "关闭";
      const roundingLabel = boundary.rounding === "hundred" ? "整百" : boundary.rounding === "thousand" ? "整千" : "自动";
      const customBoundaries = formatPriceBandCustomBoundaries(boundary.custom_boundaries || []);
      lines.push(`# 边界处理: ${boundaryLabel}${boundary.enabled ? ` · ${roundingLabel}${customBoundaries ? ` · 中间边界 ${customBoundaries}` : ""}` : ""}`);
    }
    if (rawMode === "fixed" && template.length) {
      lines.push(`# 固定模板: ${template.join(" / ")}`);
    }
  }
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
      const relationExpr = `${row.left_table || ""}.${row.left_field || ""} = ${row.right_table || ""}.${row.right_field || ""}`;
      const noteText = row.note || "";
      const cells = [
        renderTitleCell(row.left_table || "", "hover-tip-cell", relationExpr),
        renderTitleCell(row.left_field || "", "hover-tip-cell", relationExpr),
        renderTitleCell(row.right_table || "", "hover-tip-cell", relationExpr),
        renderTitleCell(row.right_field || "", "hover-tip-cell", relationExpr),
        renderTitleCell(row.join_type || "", "hover-tip-cell", relationExpr),
        renderTitleCell(noteText, "hover-tip-cell", noteText),
      ].join("");
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
      const relationExpr = `${item.left_table || ""}.${item.left_field || ""} = ${item.right_table || ""}.${item.right_field || ""}`;
      const noteText = item.note || item.reason || "";
      return `<tr class="previewable-row" data-preview-title="已选关系详情" data-preview-payload="${encodeRowPayload(item)}">
        ${renderTitleCell(relationExpr, "hover-tip-cell")}
        ${renderTitleCell(item.join_type || "", "hover-tip-cell")}
        ${renderTitleCell(item.cardinality || "", "hover-tip-cell")}
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

function getDraftSelectionKey(kind) {
  return kind === "field" ? "selected_fields" : "selected_relations";
}

function normalizeSelectionItem(kind, item, index) {
  const clone = { ...(item && typeof item === "object" ? item : {}) };
  if (!clone.candidate_id) {
    clone.candidate_id = normalizeCandidateId(kind === "field" ? "fld" : "rel", clone, index);
  }
  clone.selected = true;
  return clone;
}

function normalizeSelectionList(kind, rows) {
  const list = Array.isArray(rows) ? rows : [];
  const normalized = [];
  const seen = new Set();
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i];
    if (!item || typeof item !== "object") continue;
    const clone = normalizeSelectionItem(kind, item, i);
    const key = String(clone.candidate_id || "").trim();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    normalized.push(clone);
  }
  return normalized;
}

function syncDraftSelectionState(draft) {
  if (!draft || typeof draft !== "object") return draft;
  const candidates = draft.candidates && typeof draft.candidates === "object" ? draft.candidates : {};
  const syncKind = (kind, listKey) => {
    const selectedKey = getDraftSelectionKey(kind);
    const snapshot = normalizeSelectionList(kind, draft[selectedKey]);
    const selectedMap = new Map(snapshot.map((item) => [String(item.candidate_id || "").trim(), item]));
    const list = Array.isArray(candidates[listKey]) ? candidates[listKey] : [];
    for (let i = 0; i < list.length; i += 1) {
      const item = list[i];
      if (!item || typeof item !== "object") continue;
      if (!item.candidate_id) item.candidate_id = normalizeCandidateId(kind === "field" ? "fld" : "rel", item, i);
      const candidateId = String(item.candidate_id || "").trim();
      if (!candidateId) continue;
      const currentSelected = selectedMap.get(candidateId);
      if (currentSelected) {
        item.selected = true;
        selectedMap.set(candidateId, { ...currentSelected, ...normalizeSelectionItem(kind, item, i) });
        continue;
      }
      if (item.selected !== false) {
        selectedMap.set(candidateId, normalizeSelectionItem(kind, item, i));
      }
    }
    draft[selectedKey] = [...selectedMap.values()];
    return draft[selectedKey];
  };
  syncKind("field", "fields");
  syncKind("relation", "relations");
  return draft;
}

function mergeDraftSelectionState(targetDraft, sourceDraft) {
  if (!targetDraft || typeof targetDraft !== "object") return targetDraft;
  const mergeByCandidateId = (rowsA, rowsB, kind) => {
    const merged = new Map();
    for (const item of normalizeSelectionList(kind, rowsA)) {
      const key = String(item.candidate_id || "").trim();
      if (!key) continue;
      merged.set(key, item);
    }
    for (const item of normalizeSelectionList(kind, rowsB)) {
      const key = String(item.candidate_id || "").trim();
      if (!key) continue;
      const previous = merged.get(key) || {};
      merged.set(key, { ...previous, ...item });
    }
    return [...merged.values()];
  };
  targetDraft.selected_fields = mergeByCandidateId(sourceDraft?.selected_fields, targetDraft.selected_fields, "field");
  targetDraft.selected_relations = mergeByCandidateId(sourceDraft?.selected_relations, targetDraft.selected_relations, "relation");
  return syncDraftSelectionState(targetDraft);
}

function removeDraftSelectionItem(draft, kind, candidateId) {
  if (!draft || typeof draft !== "object") return false;
  const targetId = String(candidateId || "").trim();
  if (!targetId) return false;
  const selectedKey = getDraftSelectionKey(kind);
  const currentSnapshot = normalizeSelectionList(kind, draft[selectedKey]);
  const nextSnapshot = currentSnapshot.filter((item) => String(item.candidate_id || "").trim() !== targetId);
  const changedSnapshot = nextSnapshot.length !== currentSnapshot.length;
  draft[selectedKey] = nextSnapshot;
  const listKey = kind === "field" ? "fields" : "relations";
  const currentCandidates = Array.isArray(draft?.candidates?.[listKey]) ? draft.candidates[listKey] : [];
  let changedCandidate = false;
  for (const item of currentCandidates) {
    if (!item || typeof item !== "object") continue;
    if (String(item.candidate_id || "").trim() === targetId) {
      item.selected = false;
      changedCandidate = true;
    }
  }
  syncDraftSelectionState(draft);
  return changedSnapshot || changedCandidate;
}

function formatConfidence(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(2);
}

function formatCandidateMeta(item) {
  if (!item || typeof item !== "object") return "";
  const parts = [];
  const fieldType = String(item.column_type || item.field_type || item.data_type || "").trim();
  const tableRole = String(item.table_role_hint || "").trim();
  const fieldRole = String(item.field_role_hint || "").trim();
  const origin = String(item.origin || "").trim();
  if (fieldType) parts.push(fieldType);
  if (item.is_primary === true) parts.push("PK");
  if (item.is_nullable === false) parts.push("NOT NULL");
  if (tableRole) parts.push(tableRole);
  if (fieldRole) parts.push(fieldRole);
  if (origin) parts.push(origin);
  const comment = String(item.column_comment || item.table_comment || "").trim();
  if (comment) parts.push(comment);
  return parts.slice(0, 5).join(" / ");
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
      `${renderFixedTableOpen(["llm-candidate-table", "llm-candidate-field-table", "uniform-list-table"], [42, 88, 96, 64, 110, 64, 72, 118])}<thead><tr><th>导入</th><th>semantic</th><th>table.field</th><th>role</th><th>schema信息</th><th>required</th><th>confidence</th><th>reason</th></tr></thead><tbody>` +
      rows
        .map((item) => {
          const checked = item.selected !== false ? "checked" : "";
          const requiredText = item.required ? "true" : "false";
          return `<tr class="previewable-row" data-preview-title="字段候选详情" data-preview-payload="${encodeRowPayload(item)}">
            <td><input class="llm-candidate-check" type="checkbox" data-kind="field" data-candidate-id="${escapeHtml(item.candidate_id || "")}" ${checked} /></td>
            <td>${escapeHtml(item.semantic_name || "")}</td>
            <td>${escapeHtml(`${item.table_name || ""}.${item.field_name || ""}`)}</td>
            <td>${escapeHtml(item.role || "")}</td>
            <td>${escapeHtml(formatCandidateMeta(item))}</td>
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
    `${renderFixedTableOpen(["llm-candidate-table", "llm-candidate-relation-table", "uniform-list-table"], [42, 116, 64, 72, 84, 64, 72, 118])}<thead><tr><th>导入</th><th>relation</th><th>join</th><th>cardinality</th><th>来源</th><th>required</th><th>confidence</th><th>reason</th></tr></thead><tbody>` +
    rows
      .map((item) => {
        const checked = item.selected !== false ? "checked" : "";
        const requiredText = item.required ? "true" : "false";
        const relationExpr = `${item.left_table || ""}.${item.left_field || ""} = ${item.right_table || ""}.${item.right_field || ""}`;
        const sourceText = formatCandidateMeta(item);
        const reasonText = item.reason || item.note || "";
        return `<tr class="previewable-row" data-preview-title="关系候选详情" data-preview-payload="${encodeRowPayload(item)}">
          <td><input class="llm-candidate-check" type="checkbox" data-kind="relation" data-candidate-id="${escapeHtml(item.candidate_id || "")}" ${checked} /></td>
          ${renderTitleCell(relationExpr, "hover-tip-cell")}
          ${renderTitleCell(item.join_type || "", "hover-tip-cell")}
          ${renderTitleCell(item.cardinality || "", "hover-tip-cell")}
          ${renderTitleCell(sourceText, "hover-tip-cell")}
          <td>${escapeHtml(requiredText)}</td>
          <td>${escapeHtml(formatConfidence(item.confidence))}</td>
          ${renderTitleCell(reasonText, "hover-tip-cell")}
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
  const schemaSummary = draft?.schema_summary && typeof draft.schema_summary === "object" ? draft.schema_summary : {};
  const dictionaryHints = Array.isArray(draft?.dictionary_table_hints) ? draft.dictionary_table_hints : [];
  const schemaText = Number(schemaSummary.table_count || 0) > 0
    ? `；schema 表 ${schemaSummary.table_count || 0}，字段 ${schemaSummary.field_count || 0}，关系候选 ${schemaSummary.relation_candidate_count || 0}，字典/主数据候选 ${schemaSummary.dictionary_table_count || dictionaryHints.length}`
    : "";
  const noteText = Array.isArray(draft?.notes) && draft.notes.length ? `；备注：${draft.notes.slice(0, 2).join("；")}` : "";
  tableCandidates.textContent = tables.length ? `LLM 表候选：${tables.join("、")}${schemaText}` : `LLM 表候选：未返回${schemaText}`;
  summary.textContent = `候选导入状态：字段 ${selectedFields}/${fields.length}，关系 ${selectedRelations}/${relations.length}。应用时仅导入已勾选项${noteText}。`;
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
  if (!selected) {
    changed = removeDraftSelectionItem(draft, kind, candidateId) || changed;
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
    if (!selected) {
      removeDraftSelectionItem(draft, kind, item.candidate_id);
    }
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
    const isPreset = sceneId.startsWith("scene_prd_") || sceneId === "scene_0001";
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
  const sceneId = String(scene?.scene_id || "").trim();
  if (!sceneId) return;
  const ok = window.confirm(`确认删除场景“${formatSceneName(scene.name)}”？`);
  if (!ok) return;

  const snapshot = {
    scenes: state.scenes,
    sessions: state.sessions,
    queryHistory: state.queryHistory,
    currentSceneId: state.currentSceneId,
    currentSceneDetail: state.currentSceneDetail,
    currentScenePlaybook: state.currentScenePlaybook,
    priceBandPolicy: state.priceBandPolicy,
    priceBandMode: state.priceBandMode,
    selectedPresetKey: state.selectedPresetKey,
    selectedPresetQuestion: state.selectedPresetQuestion,
    semanticCacheFields: state.semanticCacheFields,
    editingSemanticCacheId: state.editingSemanticCacheId,
    currentLlmAgentDraft: state.currentLlmAgentDraft,
    currentSession: state.currentSession,
    restoreSessionId: state.restoreSessionId,
    currentDeck: state.currentDeck,
    currentArtifact: state.currentArtifact,
    currentSlide: state.currentSlide,
    currentReportState: state.currentReportState,
  };
  const restoreSnapshot = () => {
    state.scenes = snapshot.scenes;
    state.sessions = snapshot.sessions;
    state.queryHistory = snapshot.queryHistory;
    state.currentSceneId = snapshot.currentSceneId;
    state.currentSceneDetail = snapshot.currentSceneDetail;
    state.currentScenePlaybook = snapshot.currentScenePlaybook;
    state.priceBandPolicy = snapshot.priceBandPolicy;
    state.priceBandMode = snapshot.priceBandMode;
    state.selectedPresetKey = snapshot.selectedPresetKey;
    state.selectedPresetQuestion = snapshot.selectedPresetQuestion;
    state.semanticCacheFields = snapshot.semanticCacheFields;
    state.editingSemanticCacheId = snapshot.editingSemanticCacheId;
    state.currentLlmAgentDraft = snapshot.currentLlmAgentDraft;
    state.currentSession = snapshot.currentSession;
    state.restoreSessionId = snapshot.restoreSessionId;
    state.currentDeck = snapshot.currentDeck;
    state.currentArtifact = snapshot.currentArtifact;
    state.currentSlide = snapshot.currentSlide;
    state.currentReportState = snapshot.currentReportState;
    renderSceneWorkspaceState();
  };

  const wasCurrentScene = state.currentSceneId === sceneId;
  const currentSessionWasDeleted = state.currentSession?.scene_id === sceneId;
  state.scenes = state.scenes.filter((item) => item.scene_id !== sceneId);
  state.sessions = state.sessions.filter((item) => item.scene_id !== sceneId);
  state.queryHistory = state.queryHistory.filter((entry) => getHistorySession(entry)?.scene_id !== sceneId);
  if (wasCurrentScene) {
    state.currentSceneId = state.scenes[0]?.scene_id || "";
    setCurrentSceneDetailPlaceholder();
  } else if (state.currentSceneDetail?.scene_id === sceneId) {
    setCurrentSceneDetailPlaceholder();
  }
  if (wasCurrentScene || currentSessionWasDeleted) {
    state.currentSession = null;
    state.restoreSessionId = "";
    clearQueryResultViews();
    clearReportStateViews();
  }
  renderSceneWorkspaceState();

  try {
    await api(`/api/v1/scenes/${encodeURIComponent(sceneId)}`, { method: "DELETE" });
  } catch (error) {
    restoreSnapshot();
    throw error;
  }

  if (state.llmDraftSaveTimers[sceneId]) {
    clearTimeout(state.llmDraftSaveTimers[sceneId]);
    delete state.llmDraftSaveTimers[sceneId];
  }
  delete state.llmDraftBySceneId[sceneId];
  refreshScenes({ loadDetail: false, loadHistory: false })
    .then(async () => {
      await refreshSessions();
      if (wasCurrentScene) {
        await refreshSceneDetail();
        await refreshQueryHistory();
      }
    })
    .catch((error) => console.warn("reconcile scenes after delete failed", error));
}

function renderSceneConfig() {
  const scene = state.currentSceneDetail;
  const summaryWrap = el("sceneSummaryWrap");
  const configView = el("sceneConfigView");
  const fieldsWrap = el("sceneFieldsWrap");
  const relationsWrap = el("sceneRelationsWrap");
  const sceneConfigListHint = el("sceneConfigListHint");
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
    if (sceneConfigListHint) sceneConfigListHint.textContent = "来源：当前场景已选清单。";
    sceneConfigFieldsWrap.innerHTML = "";
    sceneConfigRelationsWrap.innerHTML = "";
    fieldsWrap.innerHTML = "";
    relationsWrap.innerHTML = "";
    if (semanticCacheKeyword) semanticCacheKeyword.value = state.semanticCacheKeyword;
    if (semanticCacheFormHint) semanticCacheFormHint.textContent = "请选择场景后新增字段。";
    syncSceneAdvancedFieldState();
    syncSceneSchemaInputLists();
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
  if (state.currentLlmAgentDraft) {
    ensureDraftCandidateMeta(state.currentLlmAgentDraft);
    syncDraftSelectionState(state.currentLlmAgentDraft);
  }
  renderLlmCandidateSelector();
  renderLlmCacheStatus();

  const sceneRelations = Array.isArray(scene.relations) ? scene.relations : [];
  const recommendation = state.currentLlmAgentDraft ? ensureDraftCandidateMeta(state.currentLlmAgentDraft) : null;
  const selectedDraftFields = normalizeSelectionList("field", recommendation?.selected_fields);
  const selectedDraftRelations = normalizeSelectionList("relation", recommendation?.selected_relations);
  if (recommendation) {
    sceneConfigListSummary.textContent =
      `当前已选字段 ${selectedDraftFields.length}，已选关系 ${selectedDraftRelations.length}。切换推荐目标会保留已选清单，点击“剔除”可从清单中移除。`;
    if (sceneConfigListHint) {
      sceneConfigListHint.textContent = "来源：当前推荐与历史已选清单，切换推荐目标不会清空。";
    }
  } else {
    sceneConfigListSummary.textContent =
      dedupedSemantic.duplicateCount > 0
        ? `尚未生成推荐结果。当前场景字段 ${sceneFields.length}（已隐藏重复 ${dedupedSemantic.duplicateCount}），启用字段 ${queryableDedupedFields.length}，关系 ${sceneRelations.length}。`
        : `尚未生成推荐结果。当前场景字段 ${sceneFields.length}，启用字段 ${queryableDedupedFields.length}，关系 ${sceneRelations.length}。`;
    if (sceneConfigListHint) {
      sceneConfigListHint.textContent = "来源：当前场景已选清单。";
    }
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
  syncSceneSchemaInputLists();
}

function requireCurrentRecommendation() {
  const recommendation = ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  if (!recommendation || typeof recommendation !== "object") {
    throw new Error("推荐结果为空：请先点击“推荐”生成候选列表");
  }
  syncDraftSelectionState(recommendation);
  return recommendation;
}

function removeSelectedDraftCandidate(kind, candidateId) {
  if (!candidateId) return;
  const draft = requireCurrentRecommendation();
  if (!removeDraftSelectionItem(draft, kind, candidateId)) return;
  state.currentLlmAgentDraft = draft;
  setSceneDraft(state.currentSceneId, draft);
  renderSceneConfig();
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

function getCurrentSessionId() {
  return String(state.currentSession?.session_id || "").trim();
}

function isCurrentSessionId(sessionId) {
  const id = String(sessionId || "").trim();
  return Boolean(id && getCurrentSessionId() === id);
}

function applySessionLocally(session) {
  if (!session?.session_id) return;
  state.currentSession = session;
  state.restoreSessionId = session.session_id;
  if (session.scene_id) state.currentSceneId = session.scene_id;
  state.sessions = [session, ...state.sessions.filter((item) => item.session_id !== session.session_id)];
  persistUiState();
}

function upsertQueryHistoryEntry(entry) {
  const session = getHistorySession(entry);
  if (!session?.session_id) return;
  state.queryHistory = [
    entry,
    ...state.queryHistory.filter((item) => getHistorySession(item)?.session_id !== session.session_id),
  ];
  renderSessions();
  renderQueryHistory();
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
  try {
    state.clothing.detail = await api(`/api/v1/clothing/items/${id}`);
  } catch (error) {
    state.clothing.detail = {
      message: "商品详情加载失败",
      detail: formatErrorDetail(error?.detail || error?.message || error),
    };
  }
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
  const requestedSessionId = state.currentSession.session_id;
  const payload = await api(`/api/v1/analysis/sessions/${requestedSessionId}/report-state`);
  if (!isCurrentSessionId(requestedSessionId)) return null;
  if (payload?.session?.session_id && payload.session.session_id !== requestedSessionId) return null;
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
  renderPriceBandModeControl();
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

async function refreshScenes({ loadDetail = true, loadHistory = true } = {}) {
  const previousSceneId = state.currentSceneId;
  try {
    const payload = await api("/api/v1/scenes");
    state.scenes = normalizeSceneList(payload);
    state.sceneLoadError = "";
  } catch (error) {
    state.sceneLoadError = error?.message || String(error);
    console.error(error);
  }
  ensureCurrentSceneFromList();
  if (previousSceneId !== state.currentSceneId) {
    resetFieldResolutionState();
  }
  renderScenes();
  const sceneChanged = previousSceneId !== state.currentSceneId;
  const hasStaleDetail =
    Boolean(state.currentSceneDetail?.scene_id) && state.currentSceneDetail.scene_id !== state.currentSceneId;
  if (loadDetail) {
    await refreshSceneDetail();
  } else if (sceneChanged || hasStaleDetail || !state.currentSceneId) {
    setCurrentSceneDetailPlaceholder();
    renderSceneConfig();
  }
  renderIntentTemplates();
  if (loadHistory) {
    await refreshQueryHistory().catch((error) => console.warn("refresh query history failed", error));
  }
  persistUiState();
  if (loadDetail) maybeAutoRecommendOnce();
}

async function refreshSessions({ preferredSessionId = "" } = {}) {
  const previousSceneId = state.currentSceneId;
  state.sessions = filterSessionsForKnownScenes(await api("/api/v1/analysis/sessions"));
  const desiredSessionId = String(preferredSessionId || state.currentSession?.session_id || state.restoreSessionId || "").trim();
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
    if (previousSceneId !== state.currentSceneId) {
      resetFieldResolutionState();
    }
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

async function refreshQueryHistory({ sceneId = "" } = {}) {
  const requestedSceneId = String(sceneId || state.currentSceneId || "").trim();
  const query = requestedSceneId ? `?scene_id=${encodeURIComponent(requestedSceneId)}` : "";
  const payload = await api(`/api/v1/sql-result-agent/history${query}`);
  if (requestedSceneId !== String(state.currentSceneId || "").trim()) return payload;
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
  if (!isCurrentSessionId(id)) return;
  fillIntentInputs(session.global_goal || "");

  if (!entry?.query_run && !entry?.query_plan) {
    try {
      const latest = await api(`/api/v1/sql-result-agent/sessions/${id}/latest`);
      if (!isCurrentSessionId(id)) return;
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
  const requestedSessionId = state.currentSession.session_id;
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
    latest = await api(`/api/v1/sql-result-agent/sessions/${requestedSessionId}/latest`);
  } catch (error) {
    console.warn("load latest sql result failed", error);
    return;
  }
  if (!isCurrentSessionId(requestedSessionId)) return;
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
  setCreateSceneHint("正在创建场景...");
  if (!name) throw new Error("创建场景失败：请填写场景名称");
  const scene = await api("/api/v1/scenes", {
    method: "POST",
    body: JSON.stringify({
      name,
      description: el("sceneDesc").value.trim(),
    }),
  });
  state.currentSceneId = scene.scene_id;
  state.selectedPresetKey = "";
  state.selectedPresetQuestion = "";
  clearCurrentSessionState();
  clearQueryResultViews();
  state.scenes = [scene, ...state.scenes.filter((item) => item.scene_id !== scene.scene_id)];
  state.currentSceneDetail = scene;
  state.currentScenePlaybook = null;
  state.priceBandPolicy = {
    mode: "adaptive",
    bucket_count: 10,
    strategy: "equal_width",
    boundary: {
      enabled: true,
      rounding: "auto",
      open_ended: true,
      custom_boundaries: [],
    },
  };
  state.priceBandMode = state.priceBandPolicy.mode;
  state.semanticCacheFields = [];
  state.editingSemanticCacheId = "";
  state.currentLlmAgentDraft = null;
  renderScenes();
  renderSceneConfig();
  renderIntentTemplates();
  setCreateSceneHint(`已创建场景：${formatSceneName(scene.name)}。下一步请点击“推荐字段/关系”或手动配置字段。`);
  persistUiState();
  await refreshScenes({ loadDetail: false, loadHistory: false });
  state.currentSceneId = scene.scene_id;
  renderScenes();
  await refreshSceneDetail();
  await refreshQueryHistory({ sceneId: scene.scene_id });
}

async function refreshSceneDetail() {
  if (!state.currentSceneId) {
    clearCurrentSceneDetailState();
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
  if (state.currentLlmAgentDraft) syncDraftSelectionState(state.currentLlmAgentDraft);
  await refreshCurrentSceneSchemaSnapshot();
  renderSceneConfig();
  renderPriceBandModeControl();
}

async function refreshCurrentSceneSchemaSnapshot() {
  const sceneId = String(state.currentSceneId || "").trim();
  if (!sceneId) {
    state.currentSceneSchemaSnapshot = null;
    state.currentSceneSchemaIndex = {
      tables: [],
      tableMap: new Map(),
      fieldsByTable: new Map(),
    };
    state.currentSceneSchemaLoadError = "";
    syncSceneSchemaInputLists();
    return null;
  }
  try {
    const snapshot = await api(`/api/v1/scene-builder/scenes/${encodeURIComponent(sceneId)}/source-schema?force_refresh=true`);
    if (sceneId !== String(state.currentSceneId || "").trim()) return null;
    state.currentSceneSchemaSnapshot = snapshot && typeof snapshot === "object" ? snapshot : null;
    state.currentSceneSchemaIndex = buildSceneSchemaIndex(state.currentSceneSchemaSnapshot);
    state.currentSceneSchemaLoadError = "";
  } catch (error) {
    if (sceneId !== String(state.currentSceneId || "").trim()) return null;
    console.warn("load scene schema snapshot failed", error);
    state.currentSceneSchemaSnapshot = null;
    state.currentSceneSchemaIndex = {
      tables: [],
      tableMap: new Map(),
      fieldsByTable: new Map(),
    };
    state.currentSceneSchemaLoadError = error?.detail || error?.message || String(error);
  }
  syncSceneSchemaInputLists();
  return state.currentSceneSchemaSnapshot;
}

function formatConfigTransferCounts(counts = {}) {
  return `字段 ${Number(counts.fields || 0)}，关系 ${Number(counts.relations || 0)}，语义字段 ${Number(counts.semantic_fields || 0)}`;
}

async function exportCurrentSceneConfig() {
  const sceneId = String(state.currentSceneId || "").trim();
  if (!sceneId) throw new Error("请先选择场景");
  const bundle = await api(`/api/v1/config-transfer/scenes/${encodeURIComponent(sceneId)}/export`);
  const sceneName = String(bundle?.scene?.name || sceneId).trim();
  const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${sceneName.replace(/[^\w\u4e00-\u9fff-]+/g, "_") || sceneId}-config.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  setCreateSceneHint(`已导出配置：${sceneName}。文件包含当前场景字段、ER关系和语义字段。`);
}

async function importSceneConfigFile(file) {
  if (!file) return;
  let bundle;
  try {
    bundle = JSON.parse(await file.text());
  } catch (_error) {
    throw new Error("导入失败：配置文件不是有效 JSON");
  }
  const mode = el("sceneConfigImportMode")?.value || "create";
  const targetSceneId = mode === "create" ? null : String(state.currentSceneId || "").trim();
  if (mode !== "create" && !targetSceneId) {
    throw new Error("合并或覆盖前请先选择目标场景");
  }
  const preview = await api("/api/v1/config-transfer/preview", {
    method: "POST",
    body: JSON.stringify({
      bundle,
      target_scene_id: targetSceneId,
      mode,
    }),
  });
  const sourceName = preview?.source_scene?.name || "未命名场景";
  const targetName = preview?.target_scene?.name || "新场景";
  const countText = formatConfigTransferCounts(preview?.counts);
  const warning = Array.isArray(preview?.warnings) ? preview.warnings.join("\n") : "";
  const confirmed = window.confirm(
    `确认导入配置？\n来源：${sourceName}\n目标：${targetName}\n${countText}\n\n${warning}`,
  );
  if (!confirmed) return;
  const result = await api("/api/v1/config-transfer/import", {
    method: "POST",
    body: JSON.stringify({
      bundle,
      target_scene_id: targetSceneId,
      mode,
    }),
  });
  const importedSceneId = String(result?.scene_id || targetSceneId || "").trim();
  if (importedSceneId) state.currentSceneId = importedSceneId;
  await refreshScenes({ loadDetail: true, loadHistory: false });
  if (importedSceneId) {
    state.currentSceneId = importedSceneId;
    await refreshSceneDetail();
  }
  setCreateSceneHint(`配置导入完成：${result?.scene_name || sourceName}。${formatConfigTransferCounts(result?.counts)}`);
}

async function recommendSceneByLlm() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const goal = (el("llmGoal")?.value || "").trim();
  const previousDraft = getSceneDraft(state.currentSceneId);
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
    business_context: result?.meta?.business_context && typeof result.meta.business_context === "object" ? result.meta.business_context : {},
    schema_summary: result?.meta?.schema_summary && typeof result.meta.schema_summary === "object" ? result.meta.schema_summary : {},
    dictionary_table_hints: Array.isArray(result?.meta?.dictionary_table_hints) ? result.meta.dictionary_table_hints : [],
    candidates: {
      tables: Array.isArray(result?.meta?.table_candidates) ? result.meta.table_candidates : [],
      fields: Array.isArray(result.field_candidates) ? result.field_candidates : [],
      relations: Array.isArray(result.relation_candidates) ? result.relation_candidates : [],
      metric_templates: [],
      regression_questions: [],
    },
  };
  ensureDraftCandidateMeta(state.currentLlmAgentDraft);
  mergeDraftSelectionState(state.currentLlmAgentDraft, previousDraft);
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
  mergeDraftSelectionState(state.currentLlmAgentDraft, recommendation);
  setSceneDraft(state.currentSceneId, state.currentLlmAgentDraft);
  renderSceneConfig();
}

async function applySceneDraftFromLlm() {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const mergeMode = "append";
  const recommendation = requireCurrentRecommendation();
  const fields = normalizeSelectionList("field", recommendation?.selected_fields);
  const relations = normalizeSelectionList("relation", recommendation?.selected_relations);
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
  mergeDraftSelectionState(state.currentLlmAgentDraft, recommendation);
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
  mergeDraftSelectionState(state.currentLlmAgentDraft, recommendation);
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
  const canValidateLocally = Array.isArray(state.currentSceneSchemaIndex?.tables) && state.currentSceneSchemaIndex.tables.length > 0;
  if (canValidateLocally) {
    const table = resolveSceneSchemaTable(payload.table_name);
    if (!table) {
      throw new Error(`数据库中不存在表：${payload.table_name}`);
    }
    const fieldName = resolveSceneSchemaField(payload.table_name, payload.field_name);
    if (!fieldName) {
      throw new Error(`数据库中不存在字段：${table.table_name}.${payload.field_name}`);
    }
    payload.table_name = table.table_name;
    payload.field_name = fieldName;
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
  const canValidateLocally = Array.isArray(state.currentSceneSchemaIndex?.tables) && state.currentSceneSchemaIndex.tables.length > 0;
  if (canValidateLocally) {
    const leftTable = resolveSceneSchemaTable(payload.left_table);
    if (!leftTable) {
      throw new Error(`数据库中不存在表：${payload.left_table}`);
    }
    const leftField = resolveSceneSchemaField(payload.left_table, payload.left_field);
    if (!leftField) {
      throw new Error(`数据库中不存在字段：${leftTable.table_name}.${payload.left_field}`);
    }
    const rightTable = resolveSceneSchemaTable(payload.right_table);
    if (!rightTable) {
      throw new Error(`数据库中不存在表：${payload.right_table}`);
    }
    const rightField = resolveSceneSchemaField(payload.right_table, payload.right_field);
    if (!rightField) {
      throw new Error(`数据库中不存在字段：${rightTable.table_name}.${payload.right_field}`);
    }
    payload.left_table = leftTable.table_name;
    payload.left_field = leftField;
    payload.right_table = rightTable.table_name;
    payload.right_field = rightField;
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
  await refreshSessions({ preferredSessionId: session.session_id });
  await refreshQueryHistory({ sceneId: session.scene_id });
  fillIntentInputs(goal || state.currentSession?.global_goal || "");
  syncArtifactDownload();
}

async function generateSqlFromIntent() {
  const intent = (el("queryIntentInput")?.value || "").trim();
  if (!intent) throw new Error("请先输入业务问题");
  fillIntentInputs(intent);
  const session = await createSessionForCurrentScene({ intent });
  if (!session) throw new Error("请先选择场景");
  await refreshSessions({ preferredSessionId: session.session_id });
  await refreshQueryHistory({ sceneId: session.scene_id });
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
  state.restoreSessionId = state.currentSession.session_id;
  await refreshSessions({ preferredSessionId: state.currentSession.session_id });
  await loadPlan();
}

async function executeQuery() {
  ensureSession();
  await syncIntentPlanIfNeeded();
  const sessionId = state.currentSession.session_id;
  const sceneId = state.currentSession.scene_id;
  const queryRun = await api(`/api/v1/analysis/sessions/${state.currentSession.session_id}/current-query/execute`, { method: "POST" });
  if (!isCurrentSessionId(sessionId)) return;
  el("queryRunView").textContent = formatQueryRunView(queryRun);
  renderQueryTable(queryRun.result_preview || []);
  if (el("querySaveHint")) el("querySaveHint").textContent = `已保存到提问历史：${state.currentSession.session_id}`;
  state.restoreSessionId = sessionId;
  const localSession = {
    ...state.currentSession,
    status: queryRun.status === "succeeded" ? "summarizing_result" : "failed",
    updated_at: new Date().toISOString(),
  };
  applySessionLocally(localSession);
  upsertQueryHistoryEntry({
    session: localSession,
    query_plan: null,
    query_run: queryRun,
    saved: true,
  });
  await refreshSessions({ preferredSessionId: sessionId });
  await refreshQueryHistory({ sceneId });
}

async function runSqlResultAgentFromConfig({ skipFieldResolution = false } = {}) {
  if (!state.currentSceneId) throw new Error("未选择场景");
  const intent =
    (el("queryIntentInput")?.value || "").trim() ||
    (el("goalInput")?.value || "").trim() ||
    (el("llmGoal")?.value || "").trim();
  if (!intent) throw new Error("请先输入业务问题或分析目标");
  fillIntentInputs(intent);
  let fieldResolutionPayload = { field_resolution: { intent, confirmed_resolutions: [], ignored_terms: [] }, has_unresolved_required_terms: false };
  if (!skipFieldResolution) {
    const existingAnalysis =
      state.fieldResolution.analysis && normalizeIntent(state.fieldResolution.intent) === normalizeIntent(intent);
    if (existingAnalysis) {
      syncFieldResolutionSelectionsFromDom();
    } else {
      const analysis = await analyzeFieldResolutionForIntent(intent);
      if (!analysis) throw new Error("字段解析失败");
    }
    fieldResolutionPayload = buildFieldResolutionPayload();
    renderFieldResolutionPanel();
    if (fieldResolutionPayload.has_unresolved_required_terms) {
      if (el("querySaveHint")) {
        el("querySaveHint").textContent = "已识别到需要确认的字段，请先在下方完成字段筛选，再继续生成SQL。";
      }
      return;
    }
    const hasFieldResolutionTerms = Array.isArray(state.fieldResolution.analysis?.terms)
      && state.fieldResolution.analysis.terms.length > 0;
    if (hasFieldResolutionTerms) {
      if (el("querySaveHint")) {
        el("querySaveHint").textContent = "字段已自动识别并默认选中，请人工点击“确认并生成SQL”执行。";
      }
      return;
    }
  } else {
    syncFieldResolutionSelectionsFromDom();
    fieldResolutionPayload = buildFieldResolutionPayload();
    if (fieldResolutionPayload.has_unresolved_required_terms) {
      if (el("querySaveHint")) {
        el("querySaveHint").textContent = "还有字段歧义未确认，请先选择字段或选择不作为过滤条件。";
      }
      renderFieldResolutionPanel();
      return;
    }
  }
  state.fieldResolutionExecutionIntent = normalizeIntent(intent);
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
  const priceBandControlsEnabled = shouldShowPriceBandControls(intent);
  const selectedPresetIsStalePriceBand =
    state.selectedPresetKey && isPriceBandIntent(state.selectedPresetQuestion) && !isPriceBandIntent(intent);
  const activePresetKey = selectedPresetIsStalePriceBand ? "" : state.selectedPresetKey;
  const activePresetQuestion = selectedPresetIsStalePriceBand ? "" : state.selectedPresetQuestion;
  const priceBandPolicy = priceBandControlsEnabled ? getSelectedPriceBandPolicy() : null;
  const requestContext = {
    source: "query_tab",
    scene_id: state.currentSceneId,
    selected_preset_key: activePresetKey || undefined,
    selected_preset_question: activePresetQuestion || undefined,
    intent_edited_from_preset: Boolean(
      activePresetKey && normalizeIntent(intent) !== normalizeIntent(activePresetQuestion),
    ),
    selected_field_count: selectedFieldCount,
    selected_relation_count: selectedRelationCount,
    field_resolution: fieldResolutionPayload.field_resolution,
  };
  if (priceBandPolicy) {
    requestContext.price_band_mode = priceBandPolicy.mode;
    requestContext.price_band_policy = { ...priceBandPolicy };
  }
  let result;
  try {
    result = await api(`/api/v1/sql-result-agent/sessions/${session.session_id}/generate-and-run`, {
      method: "POST",
      body: JSON.stringify({
        intent,
        agent_prompt: (el("llmGoal")?.value || "").trim(),
        execute: true,
        context: requestContext,
      }),
    });
  } catch (error) {
    const detail = formatErrorDetail(error?.detail || error?.message || error);
    if (el("querySaveHint")) el("querySaveHint").textContent = `SQL生成失败：${detail}`;
    throw error;
  }
  const resultSceneId = result?.scene_id || session.scene_id;
  const localSession = {
    ...session,
    scene_id: resultSceneId,
    global_goal: intent,
    status: result?.query_run ? (result.query_run.status === "succeeded" ? "summarizing_result" : "failed") : session.status,
    updated_at: new Date().toISOString(),
  };
  applySessionLocally(localSession);
  upsertQueryHistoryEntry({
    session: localSession,
    query_plan: result?.query_plan || null,
    query_run: result?.query_run || null,
    saved: Boolean(result?.query_run),
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
  await refreshSessions({ preferredSessionId: session.session_id });
  await refreshQueryHistory({ sceneId: resultSceneId });
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
  bindHoverTips();
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
  el("createSceneBtn").onclick = () => run(() => withButtonBusy("createSceneBtn", "创建中...", createScene));
  el("toggleCreateSceneBtn").onclick = () => {
    state.createSceneCollapsed = !state.createSceneCollapsed;
    renderCreateSceneCollapse();
  };
  if (el("createSessionBtn")) el("createSessionBtn").onclick = () => run(createSession);
  el("runQueryBtn").onclick = () =>
    run(() => withAgentWait("sqlResult", "SQL 结果 Agent", confirmAndGenerateSqlFromFieldResolution));
  if (el("clearFieldResolutionBtn")) {
    el("clearFieldResolutionBtn").onclick = () => resetFieldResolutionState();
  }
  if (el("intentCorrectionList")) {
    el("intentCorrectionList").addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const button = target.closest("[data-apply-intent-correction]");
      if (!(button instanceof HTMLElement)) return;
      applyIntentCorrection(button.dataset.applyIntentCorrection);
    });
  }
  if (el("fieldResolutionList")) {
    el("fieldResolutionList").addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLSelectElement)) return;
      syncFieldResolutionSelectionsFromDom();
    });
    el("fieldResolutionList").addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const trigger = target.closest("[data-field-resolution-trigger]");
      if (trigger instanceof HTMLElement) {
        const picker = trigger.closest(".field-resolution-picker");
        if (!(picker instanceof HTMLElement)) return;
        const menu = picker.querySelector(".field-resolution-menu");
        const willOpen = !picker.classList.contains("is-open");
        closeFieldResolutionMenus(picker);
        picker.classList.toggle("is-open", willOpen);
        trigger.setAttribute("aria-expanded", willOpen ? "true" : "false");
        if (menu) menu.hidden = !willOpen;
        return;
      }
      const option = target.closest("[data-field-resolution-value]");
      if (option instanceof HTMLElement) {
        const picker = option.closest(".field-resolution-picker");
        const termId = String(picker?.getAttribute("data-term-id") || "").trim();
        if (!termId) return;
        state.fieldResolution.selections[termId] = normalizeFieldResolutionCandidateIndex(option.dataset.fieldResolutionValue);
        closeFieldResolutionMenus();
        persistUiState();
        renderFieldResolutionPanel();
      }
    });
  }
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (target instanceof HTMLElement && target.closest(".field-resolution-picker")) return;
    closeFieldResolutionMenus();
  });
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
    scheduleAutoFieldResolutionAnalysis(intent, { immediate: true });
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
  if (el("queryIntentInput")) {
    el("queryIntentInput").addEventListener("input", () => {
      scheduleAutoFieldResolutionAnalysis(el("queryIntentInput").value);
      renderPriceBandModeControl();
      persistUiState();
    });
    el("queryIntentInput").addEventListener("scroll", () => {
      const overlay = el("queryIntentHighlight");
      const input = el("queryIntentInput");
      if (overlay && input) {
        overlay.scrollTop = input.scrollTop;
        overlay.scrollLeft = input.scrollLeft;
      }
    });
  }
  document.querySelectorAll("#priceBandModeToggle .price-band-mode-btn").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      setPriceBandMode(button.dataset.priceBandMode || "adaptive");
    });
  });
  if (el("priceBandBucketCountInput")) {
    el("priceBandBucketCountInput").addEventListener("change", () => {
      setPriceBandPolicy({ bucket_count: el("priceBandBucketCountInput").value });
    });
  }
  document.querySelectorAll("#priceBandStrategyToggle .price-band-strategy-btn").forEach((button) => {
    button.addEventListener("click", () => {
      setPriceBandPolicy({ strategy: button.dataset.priceBandStrategy || "quantile" });
    });
  });
  document.querySelectorAll("#priceBandBoundaryToggle .price-band-boundary-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const enabled = (button.dataset.priceBandBoundary || "raw") === "rounded";
      setPriceBandPolicy({
        strategy: "equal_width",
        boundary: {
          ...(state.priceBandPolicy.boundary || {}),
          enabled,
        },
      });
    });
  });
  document.querySelectorAll("#priceBandRoundToggle .price-band-round-btn").forEach((button) => {
    button.addEventListener("click", () => {
      setPriceBandPolicy({
        boundary: {
          ...(state.priceBandPolicy.boundary || {}),
          enabled: true,
          rounding: button.dataset.priceBandRounding || "auto",
        },
      });
    });
  });
  if (el("priceBandBoundariesInput")) {
    el("priceBandBoundariesInput").addEventListener("change", () => {
      setPriceBandPolicy({
        boundary: {
          ...(state.priceBandPolicy.boundary || {}),
          enabled: true,
          custom_boundaries: parsePriceBandCustomBoundaries(el("priceBandBoundariesInput").value),
        },
      });
    });
  }
  if (el("guideBtn")) el("guideBtn").onclick = () => el("guideDialog").showModal();
  if (el("closeGuideBtn")) el("closeGuideBtn").onclick = () => el("guideDialog").close();
  if (el("openInputCorrectionLexiconBtn")) {
    el("openInputCorrectionLexiconBtn").onclick = () => run(openInputCorrectionLexicon);
  }
  if (el("closeInputCorrectionLexiconBtn")) {
    el("closeInputCorrectionLexiconBtn").onclick = () => el("inputCorrectionLexiconDialog")?.close();
  }
  if (el("addInputCorrectionBtn")) {
    el("addInputCorrectionBtn").onclick = () =>
      run(() => withButtonBusy("addInputCorrectionBtn", "追加中...", addInputCorrectionWord));
  }
  if (el("inputCorrectionWord")) {
    el("inputCorrectionWord").addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      run(() => withButtonBusy("addInputCorrectionBtn", "追加中...", addInputCorrectionWord));
    });
  }
  if (el("inputCorrectionLexiconList")) {
    el("inputCorrectionLexiconList").addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const toggle = target.closest("[data-input-correction-toggle]");
      if (toggle instanceof HTMLElement) {
        run(() =>
          updateInputCorrectionEnabled(
            toggle.dataset.inputCorrectionToggle,
            toggle.dataset.inputCorrectionEnabled === "true",
          ),
        );
        return;
      }
      const remove = target.closest("[data-input-correction-delete]");
      if (remove instanceof HTMLElement) {
        run(() => deleteInputCorrectionWord(remove.dataset.inputCorrectionDelete));
      }
    });
  }
  if (el("fieldRoleHelpBtn")) el("fieldRoleHelpBtn").onclick = () => el("fieldRoleHelpDialog").showModal();
  if (el("closeFieldRoleHelpBtn")) el("closeFieldRoleHelpBtn").onclick = () => el("fieldRoleHelpDialog").close();
  if (el("configFlowHelpBtn")) el("configFlowHelpBtn").onclick = () => el("configFlowHelpDialog").showModal();
  if (el("closeConfigFlowHelpBtn")) el("closeConfigFlowHelpBtn").onclick = () => el("configFlowHelpDialog").close();
  el("refreshConfigBtn").onclick = () => run(refreshSceneDetail);
  el("refreshDbCacheBtn").onclick = () => run(refreshDbCacheFromMysql);
  el("addFieldBtn").onclick = () => run(addSceneField);
  el("exportSceneConfigBtn").onclick = () => run(exportCurrentSceneConfig);
  el("importSceneConfigBtn").onclick = () => el("sceneConfigFileInput").click();
  el("sceneConfigFileInput").addEventListener("change", (event) => {
    const input = event.target;
    const file = input instanceof HTMLInputElement ? input.files?.[0] : null;
    if (!file) return;
    run(() => importSceneConfigFile(file)).finally(() => {
      input.value = "";
    });
  });
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
      run(() => withAgentWait("sqlResult", "SQL 结果 Agent", confirmAndGenerateSqlFromFieldResolution));
  }
  ["fieldTableName", "relationLeftTable", "relationRightTable"].forEach((id) => {
    const input = el(id);
    if (!input) return;
    ["input", "change", "focus"].forEach((eventName) => {
      input.addEventListener(eventName, () => syncSceneSchemaInputLists());
    });
  });
  ["fieldName", "relationLeftField", "relationRightField"].forEach((id) => {
    const input = el(id);
    if (!input) return;
    input.addEventListener("focus", () => syncSceneSchemaInputLists());
  });
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
  await restoreLatestQueryResultFocus();
  await refreshScenes({ loadHistory: false });
  await refreshSessions();
  await refreshQueryHistory();
  const restoredIntent = currentFieldResolutionIntent();
  if (restoredIntent) {
    scheduleAutoFieldResolutionAnalysis(restoredIntent, { immediate: true });
  }
  if (state.currentSession?.session_id) {
    await loadReportStateForCurrentSession({ silent: true });
  }
  refreshClothingAll().catch(console.error);
  refreshLlmCacheStatus().catch(console.error);
}

loadStoredUiState();
bind();
bootstrap().catch(console.error);
