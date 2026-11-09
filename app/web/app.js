'use strict';
/* 前端异常留痕：启动白屏时靠它定位（后端 MM_DIAG 会把结果写进 diag.log） */
window.__mmErr = [];
window.addEventListener('error', (e) => {
  try { window.__mmErr.push(String((e && e.message) || e)); } catch (_) {}
});
/* ============================================================
   媒体库 · app.js
   纯原生 JS，通过 window.pywebview.api 与后端交互（全部返回 Promise）
   ============================================================ */

/* ================= 1. 全局状态 ================= */
const state = {
  boot: null,          // getBootstrap() 结果
  sidebar: null,       // getSidebar() 结果
  filters: { folderId: null, folderPath: null, authorId: null, tagIds: [], type: null, special: null, search: '', searchField: 'name' },
  activeKey: 'lib:all',// 当前侧边栏激活项标识（防重复查询）
  view: localStorage.getItem('mm.view') || 'grid', // 网格 / 列表视图
  groupBy: 'folder',   // 分组方式：folder / date / author / type / none
  dateGran: 'month',   // 日期分组粒度：year / month / day
  wsCounts: {},        // 工作区 id -> 媒体数（切换器角标）
  groups: [],          // 当前媒体分组
  flat: [],            // 展开后的媒体项（选择/Shift 范围按此顺序）
  sel: new Set(),      // 已选媒体 id
  anchor: -1,          // Shift 范围选择锚点（flat 下标）
  total: 0,
  querySeq: 0,         // 查询序号，防止旧响应覆盖新结果
  scanning: false,
  /* ---- 分页（5 万级库：DOM 只保留已加载的部分，滚动续取） ---- */
  loaded: 0,           // 已加载条目数
  hasMore: false,      // 后端是否还有下一页
  loadingMore: false,
  lastQuery: null,     // 当前筛选条件（续页时复用，避免条件漂移）
};

/* ================= 2. 工具函数 ================= */
const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    if (k === 'class') el.className = attrs[k];
    else if (k === 'text') el.textContent = attrs[k];
    else if (k === 'html') el.innerHTML = attrs[k];
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), attrs[k]);
    else el.setAttribute(k, attrs[k]);
  }
  for (const c of children) if (c != null) el.append(c);
  return el;
}

const SVG = {
  folder: '<svg viewBox="0 0 24 24"><path d="M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  person: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 5-5.5 8-5.5s6.5 1.5 8 5.5"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M4 12.5 9.5 18 20 6.5"/></svg>',
  chevR: '<svg viewBox="0 0 24 24"><path d="M9.5 6.5 15 12l-5.5 5.5"/></svg>',
  tray: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="14" rx="3"/><circle cx="9" cy="9.5" r="1.8"/><path d="M21 14.5 16 9.5 7 18"/><path d="M8 21h8"/></svg>',
  ok: '<svg viewBox="0 0 24 24"><path d="M4 12.5 9.5 18 20 6.5"/></svg>',
  err: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/></svg>',
};

function hashStr(s) { let x = 0; for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) >>> 0; return x; }
function gradClass(name) { return 'p' + (hashStr(String(name || '')) % 6 + 1); } // p1~p6 渐变占位
function fmtSize(b) {
  if (b == null || isNaN(b)) return '';
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1024 * 1024 * 1024) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}
function fmtDate(m) {
  if (m == null || m === '') return '';
  let d = (typeof m === 'number') ? new Date(m < 1e12 ? m * 1000 : m) : new Date(m);
  if (isNaN(d)) return String(m);
  const p = (n) => String(n).padStart(2, '0');
  return (d.getFullYear() === new Date().getFullYear())
    ? p(d.getMonth() + 1) + '-' + p(d.getDate())
    : d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}
function fmtDur(sec) {
  if (sec == null || isNaN(sec) || sec < 0) return '';
  sec = Math.round(sec);
  const m = Math.floor(sec / 60), s = sec % 60, hr = Math.floor(m / 60);
  const p = (n) => String(n).padStart(2, '0');
  return hr > 0 ? hr + ':' + p(m % 60) + ':' + p(s) : p(m) + ':' + p(s);
}

/* ================= 3. Toast 反馈 ================= */
function toast(msg, kind = 'ok') {
  const t = h('div', { class: 'toast ' + kind, html: (kind === 'err' ? SVG.err : SVG.ok) });
  t.append(h('span', { text: msg }));
  $('toasts').append(t);
  requestAnimationFrame(() => t.classList.add('in'));
  setTimeout(() => {
    t.classList.remove('in'); t.classList.add('out');
    setTimeout(() => t.remove(), 320);
  }, 2500);
}

/* ================= 4. API 调用封装 ================= */
/* 原生窗口走 pywebview 桥；兼容模式（WebView2 不可用，界面跑在浏览器里）走本机 /rpc。
   注意：判断「是不是兼容模式」只能看 URL 里有没有服务端下发的一次性令牌。
   绝不能用 window.pywebview 是否存在来判断 —— app.js 执行时注入脚本还没跑，
   那样会把原生模式误判成浏览器模式，导致启动直接白屏。 */
const MM_TOKEN = (function () {
  try { return new URLSearchParams(location.search).get('k') || ''; } catch (_) { return ''; }
})();
const MM_BROWSER = !!MM_TOKEN;          // true = 兼容模式（浏览器宿主）

function bridgeReady(method) {
  const b = window.pywebview && window.pywebview.api;
  if (!b) return false;
  return method ? typeof b[method] === 'function' : true;
}

/* 原生模式：pywebview 先触发 pywebviewready、之后才把方法挂到 api 上，
   所以不能只等"对象存在"，要等到具体方法可调用，否则 getBootstrap 会
   撞上 not a function，init 直接中断 → 白屏。 */
async function waitBridge(method, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (bridgeReady(method)) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return bridgeReady(method);
}

async function api(method, ...args) {
  try {
    if (!MM_BROWSER) {
      if (!bridgeReady(method) && !(await waitBridge(method))) {
        throw new Error('后端接口未就绪：' + method);
      }
      return await window.pywebview.api[method](...args);
    }
    const r = await fetch('/rpc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ m: method, a: args, t: MM_TOKEN }),
    });
    const j = await r.json();
    if (j && j.error) throw new Error(String(j.error).split('\n').pop());
    return j ? j.result : null;
  } catch (e) {
    toast('操作失败：' + (e && e.message ? e.message : e), 'err');
    throw e;
  }
}

/* 兼容模式没有 evaluate_js 通道：轮询后端事件队列，派发成同样的 CustomEvent */
function startEventPoll() {
  if (!MM_BROWSER) return;
  setInterval(async () => {
    try {
      const evs = await api('pollEvents');
      for (const e of (evs || [])) {
        window.dispatchEvent(new CustomEvent(e.event, { detail: e.detail }));
      }
    } catch (_) { /* 轮询失败不打扰用户 */ }
  }, 600);
}

/* ================= 5. 启动（pywebviewready） ================= */
let booted = false;
async function init() {
  if (booted) return; booted = true;
  bindStaticEvents();
  initSidebarResize();
  initSidebarSections();
  setupSentinel();
  try {
    state.boot = await api('getBootstrap');
  } catch (e) {
    try { window.__mmErr.push('init/getBootstrap: ' + (e && e.message ? e.message : e)); } catch (_) {}
    return;
  }
  document.documentElement.dataset.theme = state.boot.theme === 'light' ? 'light' : 'dark';
  // 恢复用户偏好：视图模式 + 缩略图尺寸
  let cardW = 210;
  try { cardW = Number(localStorage.getItem('mm.cardW')) || 210; } catch (_) {}
  applyCardW(cardW, false);
  // 恢复上次的搜索范围
  try {
    const sf = localStorage.getItem('mm.searchField');
    if (sf === 'name' || sf === 'author' || sf === 'tag') state.filters.searchField = sf;
  } catch (_) {}
  updateScope();
  applyView(state.view, false);
  state.groupBy = state.boot.groupBy || 'folder';
  state.dateGran = state.boot.dateGran || 'month';
  state.wsCounts = state.boot.counts || {};
  updateGroupBtn();
  renderWorkspaces();
  await refreshAll();
  refreshWsCounts();
  // 若后端正在扫描，恢复进度条显示
  try { const st = await api('getScanStatus'); if (st) renderScan(st); } catch (_) {}
}
window.addEventListener('pywebviewready', init);
if (MM_BROWSER) {
  // 兼容模式：没有桥，直接走 RPC 启动（顺便开始拉事件）
  startEventPoll();
  init().then(() => {
    toast('兼容模式：WebView2 组件暂不可用，界面已用浏览器打开');
    // 兼容模式把总数写进标题：窗口标题本身就是"启动成功"的第一手信号
    try { document.title = 'Lumen · ' + state.total + ' 项（兼容模式）'; } catch (_) {}
  });
} else {
  // 原生模式：等 pywebviewready；若事件没来（旧版宿主），桥一出现就启动
  if (bridgeReady('getBootstrap')) init();
  else {
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      if (booted) { clearInterval(timer); return; }
      if (bridgeReady('getBootstrap')) { clearInterval(timer); init(); }
      else if (tries > 120) {          // 约 12s 仍无桥：明确报错，便于排查
        clearInterval(timer);
        document.documentElement.dataset.bootFail = '1';
        toast('后端桥未就绪，请查看 data\\startup.log', 'err');
      }
    }, 100);
  }
}

/* 后端推送事件（合并成一次刷新，避免批量操作时反复整页重建） */
window.addEventListener('scan-progress', (e) => renderScan(e.detail || {}));
let changedTimer = 0;
window.addEventListener('data-changed', () => {
  clearTimeout(changedTimer);
  changedTimer = setTimeout(() => {
    // 数据变化只刷新侧栏/计数；媒体列表由发起的操作自行做增量更新，
    // 避免「打 1000 个标签 → 列表被重置回第一页」
    refreshSidebar();
    refreshWsCounts();
  }, 180);
});

/* ================= 6. 数据加载 ================= */
async function refreshAll(keepSel) {
  await refreshSidebar();
  await runQuery(keepSel);
}

async function refreshSidebar() {
  state.sidebar = await api('getSidebar');
  renderSidebar();
  // 当前工作区计数（侧边栏总数是实时的，直接同步到切换器角标）
  if (state.sidebar && state.sidebar.counts && state.boot) {
    state.wsCounts[state.boot.currentWorkspaceId] = state.sidebar.counts.all;
    syncWsCounts();
  }
}

/* 查询结果的「筛选指纹」：指纹不变说明只是刷新数据，此时保留滚动位置 */
let lastQuerySig = '';
let scrollMemo = -1;
function querySig() {
  const f = state.filters;
  return JSON.stringify([f.folderId, f.folderPath, f.authorId, f.tagIds, f.type,
    f.special, f.search, f.searchField, state.groupBy, state.dateGran]);
}

/* 一页 300 条：首屏只付 300 条的代价，滚动时再续（DOM 永远不会一次撑到 5 万） */
const PAGE = 300;
const AUTO_CAP = 3000;      // 自动续页上限，超过后改为手动「继续加载」

function queryParams(offset, limit) {
  const f = state.filters;
  return [{
    folderId: f.folderId,
    folderPath: f.folderPath,
    authorId: f.authorId,
    tagIds: f.tagIds.slice(),
    type: f.type,
    special: f.special,
    search: f.search,
    searchField: f.searchField,
    groupBy: state.groupBy,
    dateGran: state.dateGran,
  }, offset || 0, limit || PAGE];
}

function countItems(groups) {
  let n = 0;
  for (const g of groups || []) n += (g.items || []).length;
  return n;
}

async function runQuery(keepSel) {
  const seq = ++state.querySeq;
  const sig = querySig();
  if (sig === lastQuerySig) {
    // 只是刷新数据（打标签 / 设作者 / 扫描完成）：保留滚动位置，不闪骨架屏
    const w = $('gridWrap');
    scrollMemo = w ? w.scrollTop : 0;
  } else {
    lastQuerySig = sig;
    scrollMemo = 0;   // 换了筛选条件 → 回到顶部
    showSkeleton();
  }
  let res;
  try {
    res = await api('queryMedia', ...queryParams(0));
  } catch (e) { if (seq === state.querySeq) renderEmpty(); return; }
  if (seq !== state.querySeq) return; // 已有更新的查询
  state.groups = (res && res.groups) || [];
  state.total = (res && res.total) || 0;
  state.loaded = countItems(state.groups);
  state.hasMore = !!(res && res.hasMore);
  state.loadingMore = false;
  state.lastQuery = queryParams(0)[0];
  rebuildFlat();
  if (!keepSel) { state.sel.clear(); state.anchor = -1; }
  else {
    // 保留选择时，剔除已不存在的项，避免「已选 N 项」里混入幽灵
    const alive = new Set(state.flat.map(i => i.id));
    for (const id of Array.from(state.sel)) if (!alive.has(id)) state.sel.delete(id);
  }
  renderGrid();
  renderCrumb();
  updateCounts();
  refreshBatchBar();
}

function rebuildFlat() {
  state.flat = [];
  for (const g of state.groups) for (const it of g.items) state.flat.push(it);
}

/* ================= 7. 工作区切换器 ================= */
function currentWs() {
  return (state.boot.workspaces || []).find(w => w.id === state.boot.currentWorkspaceId) || null;
}
function renderWorkspaces() {
  const seg = $('wsSeg'); seg.innerHTML = '';
  for (const ws of state.boot.workspaces || []) {
    const on = ws.id === state.boot.currentWorkspaceId;
    const b = h('button', { class: 'tb-btn' + (on ? ' on' : ''), title: ws.name + '（右键重命名/删除）' });
    b.append(h('span', { class: 'ws-name', text: ws.name }));
    const n = (state.wsCounts || {})[ws.id];
    b.append(h('span', { class: 'ws-n', text: n == null ? '' : n }));
    b.dataset.ws = ws.id;
    b.addEventListener('click', () => switchWs(ws.id));
    b.addEventListener('contextmenu', (e) => { e.preventDefault(); openWsPop('manage', ws); });
    seg.append(b);
  }
  updateTitle();
}
/* 只刷新角标数字（避免整段重建造成闪烁） */
function syncWsCounts() {
  document.querySelectorAll('#wsSeg .tb-btn').forEach(b => {
    const n = (state.wsCounts || {})[b.dataset.ws];
    const span = b.querySelector('.ws-n');
    if (span && n != null) span.textContent = n;
  });
}
async function refreshWsCounts() {
  try { state.wsCounts = await api('workspaceCounts') || {}; } catch (_) { return; }
  // 当前工作区以侧边栏总数（实时）为准
  if (state.sidebar && state.sidebar.counts) {
    state.wsCounts[state.boot.currentWorkspaceId] = state.sidebar.counts.all;
  }
  syncWsCounts();
}
function updateTitle() {
  const ws = currentWs();
  // 副标题已移除：总数见状态栏 / 工作区角标
}
async function switchWs(id) {
  if (id === state.boot.currentWorkspaceId) return;
  const inner = $('contentInner');
  inner.classList.add('fading'); // 内容交叉淡入
  await new Promise(r => setTimeout(r, 220));
  const r = await api('switchWorkspace', id);
  if (r && r.ok) {
    state.boot.currentWorkspaceId = id;
    state.activeKey = 'lib:all';
    state.filters = { folderId: null, folderPath: null, authorId: null, tagIds: [], type: null, special: null, search: '', searchField: state.filters.searchField };
    $('searchInput').value = ''; $('searchBox').classList.remove('has-text');
    renderWorkspaces();
    await refreshAll();
  }
  inner.classList.remove('fading');
}

/* 工作区弹层：create=新建 / manage=重命名+删除 */
let wsPopMode = 'create', wsPopTarget = null;
function openWsPop(mode, ws) {
  wsPopMode = mode; wsPopTarget = ws || null;
  closePops();
  const pop = $('wsPop'), sw = $('wsSwitch').getBoundingClientRect();
  pop.style.left = Math.max(8, sw.left) + 'px';
  pop.style.top = (sw.bottom + 8) + 'px';
  pop.style.bottom = 'auto';
  pop.style.transformOrigin = 'top left';
  $('wsPopTitle').textContent = mode === 'create' ? '新建工作区' : '重命名工作区';
  $('wsName').value = mode === 'manage' && ws ? ws.name : '';
  $('wsCreateBtn').textContent = mode === 'create' ? '创建' : '重命名';
  // manage 模式追加删除按钮（先清掉上一次的，避免重复）
  pop.querySelectorAll('.ws-manage-extra').forEach(n => n.remove());
  if (mode === 'manage') {
    const del = h('div', { class: 'pop-item danger ws-manage-extra', html: SVG.err });
    del.append(h('span', { text: '删除该工作区' }));
    del.addEventListener('click', deleteWs);
    pop.append(h('div', { class: 'pop-sep ws-manage-extra' }), del);
  }
  pop.classList.add('show');
  setTimeout(() => $('wsName').focus(), 30);
}
async function submitWsPop() {
  const name = $('wsName').value.trim();
  if (!name) { toast('请输入工作区名称', 'err'); return; }
  if (wsPopMode === 'create') {
    const ws = await api('createWorkspace', name);
    if (ws && ws.id != null) {
      state.boot.workspaces.push(ws);
      renderWorkspaces();
      closePops();
      toast('工作区「' + ws.name + '」已创建');
      switchWs(ws.id);
    }
  } else if (wsPopTarget) {
    await api('renameWorkspace', wsPopTarget.id, name);
    wsPopTarget.name = name;
    renderWorkspaces();
    closePops();
    toast('已重命名为「' + name + '」');
  }
}
async function deleteWs() {
  if (!wsPopTarget) return;
  const target = wsPopTarget;
  const wasCurrent = target.id === state.boot.currentWorkspaceId;
  const ok = await askConfirm({
    title: '删除工作区「' + target.name + '」？',
    msg: '该工作区的全部索引、标签与作者整理记录都会被删除（磁盘上的媒体文件不受影响）。删除后无法恢复。',
    confirm: '删除工作区', danger: true,
  });
  if (!ok) return;
  const r = await api('deleteWorkspace', target.id);
  if (r && r.ok) {
    state.boot.workspaces = state.boot.workspaces.filter(w => w.id !== target.id);
    closePops();
    toast('工作区已删除');
    if (wasCurrent && r.currentWorkspaceId != null) {
      // 删除的是当前工作区 → 切到后端指定的新工作区
      state.boot.currentWorkspaceId = null;
      await switchWs(r.currentWorkspaceId);
    } else {
      renderWorkspaces();
      await refreshAll();
      refreshWsCounts();
    }
  }
}

/* ================= 8. 侧边栏渲染与筛选 ================= */

/* ---- 文件夹树：展开/收起状态（按工作区记忆） ---- */
let folderCollapsed = new Set();
let foldStateWs = null;

function loadFoldState() {
  const ws = state.boot.currentWorkspaceId || 0;
  if (foldStateWs === ws) return;
  foldStateWs = ws;
  try { folderCollapsed = new Set(JSON.parse(localStorage.getItem('mm.folded.' + ws) || '[]')); }
  catch (_) { folderCollapsed = new Set(); }
}
function saveFoldState() {
  try { localStorage.setItem('mm.folded.' + (state.boot.currentWorkspaceId || 0),
        JSON.stringify([...folderCollapsed])); } catch (_) {}
}
function buildFolderTree(folders) {
  const byPath = new Map(folders.map(f => [f.path, Object.assign({ children: [] }, f)]));
  const roots = [];
  for (const f of byPath.values()) {
    const p = f.parent && byPath.get(f.parent);
    if (p) p.children.push(f); else roots.push(f);
  }
  const cmp = (a, b) => (a.path.toLowerCase() < b.path.toLowerCase() ? -1 : 1);
  for (const f of byPath.values()) f.children.sort(cmp);
  roots.sort(cmp);
  return roots;
}
/* 激活的文件夹若在收起分支里，逐级展开祖先 */
function ensureFolderVisible(path) {
  const byPath = new Map((state.sidebar.folders || []).map(f => [f.path, f]));
  let p = path, changed = false;
  while (p) {
    const f = byPath.get(p);
    if (!f) break;
    if (folderCollapsed.has(f.path)) { folderCollapsed.delete(f.path); changed = true; }
    p = f.parent;
  }
  if (changed) saveFoldState();
}
function renderFolderTree(box, nodes, level) {
  for (const f of nodes) {
    const it = h('div', { class: 'side-item' + (level > 0 ? ' indent' : ''), title: f.path });
    it.style.paddingLeft = (8 + level * 16) + 'px';
    if (f.hasChildren) {
      const tw = h('span', { class: 'tw' + (folderCollapsed.has(f.path) ? '' : ' open'),
                             title: folderCollapsed.has(f.path) ? '展开' : '收起', html: SVG.chevR });
      tw.addEventListener('click', (e) => {
        e.stopPropagation();
        if (folderCollapsed.has(f.path)) folderCollapsed.delete(f.path);
        else folderCollapsed.add(f.path);
        saveFoldState();
        renderSidebar();
      });
      it.append(tw);
    } else {
      it.append(h('span', { class: 'tw none' }));
    }
    it.append(h('span', { class: 'ficon', html: SVG.folder }));
    it.append(h('span', { class: 'txt', text: f.name }), h('span', { class: 'count', text: f.count }));
    it.dataset.key = 'folder:' + f.id;
    it.addEventListener('click', () => applyFilter('folder:' + f.id, { folderId: f.id, folderPath: f.path }));
    it.addEventListener('contextmenu', (e) => { e.preventDefault(); folderMenu(f, e.clientX, e.clientY); });
    box.append(it);
    if (f.hasChildren && !folderCollapsed.has(f.path)) renderFolderTree(box, f.children, level + 1);
  }
}

function renderSidebar() {
  const sb = state.sidebar; if (!sb) return;
  loadFoldState();
  const c = sb.counts || {};
  $('cntAll').textContent = c.all ?? '';
  $('cntImages').textContent = c.images ?? '';
  $('cntVideos').textContent = c.videos ?? '';
  $('cntUntagged').textContent = c.untagged ?? '';
  $('cntUnknown').textContent = c.unknownAuthor ?? '';

  // 文件夹（可展开/收起的层级树）
  const fbox = $('sideFolders'); fbox.innerHTML = '';
  const folds = sb.folders || [];
  if (!folds.length) fbox.append(h('div', { class: 'side-item empty-hint', text: '尚无文件夹' }));
  else {
    if (typeof state.activeKey === 'string' && state.activeKey.startsWith('folder:')) {
      const act = folds.find(f => 'folder:' + f.id === state.activeKey);
      if (act) ensureFolderVisible(act.path);
    }
    renderFolderTree(fbox, buildFolderTree(folds), 0);
  }

  // 作者（头像色点 + 名称 + 数量）
  const abox = $('sideAuthors'); abox.innerHTML = '';
  if (!(sb.authors || []).length) abox.append(h('div', { class: 'side-item empty-hint', text: '尚无作者' }));
  for (const a of sb.authors || []) {
    const hue = hashStr(a.name) % 360;
    const dot = h('span', { class: 'dot-author', text: (a.name || '?').slice(0, 1) });
    dot.style.background = 'linear-gradient(135deg, hsl(' + hue + ',70%,60%), hsl(' + ((hue + 40) % 360) + ',70%,48%))';
    const it = h('div', { class: 'side-item', title: a.name });
    it.append(dot, h('span', { class: 'txt', text: a.name }), h('span', { class: 'count', text: a.count }));
    it.dataset.key = 'author:' + a.id;
    it.addEventListener('click', () => applyFilter('author:' + a.id, { authorId: a.id }));
    it.addEventListener('contextmenu', (e) => { e.preventDefault(); authorMenu(a, e.clientX, e.clientY); });
    abox.append(it);
  }

  // 标签已移到标题栏中央面板（给左侧文件夹树腾空间），侧栏不再渲染
  const tbox = $('sideTags');
  if (tbox) { tbox.innerHTML = ''; }

  markActiveSideItem();
  loadSecState();
  const tp = $('tagPanel');
  if (tp && tp.classList.contains('show')) renderTagPanel();
}

/* ---- 侧栏分区高度拖拽：上下拉动分隔条改变相邻两区的高度 ----
   注意两个坑（这两个坑会让"拉大"完全失效）：
   1) .side-section 默认 flex: 0 1 auto（shrink=1）。侧栏内容撑满容器时，
      浏览器会把拉大的部分按 shrink 比例当场压回去 —— 表现就是"只能缩小、拖不大"。
      所以拖拽时必须改成 flex: 0 0 Npx（不可压缩）。
   2) 可分配的总高度必须扣掉"其它分区 + 所有拖拽条 + 侧栏 padding"，
      否则两个分区的固定高度会超出容器，被 .sidebar 的 overflow:hidden 裁掉。
*/
const SEC_MIN = 58;              // 每区最小高度：标题 + 至少一行内容
const SEC_KEY = 'mm.secH';

function secList(sb) {
  return [...sb.querySelectorAll(':scope > .side-section[data-sec]')];
}

function usableHeight(sb) {
  const cs = getComputedStyle(sb);
  const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  let grip = 0;
  sb.querySelectorAll(':scope > .side-grip').forEach(g => {
    grip += g.getBoundingClientRect().height;
  });
  return Math.max(0, sb.clientHeight - pad - grip);
}

function setSecH(sec, px) {
  sec.style.flex = '0 0 ' + Math.max(SEC_MIN, Math.round(px)) + 'px';
}

/* 折叠/展开或窗口尺寸变化后，把未折叠的分区按比例重新分配可用高度 */
function rebalanceSecs() {
  const sb = $('sidebar');
  if (!sb) return;
  const secs = secList(sb);
  const room = usableHeight(sb);
  let fixed = 0;
  const items = [];
  secs.forEach(s => {
    if (s.classList.contains('collapsed')) fixed += s.getBoundingClientRect().height;
    else items.push(s);
  });
  if (!items.length) return;
  const avail = Math.max(SEC_MIN * items.length, room - fixed);
  const cur = items.reduce((n, s) => n + s.getBoundingClientRect().height, 0);
  if (cur <= 0) return;
  const k = avail / cur;
  items.forEach(s => setSecH(s, s.getBoundingClientRect().height * k));
}

function persistSecHeights() {
  const out = {};
  document.querySelectorAll('#sidebar > .side-section[data-sec]').forEach(s => {
    if (!s.classList.contains('collapsed')) {
      out[s.dataset.sec] = Math.round(s.getBoundingClientRect().height);
    }
  });
  try { localStorage.setItem(SEC_KEY, JSON.stringify(out)); } catch (_) {}
}

function initSidebarResize() {
  const sb = $('sidebar');
  if (!sb) return;
  const secs = secList(sb);

  // 先插好所有拖拽条，usableHeight() 才好把它们的总高扣掉
  secs.forEach((sec, i) => {
    if (i >= secs.length - 1) return;
    const grip = h('div', { class: 'side-grip', title: '上下拖动，调整两个区块的高度' });
    grip.addEventListener('pointerdown', (e) => startSecDrag(e, sec, secs[i + 1], grip));
    sec.after(grip);
  });

  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(SEC_KEY) || '{}'); } catch (_) {}
  const anySaved = secs.some(s => saved[s.dataset.sec] != null);
  if (anySaved) {
    // 恢复上次高度；窗口比上次小时按比例收进来，避免一上线就溢出
    const room = usableHeight(sb);
    let used = 0;
    secs.forEach(s => { if (saved[s.dataset.sec] != null) used += saved[s.dataset.sec]; });
    const k = used > room && used > 0 ? room / used : 1;
    secs.forEach(s => {
      const px = saved[s.dataset.sec];
      if (px != null) setSecH(s, px * k);
    });
  } else {
    // 首次布局：不能让浏览器按 flex-shrink 把上面几个分区压成一条（原 bug 的观感来源）。
    // 按可用空间均分，给内容高的分区（文件夹/作者）多留一点，富余全部给"文件夹"。
    const room = usableHeight(sb);
    const base = Math.max(SEC_MIN, Math.floor(room / secs.length));
    let used = 0;
    secs.forEach(s => {
      const content = s.getBoundingClientRect().height;
      const want = Math.round(Math.min(base, Math.max(SEC_MIN, content + 24)));
      s._want = want;
      used += want;
    });
    // 富余给文件夹区（内容通常会更多）
    let spare = room - used;
    secs.forEach(s => {
      if (spare > 0 && (s.dataset.sec === 'folders' || s.dataset.sec === 'authors')) {
        s._want += Math.floor(spare / 2);
        spare -= Math.floor(spare / 2);
      }
      setSecH(s, s._want);
      delete s._want;
    });
  }

  // 窗口缩放后重新平衡（防抖）
  let rt = 0;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => { rebalanceSecs(); persistSecHeights(); }, 140);
  });
}

function startSecDrag(e, a, b, grip) {
  if (e.button != null && e.button !== 0) return;
  e.preventDefault();
  grip.classList.add('drag');
  document.body.classList.add('resizing-row');
  const sb = $('sidebar');
  const secs = secList(sb);

  // 其它分区至少要留下的高度（它们仍可被压缩到 min-height，所以用最小值算上限）
  let othersMin = 0;
  secs.forEach(s => {
    if (s !== a && s !== b && !s.classList.contains('collapsed')) othersMin += SEC_MIN;
  });
  // a 能拉到多大：可用高度 − 其它分区最小需求 − b 的最小高度
  const hi = Math.max(SEC_MIN + 1, usableHeight(sb) - othersMin - SEC_MIN);

  let ha = a.getBoundingClientRect().height;
  let hb = b.getBoundingClientRect().height;
  const sum = ha + hb;
  // 两个分区都变成不可压缩，否则拉大的部分会被 flex-shrink 立刻吃掉
  setSecH(a, ha);
  setSecH(b, hb);

  const y0 = e.clientY;
  const move = (ev) => {
    const na = Math.min(hi, Math.max(SEC_MIN, ha + (ev.clientY - y0)));
    // b 至少 SEC_MIN；a+b 超出原总和时，多出来的空间由下面其它分区让出
    setSecH(a, na);
    setSecH(b, Math.max(SEC_MIN, sum - na));
  };
  const up = () => {
    grip.classList.remove('drag');
    document.body.classList.remove('resizing-row');
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', up);
    persistSecHeights();
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);
}

/* ---- 侧栏主分区：一键收起 / 展开（按工作区记忆，macOS 手风琴手感） ---- */
let secCollapsed = new Set();
let secStateWs = null;

function loadSecState(force) {
  const ws = state.boot ? (state.boot.currentWorkspaceId || 0) : 0;
  if (secStateWs !== ws || force) {
    secStateWs = ws;
    try { secCollapsed = new Set(JSON.parse(localStorage.getItem('mm.sec.' + ws) || '[]')); }
    catch (_) { secCollapsed = new Set(); }
  }
  applySecStates();
}
function saveSecState() {
  try {
    localStorage.setItem('mm.sec.' + (state.boot.currentWorkspaceId || 0),
      JSON.stringify([...secCollapsed]));
  } catch (_) {}
}
function applySecStates() {
  document.querySelectorAll('.side-section[data-sec]').forEach(sec => {
    const collapsed = secCollapsed.has(sec.dataset.sec);
    sec.classList.toggle('collapsed', collapsed);
    const body = sec.querySelector('.side-body');
    if (body) body.style.maxHeight = collapsed ? '0px' : '';
    const tw = sec.querySelector('.sec-tw');
    if (tw) { tw.classList.toggle('collapsed', collapsed); tw.title = collapsed ? '展开' : '收起'; }
  });
  updateFolderAllBtn();
}
function toggleSection(key) {
  const sec = document.querySelector('.side-section[data-sec="' + key + '"]');
  if (!sec) return;
  const body = sec.querySelector('.side-body');
  const collapse = !sec.classList.contains('collapsed');
  if (body) {
    body.style.maxHeight = body.scrollHeight + 'px';   // 先量高度，动画才有起点
    if (collapse) { void body.offsetHeight; body.style.maxHeight = '0px'; }
  }
  sec.classList.toggle('collapsed', collapse);
  if (collapse) secCollapsed.add(key); else secCollapsed.delete(key);
  saveSecState();
  const tw = sec.querySelector('.sec-tw');
  if (tw) { tw.classList.toggle('collapsed', collapse); tw.title = collapse ? '展开' : '收起'; }
  // 折叠/展开会改变可分配空间：立刻重新平衡，别等用户再拖一次
  requestAnimationFrame(() => rebalanceSecs());
  clearTimeout(sec._mt);
  sec._mt = setTimeout(() => {
    if (body && !sec.classList.contains('collapsed')) body.style.maxHeight = '';
    rebalanceSecs();
    persistSecHeights();
  }, 320);
}

/* ---- 文件夹树：整棵「全部收起 / 全部展开」（与单个节点的 ▸ 互不干扰） ---- */
function folderParents() {
  return (state.sidebar && state.sidebar.folders || []).filter(f => f.hasChildren);
}
function updateFolderAllBtn() {
  const btn = document.querySelector('.sec-all[data-all="folders"]');
  if (!btn) return;
  const parents = folderParents();
  btn.classList.toggle('hide', !parents.length);
  const allClosed = parents.length > 0 && parents.every(f => folderCollapsed.has(f.path));
  btn.classList.toggle('closed', allClosed);
  btn.title = allClosed ? '展开全部子文件夹' : '收起全部子文件夹';
}
function toggleAllFolders() {
  const parents = folderParents();
  if (!parents.length) return;
  const allClosed = parents.every(f => folderCollapsed.has(f.path));
  folderCollapsed = new Set();
  if (!allClosed) for (const f of parents) folderCollapsed.add(f.path);
  saveFoldState();
  renderSidebar();
  toast(allClosed ? '已展开全部子文件夹' : '已收起全部子文件夹');
}
function initSidebarSections() {
  document.querySelectorAll('.side-section[data-sec]').forEach(sec => {
    const key = sec.dataset.sec;
    const title = sec.querySelector('.side-title');
    if (title) {
      title.addEventListener('click', (e) => {
        if (e.target.closest('.sec-all')) return;
        toggleSection(key);
      });
    }
    const all = sec.querySelector('.sec-all');
    if (all) all.addEventListener('click', (e) => { e.stopPropagation(); toggleAllFolders(); });
  });
  loadSecState(true);
}

/* ---------- 搜索范围：自绘胶囊 + 玻璃菜单（与右键菜单同一套渲染） ---------- */
const SCOPE_LABEL = { name: '文件名', author: '作者', tag: '标签' };

function updateScope() {
  const v = state.filters.searchField || 'name';
  const lab = $('sfLabel');
  if (lab) lab.textContent = SCOPE_LABEL[v] || '文件名';
  const si = $('searchInput');
  if (si) si.placeholder = '搜索' + (SCOPE_LABEL[v] || '');
}

function openScopeMenu() {
  const btn = $('searchField');
  if (!btn) return;
  if (btn.classList.contains('sf-open')) { closeCtx(); return; } // 再点一次收起
  const r = btn.getBoundingClientRect();
  const items = [{ head: true, label: '搜索范围' }];
  for (const v of ['name', 'author', 'tag']) {
    items.push({
      label: SCOPE_LABEL[v],
      checked: state.filters.searchField === v,
      onClick: () => {
        if (state.filters.searchField === v) return;
        state.filters.searchField = v;
        updateScope();
        try { localStorage.setItem('mm.searchField', v); } catch (_) {}
        runQuery(); // 换搜索范围立即重查
      },
    });
  }
  showCtx(items, r.left, r.bottom + 6);
  btn.classList.add('sf-open');
}

function baseFilters() { return { folderId: null, folderPath: null, authorId: null, tagIds: [], type: null, special: null, search: state.filters.search, searchField: state.filters.searchField }; }

/* ---------- 标签面板（标题栏中央）：点击标签即叠加/移除筛选，多个标签 = 同时满足 ---------- */
function openTagPanel() {
  const pop = $('tagPanel'), btn = $('tagBtn');
  if (!pop || !btn) return;
  if (pop.classList.contains('show')) { closePops(); return; }
  closePops();
  const r = btn.getBoundingClientRect();
  const W = 400;
  pop.style.left = Math.max(10, Math.min(window.innerWidth - W - 12,
    r.left + r.width / 2 - W / 2)) + 'px';
  pop.style.top = (r.bottom + 8) + 'px';
  pop.style.bottom = 'auto';
  pop.style.transformOrigin = 'top center';
  pop.classList.add('show');
  btn.classList.add('open');
  renderTagPanel();
  const f = $('tagFilter');
  if (f) f.value = '';
}

function renderTagPanel() {
  const box = $('tagChips');
  if (!box) return;
  const tags = (state.sidebar && state.sidebar.tags) || [];
  const f = $('tagFilter');
  const q = (f ? f.value : '').trim().toLowerCase();
  const shown = q ? tags.filter(t => (t.name || '').toLowerCase().includes(q)) : tags;
  box.innerHTML = '';
  if (!shown.length) {
    box.append(h('div', { class: 'tp-empty', text: q ? '没有匹配的标签' : '还没有标签，点右下角新建' }));
  }
  for (const t of shown) {
    const on = state.filters.tagIds.includes(t.id);
    const chip = h('button', { class: 'tag-chip' + (on ? ' on' : ''), title: t.name + ' · ' + t.count + ' 项' });
    const dot = h('span', { class: 'dot-tag' });
    dot.style.background = t.color || 'var(--accent)';
    chip.append(dot, h('span', { class: 'tc-name', text: t.name }),
      h('span', { class: 'tc-n', text: t.count }));
    chip.addEventListener('click', (e) => { e.stopPropagation(); onTagClick(t.id, e); });
    chip.addEventListener('contextmenu', (e) => { e.preventDefault(); e.stopPropagation(); tagMenu(t, e.clientX, e.clientY); });
    box.append(chip);
  }
  // 顶部按钮上的角标 + 高亮
  const n = state.filters.tagIds.length;
  const badge = $('tagBadge');
  if (badge) {
    badge.textContent = n;
    badge.classList.toggle('hide', n === 0);
  }
  const btn = $('tagBtn');
  if (btn) {
    btn.classList.toggle('active', n > 0 || state.filters.special === 'untagged');
    btn.title = n ? ('标签筛选：' + n + ' 个（同时满足）') : '标签';
  }
  const un = $('tpUntagged');
  if (un) un.classList.toggle('on', state.filters.special === 'untagged');
}

/* 点标签 = 加入/移除当前筛选（多标签 AND；取消到空即回到全部媒体） */
function onTagClick(id, e) {
  const has = state.filters.tagIds.includes(id);
  const ids = has
    ? state.filters.tagIds.filter(x => x !== id)
    : state.filters.tagIds.concat([id]);
  if (!ids.length) { applyFilter('lib:all', {}); return; }
  applyFilter('tags:' + ids.slice().sort((a, b) => a - b).join(','), { tagIds: ids });
}

async function createTagOnly() {
  const name = await askText({ title: '新建标签', placeholder: '标签名称', confirm: '创建' });
  if (!name) return;
  const palette = ['#0A84FF', '#30D158', '#FF9F0A', '#FF453A', '#BF5AF2', '#64D2FF', '#FFD60A', '#FF375F'];
  const color = palette[Math.abs(hashStr(name)) % palette.length];
  const t = await api('createTag', name, color);
  if (t && t.id != null) {
    await refreshSidebar();
    renderTagPanel();
    toast('标签「' + t.name + '」已创建');
  }
}

/* 标签变化后只更新受影响的列表行（网格不显示标签，无需重绘） */
function updateTagsInDom(ids) {
  if (state.view !== 'list') return;
  const want = new Set(ids);
  for (const it of state.flat) {
    if (!want.has(it.id)) continue;
    const row = document.querySelector('.lrow[data-id="' + it.id + '"]');
    if (!row) continue;
    const tc = row.querySelector('.lcell.tags');
    if (tc) fillTagCell(tc, it);
  }
}

/* 设置筛选并查询；与当前激活项相同则不重复查询 */
function applyFilter(key, patch) {
  if (key === state.activeKey) return;
  state.activeKey = key;
  state.filters = Object.assign(baseFilters(), patch);
  markActiveSideItem();
  runQuery();
  if (window.innerWidth <= 1000) toggleSidebar(true); // 抽屉模式下选定即收起
}

function markActiveSideItem() {
  document.querySelectorAll('.side-item[data-key]').forEach(el => {
    el.classList.toggle('active', el.dataset.key === state.activeKey);
  });
  document.querySelectorAll('[data-lib]').forEach(el => {
    el.classList.toggle('active', state.activeKey === 'lib:' + el.dataset.lib);
  });
  document.querySelectorAll('[data-special]').forEach(el => {
    el.classList.toggle('active', state.activeKey === 'special:' + el.dataset.special);
  });
  renderTagPanel();   // 标签在标题栏面板里，激活态/角标同步
}

/* 面包屑 */
function renderCrumb() {
  const f = state.filters, sb = state.sidebar;
  let title = '全部媒体';
  if (f.special === 'untagged') title = '无标签';
  else if (f.special === 'unknownAuthor') title = '未识别作者';
  else if (f.type === 'image') title = '图片';
  else if (f.type === 'video') title = '视频';
  else if (f.folderPath && sb) title = ((sb.folders || []).find(x => x.path === f.folderPath) || {}).name || '文件夹';
  else if (f.authorId != null && sb) title = ((sb.authors || []).find(x => x.id === f.authorId) || {}).name || '作者';
  else if (f.tagIds.length && sb) {
    title = '标签：' + f.tagIds.map(id => ((sb.tags || []).find(x => x.id === id) || {}).name || id).join(' × ');
  }
  $('crumbTitle').textContent = title;
  const subs = [state.total + ' 项'];
  if (f.search) subs.push('搜索：“' + f.search + '”');
  $('crumbSub').textContent = subs.join(' · ');
}

/* 标题副标题 + 状态栏计数 */
function updateCounts() {
  updateTitle();
  $('statusLeft').textContent = '共 ' + state.total + ' 项' + (state.sel.size ? ' · 已选 ' + state.sel.size + ' 项' : '');
}

/* ================= 9. 媒体网格渲染 ================= */
function showSkeleton() {
  const w = $('gridWrap'); w.innerHTML = '';
  const g = h('div', { class: 'grid' });
  for (let i = 0; i < 8; i++) {
    const card = h('div', { class: 'card skel enter' });
    card.style.setProperty('--i', i);
    card.append(h('div', { class: 'thumb' }), h('div', { class: 'sline' }), h('div', { class: 'sline short' }));
    g.append(card);
  }
  w.append(g);
}

/* ---- 分组块 + 分页渲染（DOM 只保留已加载的部分，滚动续取） ---- */
let groupEls = new Map();     // gkey -> { blk, itemsBox, cntEl }

function groupHeadText(grp) {
  const n = (grp.count != null && grp.count > 0) ? grp.count : (grp.items || []).length;
  return (grp.folderName || '未分组') + ' · ' + n;
}

/* 插入一个分组块（视图切换时会重建全部，续页时只追加新块） */
function appendGroupBlock(w, grp, ref, beforeEl) {
  const isList = state.view === 'list';
  const blk = h('div', { class: isList ? 'lgroup' : 'gblock' });
  const head = h('div', { class: isList ? 'lhead' : 'group-title', html: SVG.folder });
  const cntEl = h('span', { text: groupHeadText(grp) });
  head.append(cntEl, h('span', { class: 'gpath', text: grp.path || '' }));
  wireGroupHead(head, grp);
  const itemsBox = h('div', { class: isList ? '' : 'grid' });
  blk.append(head, itemsBox);
  if (beforeEl && beforeEl.parentNode === w) w.insertBefore(blk, beforeEl);
  else w.append(blk);
  for (const item of grp.items) {
    itemsBox.append(isList ? renderRow(item, ref.n) : renderCard(item, ref.n));
    ref.n++;
  }
  groupEls.set(grp.key, { blk, itemsBox, cntEl });
  return blk;
}

function buildListHeader() {
  const head = h('div', { class: 'lheader' });
  head.append(h('span'), lcell('name', '文件名'), lcell('author', '作者'),
    lcell('tags col-tags', '标签'), lcell('kind', '类型/时长'),
    lcell('dims col-dims', '尺寸'), lcell('size col-size', '大小'),
    lcell('date col-date', '修改日期'));
  return head;
}

/* 按当前视图渲染已加载的全部内容（切换视图 / 换筛选条件时） */
function renderGrid() {
  const w = $('gridWrap');
  w.innerHTML = '';
  groupEls = new Map();
  if (!state.groups.length) { scrollMemo = -1; renderEmpty(); return; }
  if (state.view === 'list') w.append(buildListHeader());
  const ref = { n: 0 };
  for (const grp of state.groups) appendGroupBlock(w, grp, ref);
  ensureSentinel();
  requestAnimationFrame(() => w.querySelectorAll('.card, .lrow').forEach(c => c.classList.add('enter')));
  // 还原滚动位置：刷新数据（打标签/设作者/扫描完成）不应该把视图弹回顶部
  const keep = scrollMemo >= 0 ? scrollMemo : w.scrollTop;
  scrollMemo = -1;
  if (keep > 0) {
    w.scrollTop = keep;
    requestAnimationFrame(() => { if (w.scrollTop !== keep) w.scrollTop = keep; });
  }
}

/* ---- 续页哨兵：进入视口就自动取下一页；超过 3000 项改为点击加载 ---- */
let sentinelEl = null, sentinelIO = null;

function setupSentinel() {
  const w = $('gridWrap');
  if (!('IntersectionObserver' in window)) return;
  sentinelIO = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) loadNextPage(false);
  }, { root: w, rootMargin: '800px 0px' });
}

function ensureSentinel() {
  const w = $('gridWrap');
  if (!state.hasMore) {
    if (sentinelEl) { sentinelEl.remove(); sentinelEl = null; }
    return;
  }
  if (!sentinelEl || !sentinelEl.isConnected) {
    sentinelEl = h('div', { class: 'load-more' });
    sentinelEl.addEventListener('click', () => {
      if (sentinelEl && sentinelEl.classList.contains('manual')) loadNextPage(true);
    });
    if (sentinelIO) sentinelIO.observe(sentinelEl);
  }
  w.append(sentinelEl);            // 永远排在最后
  const manual = state.loaded >= AUTO_CAP;
  sentinelEl.className = 'load-more' + (manual ? ' manual' : '');
  sentinelEl.textContent = manual
    ? '已加载 ' + state.loaded + ' / ' + state.total + ' 项 · 点击继续加载'
    : '正在加载更多…';
}

async function loadNextPage(manual) {
  if (!state.hasMore || state.loadingMore || !state.lastQuery) return;
  if (!manual && state.loaded >= AUTO_CAP) { ensureSentinel(); return; }
  state.loadingMore = true;
  const seq = state.querySeq;
  try {
    const res = await api('queryMedia', state.lastQuery, state.loaded, PAGE);
    if (seq !== state.querySeq) return;   // 期间换了筛选条件
    if (res) appendPage(res);
  } catch (_) {
    /* 续页失败不打断浏览 */
  } finally {
    state.loadingMore = false;
  }
}

/* 只追加新一页的 DOM，不动已渲染的卡片（滚动不掉帧） */
function appendPage(res) {
  const w = $('gridWrap');
  const ref = { n: state.flat.length };
  for (const g of res.groups || []) {
    const cur = groupEls.get(g.key);
    if (cur) {
      const target = state.groups.find(x => x.key === g.key);
      for (const item of g.items) {
        cur.itemsBox.append(state.view === 'list' ? renderRow(item, ref.n) : renderCard(item, ref.n));
        ref.n++;
        state.flat.push(item);
        if (target) target.items.push(item);
      }
      if (target) cur.cntEl.textContent = groupHeadText(target);
    } else {
      const ng = Object.assign({}, g, { items: (g.items || []).slice() });
      appendGroupBlock(w, ng, ref, $('loadMore'));
      for (const it of ng.items) state.flat.push(it);
      state.groups.push(ng);
    }
  }
  state.loaded = state.flat.length;
  state.hasMore = !!res.hasMore;
  ensureSentinel();
  // 新进来的一批做轻微入场（只给前 24 个，避免长列表整体动画拖慢滚动）
  requestAnimationFrame(() => {
    const fresh = w.querySelectorAll('.card:not(.enter), .lrow:not(.enter)');
    for (let i = 0; i < fresh.length && i < 24; i++) fresh[i].classList.add('enter');
  });
  syncSelDom();
  updateCounts();
}

/* 分组标题：按文件夹分组时，单击即可只看这个文件夹 */
function wireGroupHead(el, grp) {
  if (state.groupBy === 'folder' && grp.path) {
    el.classList.add('clickable');
    el.title = grp.path + '\n单击只看此文件夹 · 右键更多操作';
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      applyFilter('folder:' + grp.folderId, { folderId: grp.folderId, folderPath: grp.path });
    });
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault(); e.stopPropagation();
      folderMenu({ id: grp.folderId, path: grp.path, name: grp.folderName,
        folderName: grp.folderName }, e.clientX, e.clientY);
    });
  } else {
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault(); e.stopPropagation();
      blankMenu(e.clientX, e.clientY);
    });
  }
}

/* 缩略图：懒加载 + 加载失败自动重试（首次请求会即时生成），全部失败才回退渐变 */
function attachThumb(container, item) {
  if (!item.thumbUrl) { container.classList.add(gradClass(item.name)); return; }
  const img = h('img', { src: item.thumbUrl, loading: 'lazy', decoding: 'async', draggable: 'false', alt: '' });
  let tries = 0;
  img.addEventListener('load', () => { img.classList.add('ok'); container.classList.remove(gradClass(item.name)); });
  img.addEventListener('error', () => {
    if (tries < 4) {
      tries++;
      setTimeout(() => { img.src = item.thumbUrl + '?r=' + tries; }, 450 * tries);
      return;
    }
    img.remove();
    container.classList.add(gradClass(item.name));
  });
  container.append(img);
}

/* ---- 列表视图（Finder 风格；列：缩略图/文件名/作者/标签/类型·时长/尺寸/大小/修改日期） ---- */
function lcell(cls, txt) { return h('div', { class: 'lcell ' + cls, text: txt }); }

function renderRow(item, idx) {
  const row = h('div', { class: 'lrow' + (idx % 2 ? ' alt' : '') + (state.sel.has(item.id) ? ' sel' : '') });
  row.style.setProperty('--i', idx % 16);
  row.dataset.id = item.id;
  row.dataset.idx = idx;

  // 小缩略图（失败自动重试，最终回退渐变；视频叠播放徽标）
  const th = h('div', { class: 'lthumb' });
  attachThumb(th, item);
  if (item.type === 'video') th.append(h('span', { class: 'play', html: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>' }));
  row.append(th);

  const nm = lcell('name', item.name); nm.title = item.name;
  row.append(nm);
  row.append(lcell('author' + (item.authorName ? '' : ' none'), item.authorName || '未识别'));

  // 标签：彩色圆点+名称，最多 3 个，多余 +N
  const tc = h('div', { class: 'lcell tags col-tags' });
  fillTagCell(tc, item);
  row.append(tc);

  row.append(lcell('kind', item.type === 'video' ? ('视频' + (item.duration != null ? ' ' + fmtDur(item.duration) : '')) : '图片'));
  row.append(lcell('dims col-dims', (item.width && item.height) ? item.width + '×' + item.height : ''));
  row.append(lcell('size col-size', fmtSize(item.size)));
  row.append(lcell('date col-date', fmtDate(item.mtime)));

  row.addEventListener('click', (e) => onCardClick(item.id, idx, e)); // 与网格完全一致的选择逻辑
  row.addEventListener('dblclick', () => openPreview(idx));
  row.addEventListener('contextmenu', (e) => { e.preventDefault(); e.stopPropagation(); onItemContext(item, idx, e); });
  return row;
}

function fillTagCell(tc, item) {
  tc.innerHTML = '';
  const tags = item.tags || [];
  for (const t of tags.slice(0, 3)) {
    const dot = h('span', { class: 'dot-tag' });
    dot.style.background = t.color || 'var(--accent)';
    tc.append(h('span', { class: 't' }, dot, h('span', { text: t.name })));
  }
  if (tags.length > 3) tc.append(h('span', { class: 'more', text: '+' + (tags.length - 3) }));
}

function renderCard(item, idx) {
  const card = h('div', { class: 'card' + (state.sel.has(item.id) ? ' sel' : '') });
  card.style.setProperty('--i', idx % 12);
  card.dataset.id = item.id;
  card.dataset.idx = idx;

  const thumb = h('div', { class: 'thumb' });
  attachThumb(thumb, item);

  // 视频：右下角时长徽标
  if (item.type === 'video' && item.duration != null) {
    thumb.append(h('span', { class: 'dur', text: fmtDur(item.duration) }));
  }
  // RAW 角标
  if (item.ext && /^(raw|dng|arw|cr2|cr3|nef|orf|rw2)$/i.test(item.ext)) {
    thumb.append(h('span', { class: 'badge', text: 'RAW' }));
  }
  thumb.insertAdjacentHTML('beforeend', '<span class="check">' + SVG.check + '</span>');
  card.append(thumb);

  const author = item.authorName
    ? h('span', { class: 'author who', text: item.authorName })
    : h('span', { class: 'who', text: '未识别' });
  card.append(h('div', { class: 'meta' },
    h('div', { class: 'name', text: item.name, title: item.name }),
    h('div', { class: 'info' }, author, h('span', { text: fmtDate(item.mtime) }))));

  card.addEventListener('click', (e) => onCardClick(item.id, idx, e));
  card.addEventListener('dblclick', () => openPreview(idx)); // 双击进入应用内预览
  card.addEventListener('contextmenu', (e) => { e.preventDefault(); e.stopPropagation(); onItemContext(item, idx, e); });
  return card;
}

function renderEmpty() {
  const w = $('gridWrap'); w.innerHTML = '';
  scrollMemo = -1;
  const isFresh = state.sidebar && (state.sidebar.counts || {}).all === 0
    && state.activeKey === 'lib:all' && !state.filters.search;
  if (isFresh) {
    // 完全空库：引导添加
    const box = h('div', { class: 'empty-state', html: SVG.tray });
    box.append(h('div', { class: 't', text: '还没有媒体文件' }),
               h('div', { class: 's', text: '添加文件或扫描文件夹，开始整理你的媒体库' }));
    const btns = h('div', { class: 'btns' });
    const b1 = h('button', { class: 'big-btn primary', html: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>' });
    b1.append(h('span', { text: '添加文件' }));
    b1.addEventListener('click', () => pickScan('files'));
    const b2 = h('button', { class: 'big-btn', html: SVG.folder });
    b2.append(h('span', { text: '扫描文件夹' }));
    b2.addEventListener('click', () => pickScan('folder'));
    btns.append(b1, b2);
    box.append(btns);
    w.append(box);
  } else {
    const box = h('div', { class: 'empty-state', html: SVG.tray });
    box.append(h('div', { class: 't', text: '没有符合条件的媒体' }),
               h('div', { class: 's', text: '试试调整筛选条件或搜索关键词' }));
    w.append(box);
  }
}

/* ================= 10. 选择逻辑（单击/Ctrl/Shift/Esc） ================= */
let suppressClick = false; // 框选结束后的那次 click 需要吞掉
function onCardClick(id, idx, e) {
  if (suppressClick) return;
  if (e.shiftKey && state.anchor >= 0) {
    // Shift：按展开顺序范围选择
    const [a, b] = [Math.min(state.anchor, idx), Math.max(state.anchor, idx)];
    state.sel.clear();
    for (let i = a; i <= b; i++) state.sel.add(state.flat[i].id);
  } else if (e.ctrlKey || e.metaKey) {
    // Ctrl/Cmd：切换多选
    if (state.sel.has(id)) state.sel.delete(id); else state.sel.add(id);
    state.anchor = idx;
  } else {
    // 单击：单选
    state.sel.clear(); state.sel.add(id);
    state.anchor = idx;
  }
  syncSelDom();
  refreshBatchBar();
  updateCounts();
}
function syncSelDom() {
  document.querySelectorAll('#gridWrap .card, #gridWrap .lrow').forEach(c => {
    c.classList.toggle('sel', state.sel.has(Number(c.dataset.id)));
  });
}
function clearSelection() {
  if (!state.sel.size) return;
  state.sel.clear(); state.anchor = -1;
  syncSelDom(); refreshBatchBar(); updateCounts();
}
document.addEventListener('keydown', (e) => {
  const typing = e.target.closest('input, textarea, select');
  const key = e.key;
  if (key === 'Escape') {
    // 优先级：右键菜单 > 预览层 > 弹层 > 搜索框 > 清空选择
    if (ctxEl) closeCtx();
    else if (preview.idx >= 0) closePreview();
    else if (document.querySelector('.popover.show')) closePops();
    else if (document.activeElement === $('searchInput') && $('searchInput').value) {
      $('searchInput').value = '';
      $('searchBox').classList.remove('has-text');
      state.filters.search = '';
      runQuery();
    }
    else clearSelection();
    return;
  }
  if (key === 'f' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    $('searchInput').focus(); $('searchInput').select();
    return;
  }
  if (typing) return;
  // 右键菜单打开时，方向键 + Enter 直接操作菜单
  if (ctxEl) {
    if (key === 'ArrowDown') { e.preventDefault(); kbdMove(1); return; }
    if (key === 'ArrowUp') { e.preventDefault(); kbdMove(-1); return; }
    if (key === 'ArrowRight') { e.preventDefault(); kbdRight(); return; }
    if (key === 'ArrowLeft') { e.preventDefault(); kbdLeft(); return; }
    if (key === 'Enter') {
      e.preventDefault();
      if (kbdEl) kbdEl.click(); else kbdMove(1);
      return;
    }
  }
  if (key === 'a' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); selectAll(); return; }
  if (key === 'Enter' && preview.idx < 0 && state.sel.size === 1) {
    // Enter：预览当前选中项（与 Finder 一致）
    const idx = state.flat.findIndex(t => t.id === Array.from(state.sel)[0]);
    if (idx >= 0) { e.preventDefault(); openPreview(idx); return; }
  }
  if ((key === 'Delete' || (key === 'Backspace' && e.metaKey)) && state.sel.size) {
    e.preventDefault(); deleteFilesFlow(selIds()); return;
  }
  if (key === ' ' && preview.idx < 0 && !e.target.closest('video')) {
    // 空格快速预览当前项（单选时）
    if (state.sel.size === 1) {
      const id = Array.from(state.sel)[0];
      const idx = state.flat.findIndex(t => t.id === id);
      if (idx >= 0) { e.preventDefault(); openPreview(idx); return; }
    }
  }
  if (preview.idx >= 0 && !e.target.closest('video')) {
    if (key === 'ArrowLeft') { e.preventDefault(); navPreview(-1); }
    else if (key === 'ArrowRight') { e.preventDefault(); navPreview(1); }
  }
});

/* ================= 11. 批量操作条 ================= */
function refreshBatchBar() {
  const n = state.sel.size;
  $('batchBar').classList.toggle('show', n >= 1);
  $('selCount').textContent = '已选 ' + n + ' 项';
  if (n < 1) { $('tagPop').classList.remove('show'); $('authorPop').classList.remove('show'); }
}
function selIds() { return Array.from(state.sel); }
function selItems() { return state.flat.filter(it => state.sel.has(it.id)); }

/* ================= 12. 弹层：打标签 / 设作者 ================= */
function closePops() {
  document.querySelectorAll('.popover.show').forEach(p => p.classList.remove('show'));
  const b = $('tagBtn');
  if (b) b.classList.remove('open');
}
function placePopAboveBatch(pop) {
  pop.style.left = '50%'; pop.style.top = 'auto'; pop.style.bottom = '96px';
  pop.style.transform = 'translateX(-50%)';
  pop.style.transformOrigin = 'bottom center';
}
function togglePop(pop) {
  const was = pop.classList.contains('show');
  closePops();
  if (!was) { placePopAboveBatch(pop); pop.classList.add('show'); }
  return !was;
}
/* 点击弹层外部关闭 */
document.addEventListener('mousedown', (e) => {
  if (e.target.closest('.popover') || e.target.closest('#batchBar') ||
      e.target.closest('#wsSwitch') || e.target.closest('#tagBar')) return;
  closePops();
});

/* ---- 打标签弹层（三态：全部已有 ✓ / 部分 — / 无；点一下即增/删） ---- */
function tagStateOf(t, items) {
  if (!items.length) return 'none';
  const n = items.filter(it => (it.tags || []).some(x => x.id === t.id)).length;
  if (!n) return 'none';
  return n === items.length ? 'all' : 'part';
}
function renderTagPop() {
  const list = $('tagPopList'); list.innerHTML = '';
  const tags = (state.sidebar && state.sidebar.tags) || [];
  const items = selItems();
  if (!tags.length) list.append(h('div', { class: 'pop-item disabled', text: '尚无标签，可在下方新建' }));
  $('tagPopTitle').textContent = items.length
    ? '点击标签：空=添加，✓=移除（' + items.length + ' 项已选）'
    : '点击标签即应用到所选';
  for (const t of tags) {
    const st = tagStateOf(t, items);
    const dot = h('span', { class: 'dot-tag' });
    dot.style.background = t.color || 'var(--accent)';
    const row = h('div', { class: 'pop-item' + (st === 'all' ? ' on' : '') });
    row.append(dot, h('span', { text: t.name }));
    if (st === 'all') row.append(h('span', { class: 'tag-state', html: SVG.check }));
    else if (st === 'part') row.append(h('span', { class: 'tag-state dash' }));
    else row.append(h('span', { class: 'cnt', text: t.count }));
    row.title = st === 'all' ? '点击从所选移除该标签'
      : st === 'part' ? '部分已含：点击为全部添加' : '点击添加到所选';
    row.addEventListener('click', () => toggleTagOnSel(t));
    list.append(row);
  }
}
async function toggleTagOnSel(t) {
  const items = selItems(); if (!items.length) return;
  const mode = tagStateOf(t, items) === 'all' ? 'remove' : 'add';
  const r = await api('assignTags', selIds(), [t.id], mode);
  if (r && r.ok) {
    for (const it of items) {
      it.tags = it.tags || [];
      if (mode === 'add') {
        if (!it.tags.some(x => x.id === t.id)) it.tags.push({ id: t.id, name: t.name, color: t.color });
      } else it.tags = it.tags.filter(x => x.id !== t.id);
    }
    toast(mode === 'add' ? '已应用标签「' + t.name + '」' : '已移除标签「' + t.name + '」');
    renderTagPop();
    updateTagsInDom(selIds());   // 只改受影响的行，不整页重绘
    refreshSidebar();
  }
}
async function createTagFromPop() {
  const name = $('tagNewName').value.trim();
  if (!name) { toast('请输入标签名称', 'err'); return; }
  const color = $('tagNewColor').value;
  const t = await api('createTag', name, color);
  if (t && t.id != null) {
    $('tagNewName').value = '';
    await refreshSidebar();       // 新标签立刻出现在侧边栏与弹层
    const ids = selIds();
    if (ids.length) {
      const r = await api('assignTags', ids, [t.id], 'add');
      if (r && r.ok) {
        toast('已创建并应用标签「' + t.name + '」');
        for (const it of selItems()) {
          it.tags = it.tags || [];
          if (!it.tags.some(x => x.id === t.id)) it.tags.push({ id: t.id, name: t.name, color: t.color });
        }
      }
    } else toast('标签「' + t.name + '」已创建');
    renderTagPop();
  }
}

/* ---- 设作者弹层 ---- */
function renderAuthorPop() {
  const list = $('authorPopList'); list.innerHTML = '';
  const authors = (state.sidebar && state.sidebar.authors) || [];
  if (!authors.length) list.append(h('div', { class: 'pop-item disabled', text: '尚无作者' }));
  for (const a of authors) {
    const row = h('div', { class: 'pop-item', html: SVG.person });
    row.append(h('span', { text: a.name }), h('span', { class: 'cnt', text: a.count }));
    row.addEventListener('click', () => applyAuthor(a.id, null, a.name));
    list.append(row);
  }
}
async function applyAuthor(authorId, newName, label) {
  const ids = selIds(); if (!ids.length) return;
  const r = await api('assignAuthor', ids, authorId, newName);
  if (r && r.ok) {
    toast(authorId == null && !newName ? '已清除作者标记' : '已设置作者「' + (label || newName) + '」');
    closePops();
  }
}
async function createAuthorFromPop() {
  const name = $('authorNewName').value.trim();
  if (!name) { toast('请输入作者名称', 'err'); return; }
  $('authorNewName').value = '';
  await applyAuthor(null, name, name); // newName 非空：后端先建作者再指派
}

/* ================= 13. 扫描与状态栏进度 ================= */
async function pickScan(mode) {
  const r = await api('pickAndScan', mode);
  if (r && r.started) toast('扫描已开始');
  else if (r && r.reason === 'busy') toast('已有扫描任务进行中，请稍候', 'err');
}
let lastScanRunning = false;
function renderScan(st) {
  const box = $('scanBox'), dot = $('statusDot');
  const wasRunning = lastScanRunning;
  lastScanRunning = !!st.running;
  state.scanning = !!st.running;
  dot.classList.toggle('busy', !!st.running);
  if (st.running) {
    box.classList.remove('hide');
    $('scanText').textContent = (st.phase || '扫描中') + (st.current ? ' · ' + st.current : '');
    $('scanText').title = st.current || '';
    const bar = $('scanBar');
    if (!st.total) {
      // 枚举阶段（总数未知）：不确定态进度条
      bar.classList.add('indet');
      $('scanNum').textContent = '';
    } else {
      bar.classList.remove('indet');
      const pct = Math.min(100, Math.round((st.done || 0) / st.total * 100));
      bar.style.width = pct + '%';
      $('scanNum').textContent = (st.done || 0) + ' / ' + st.total;
    }
  } else if (st.newFiles) {
    // 完成提示短暂显示后淡出
    box.classList.remove('hide');
    $('scanBar').classList.remove('indet');
    $('scanText').textContent = '完成，新增 ' + st.newFiles + ' 项';
    $('scanBar').style.width = '100%';
    $('scanNum').textContent = '';
    setTimeout(() => { if (!state.scanning) box.classList.add('hide'); }, 3000);
  } else {
    box.classList.add('hide');
    $('scanBar').classList.remove('indet');
  }
  // 扫描刚结束且确有新增：把视图拉回「全部媒体」，避免当前筛选把新文件挡住
  if (wasRunning && !st.running && st.newFiles > 0) onScanFinished(st);
}
function onScanFinished(st) {
  const target = st.wsId;
  const cur = state.boot ? state.boot.currentWorkspaceId : null;
  if (target != null && cur != null && Number(target) !== Number(cur)) {
    // 扫描发生在别的工作区（例如扫描途中切换了工作区）
    toast('新增 ' + st.newFiles + ' 项已入库到「' + (st.wsName || '其他') + '」工作区', 'err');
    refreshWsCounts();
    return;
  }
  state.activeKey = 'lib:all';
  state.filters = Object.assign(baseFilters(), { search: '', searchField: state.filters.searchField });
  const si = $('searchInput'); if (si) si.value = '';
  const sb = $('searchBox'); if (sb) sb.classList.remove('has-text');
  markActiveSideItem();
  refreshAll();
  refreshWsCounts();
  toast('扫描完成，新增 ' + st.newFiles + ' 项');
}

/* ================= 14. 备份（立即备份 + 历史/恢复弹层） ================= */
/* name 形如「工作区名::文件名」，展示拆为「工作区 · 文件名」 */
function fmtBackupName(name) {
  const i = String(name || '').indexOf('::');
  return i >= 0 ? name.slice(0, i) + ' · ' + name.slice(i + 2) : String(name || '');
}
const BACKUP_KIND_TEXT = { manual: '手动', auto: '自动', before_restore: '恢复前', legacy: '自动' };
async function doBackup() {
  const r = await api('backupNow');
  if (r && r.ok) toast('备份完成：' + fmtBackupName(r.name) + '（' + fmtSize(r.size) + '）');
}
async function openBackupPop() {
  closePops();
  const pop = $('backupPop');
  pop.style.left = 'auto'; pop.style.right = '12px';
  pop.style.top = 'auto'; pop.style.bottom = '40px';
  pop.style.transform = 'none'; pop.style.transformOrigin = 'bottom right';
  pop.classList.add('show');
  const list = $('backupList');
  list.innerHTML = '';
  list.append(h('div', { class: 'pop-item disabled', text: '读取中…' }));
  let arr;
  try { arr = await api('listBackups') || []; } catch (_) { return; }
  if (!pop.classList.contains('show')) return; // 等待期间已被关闭
  list.innerHTML = '';
  if (!arr.length) { list.append(h('div', { class: 'pop-item disabled', text: '暂无备份' })); return; }
  for (const b of arr) {
    const ws = String(b.name || '').split('::')[0] || '';
    const row = h('div', { class: 'pop-item backup-row', title: fmtBackupName(b.name) + (b.createdText ? '\n创建时间：' + b.createdText : '') });
    row.append(h('span', { class: 'ico-backup', html: '<svg viewBox="0 0 24 24"><path d="M4 5a8 8 0 1 1-1 9"/><path d="M4 5v5h5"/></svg>' }));
    row.append(h('span', { class: 'b-badge' + (b.kind === 'manual' ? ' manual' : (b.kind === 'before_restore' ? ' warn' : '')), text: BACKUP_KIND_TEXT[b.kind] || '自动' }));
    row.append(h('span', { class: 'b-name', text: ws }),
      h('span', { class: 'b-time', text: b.createdText || '' }),
      h('span', { class: 'cnt', text: fmtSize(b.size) }));
    const rb = h('button', { class: 'restore', text: '恢复' });
    rb.addEventListener('click', (e) => { e.stopPropagation(); restoreBackup(b); });
    row.append(rb);
    list.append(row);
  }
}
async function restoreBackup(b) {
  const ok = await askConfirm({
    title: '恢复这个备份？',
    msg: '当前工作区「' + (fmtBackupName(b.name).split(' · ')[0] || '') + '」的数据将被该备份覆盖，恢复后无法撤销。',
    detail: (BACKUP_KIND_TEXT[b.kind] ? BACKUP_KIND_TEXT[b.kind] + '备份 · ' : '') + (b.createdText || fmtBackupName(b.name)),
    confirm: '恢复',
  });
  if (!ok) return;
  const r = await api('restoreBackup', b.name); // 注意：回传原始 name（含工作区前缀）
  if (r && r.ok) {
    closePops();
    toast('备份已恢复');
    await refreshAll();
  } else toast('恢复失败', 'err');
}

/* ================= 15. 全屏预览层（lightbox） ================= */
const preview = { idx: -1 }; // 当前预览项在 state.flat 中的下标；-1 = 未打开

function openPreview(idx) {
  if (idx < 0 || idx >= state.flat.length) return;
  closePops();
  preview.idx = idx;
  renderPreviewMedia(true);
  const lb = $('lightbox');
  lb.classList.remove('closing');
  lb.classList.add('show');
  document.body.classList.add('lb-open'); // 锁定背景滚动
}
function closePreview() {
  const lb = $('lightbox');
  if (!lb.classList.contains('show')) return;
  preview.idx = -1;
  lb.classList.add('closing');           // backdrop 淡出
  document.body.classList.remove('lb-open');
  setTimeout(() => {
    if (preview.idx >= 0) return;        // 关闭动画期间又被打开，放弃清理
    lb.classList.remove('show', 'closing');
    $('lbStage').innerHTML = '';         // 移除 video 即停止播放与加载
  }, 240);
}
function navPreview(dir) {
  if (preview.idx < 0 || !state.flat.length) return;
  preview.idx = (preview.idx + dir + state.flat.length) % state.flat.length; // 按网格顺序循环
  renderPreviewMedia(false);
}
function renderPreviewMedia(first) {
  const item = state.flat[preview.idx];
  if (!item) return;
  // 顶栏：文件名 + 作者 + 尺寸/时长/大小等元信息
  $('lbName').textContent = item.name;
  $('lbName').title = item.name;
  const meta = [item.authorName || '未识别'];
  if (item.width && item.height) meta.push(item.width + '×' + item.height);
  if (item.type === 'video' && item.duration != null) meta.push(fmtDur(item.duration));
  if (item.ext) meta.push(String(item.ext).toUpperCase());
  if (item.size != null) meta.push(fmtSize(item.size));
  const d = fmtDate(item.mtime); if (d) meta.push(d);
  $('lbMeta').textContent = meta.join(' · ');
  $('lbMeta').title = $('lbMeta').textContent;
  // 舞台内容：切换时旧媒体淡出 → 新媒体弹簧缩放淡入
  const stage = $('lbStage');
  const build = () => {
    stage.innerHTML = '';
    const el = (item.type === 'video')
      ? h('video', { class: 'lb-media', src: item.mediaUrl || '', controls: '', autoplay: '', playsinline: '' })
      : h('img', { class: 'lb-media', src: item.mediaUrl || '', alt: item.name, draggable: 'false' });
    stage.append(el);
  };
  if (first || !stage.firstChild) { build(); return; }
  const old = stage.firstChild;
  if (old.pause) old.pause();
  old.style.transition = 'opacity .12s ease-out';
  old.style.opacity = '0';
  setTimeout(build, 120);
}

/* ================= 15.5 视图切换（网格 / 列表）与缩略图尺寸 ================= */
function applyView(v, persist = true) {
  state.view = (v === 'list') ? 'list' : 'grid';
  document.body.classList.toggle('view-list', state.view === 'list');
  const g = $('viewGrid'), l = $('viewList');
  if (g) g.classList.toggle('on', state.view === 'grid');
  if (l) l.classList.toggle('on', state.view === 'list');
  if (persist) { try { localStorage.setItem('mm.view', state.view); } catch (_) {} }
  if (state.sidebar) renderGrid(); // 只重渲染，不重新查询
}
function applyCardW(px, persist = true) {
  const w = Math.max(140, Math.min(360, Number(px) || 210));
  document.documentElement.style.setProperty('--card-w', w + 'px');
  const sl = $('zoomSlider');
  if (sl && Number(sl.value) !== w) sl.value = w;
  if (persist) { try { localStorage.setItem('mm.cardW', String(w)); } catch (_) {} }
}

/* ================= 15.6 左键框选（rubber band） ================= */
const band = { on: false, x0: 0, y0: 0, base: null, el: null, moved: false, raf: 0, vy: 0, lastX: 0, lastY: 0 };
const BAND_SKIP = '.card, .lrow, .group-title, .lhead, .lheader, button, input, select, img, .empty-state, .skel, a';

function bandTargetEls() {
  return document.querySelectorAll('#gridWrap .card, #gridWrap .lrow');
}

function bandUpdate(cx, cy) {
  const l = Math.min(band.x0, cx), t = Math.min(band.y0, cy);
  const w = Math.abs(cx - band.x0), hgt = Math.abs(cy - band.y0);
  if (band.el) {
    band.el.style.left = l + 'px';
    band.el.style.top = t + 'px';
    band.el.style.width = w + 'px';
    band.el.style.height = hgt + 'px';
  }
  const sel = new Set(band.base);
  const r2 = l + w, b2 = t + hgt;
  for (const el of bandTargetEls()) {
    const b = el.getBoundingClientRect();
    if (b.width && b.right > l && b.left < r2 && b.bottom > t && b.top < b2) {
      sel.add(Number(el.dataset.id));
    }
  }
  state.sel = sel;
  syncSelDom();
  refreshBatchBar();
  updateCounts();
}

function bandStop() {
  if (!band.on) return;
  band.on = false;
  cancelAnimationFrame(band.raf);
  document.body.classList.remove('banding');
  if (band.el) { band.el.remove(); band.el = null; }
  // 吞掉框选结束瞬间触发的那次 click
  suppressClick = true;
  setTimeout(() => { suppressClick = false; }, 0);
}

function startBand(e) {
  if (e.button !== 0) return;
  const wrap = $('gridWrap');
  if (!wrap || !wrap.contains(e.target)) return;
  if (e.target.closest(BAND_SKIP)) return;
  band.on = true;
  band.moved = false;
  band.x0 = e.clientX; band.y0 = e.clientY;
  band.lastX = e.clientX; band.lastY = e.clientY;
  band.base = new Set((e.ctrlKey || e.metaKey) ? state.sel : []);
  band.el = h('div', { class: 'rubber' });
  document.body.append(band.el);
  document.body.classList.add('banding');
  if (!e.ctrlKey && !e.metaKey) { state.sel.clear(); syncSelDom(); refreshBatchBar(); updateCounts(); }
  e.preventDefault();

  const step = () => {
    if (!band.on) return;
    if (band.vy) {
      wrap.scrollTop += band.vy;
      bandUpdate(band.lastX, band.lastY);
    }
    band.raf = requestAnimationFrame(step);
  };
  band.raf = requestAnimationFrame(step);
}

document.addEventListener('pointerdown', (e) => {
  if (e.target.closest('.popover')) return;
  startBand(e);
});
document.addEventListener('pointermove', (e) => {
  if (!band.on) return;
  if (!band.moved && Math.abs(e.clientX - band.x0) + Math.abs(e.clientY - band.y0) > 3) band.moved = true;
  band.lastX = e.clientX; band.lastY = e.clientY;
  const wrap = $('gridWrap');
  if (wrap && band.moved) {
    const r = wrap.getBoundingClientRect();
    const EDGE = 36, SPEED = 14;
    if (e.clientY > r.bottom - EDGE) band.vy = SPEED;
    else if (e.clientY < r.top + EDGE) band.vy = -SPEED;
    else band.vy = 0;
  }
  bandUpdate(e.clientX, e.clientY);
});
document.addEventListener('pointerup', () => bandStop());
document.addEventListener('pointercancel', () => bandStop());
window.addEventListener('blur', () => bandStop());

/* ================= 15.7 扫描源管理（重扫指定一个或多个目录） ================= */
async function openSrcPop() {
  closePops();
  const pop = $('srcPop'), anchor = $('btnRescan').getBoundingClientRect();
  pop.style.left = Math.max(12, Math.min(window.innerWidth - 400, anchor.right - 360)) + 'px';
  pop.style.top = (anchor.bottom + 8) + 'px';
  pop.style.bottom = 'auto'; pop.style.transform = 'none'; pop.style.transformOrigin = 'top right';
  pop.classList.add('show');
  await renderSrcList();
}
async function renderSrcList() {
  const list = $('srcList');
  list.innerHTML = '';
  list.append(h('div', { class: 'pop-item disabled', text: '读取中…' }));
  let res;
  try { res = await api('listSources'); } catch (_) { return; }
  const arr = ((res && res.sources) || []).slice();
  list.innerHTML = '';
  if (!arr.length) {
    list.append(h('div', { class: 'pop-item disabled', text: '还没有扫描源，点下方「添加文件夹…」' }));
    return;
  }
  for (const s of arr) {
    const row = h('div', { class: 'pop-item src-row' });
    const cb = h('input', { type: 'checkbox' });
    cb.checked = true; cb.dataset.path = s.path;
    const name = h('span', { class: 'src-name', text: s.path, title: s.path });
    const del = h('button', { class: 'src-del', title: '从扫描源移除（已入库文件保留）',
      html: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>' });
    row.append(cb, name, h('span', { class: 'cnt', text: s.count }));
    row.append(del);
    row.addEventListener('click', (e) => {
      if (e.target === cb) return;
      if (del.contains(e.target)) return;
      cb.checked = !cb.checked;
    });
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      await api('removeSource', s.id);
      toast('已移除扫描源');
      await renderSrcList();
      await refreshSidebar();
    });
    list.append(row);
  }
}
function srcCheckedPaths() {
  return Array.from(document.querySelectorAll('#srcList input[type="checkbox"]'))
    .filter(cb => cb.checked).map(cb => cb.dataset.path);
}
async function rescanSelected() {
  const paths = srcCheckedPaths();
  if (!paths.length) { toast('请先勾选要重扫的目录', 'err'); return; }
  const r = await api('rescan', paths);
  if (r && r.started) { toast('已开始重扫 ' + paths.length + ' 个目录'); closePops(); }
  else toast('没有可扫描的目录', 'err');
}
async function addSourceFromPop() {
  const r = await api('addSource');
  if (r && r.added) { toast('已添加扫描源'); await renderSrcList(); }
}

/* ================= 16. 静态事件绑定 ================= */
/* 侧边栏开合（<1000px 抽屉模式时联动遮罩） */
function toggleSidebar(force) {
  const sb = $('sidebar');
  const hide = (force !== undefined) ? force : !sb.classList.contains('hidden');
  sb.classList.toggle('hidden', hide);
  $('sideMask').classList.toggle('show', !hide && window.innerWidth <= 1000);
}

function bindStaticEvents() {
  /* 窗口控制（frameless 红绿灯） */
  $('winClose').addEventListener('click', () => {
    if (MM_BROWSER) { try { window.close(); } catch (_) {} return; }
    api('winClose');
  });
  $('winMin').addEventListener('click', () => { if (!MM_BROWSER) api('winMinimize'); });
  $('winMax').addEventListener('click', async () => {
    const r = await api('winToggleMaximize').catch(() => null);
    const mx = !!(r && r.maximized);
    $('winMax').classList.toggle('restore', mx);
    const sp = $('winMax').querySelector('span');
    if (sp) sp.textContent = mx ? '❐' : '＋';
  });

  /* 侧边栏开合 + 抽屉遮罩 */
  $('sideToggle').addEventListener('click', () => toggleSidebar());
  $('sideMask').addEventListener('click', () => toggleSidebar(true));
  // 窄屏默认收起侧边栏；跨过断点时校正遮罩
  if (window.innerWidth <= 1000) toggleSidebar(true);
  window.addEventListener('resize', () => {
    if (window.innerWidth > 1000) $('sideMask').classList.remove('show');
    else $('sideMask').classList.toggle('show', !$('sidebar').classList.contains('hidden'));
  });

  /* 主题切换（切换 data-theme 并持久化） */
  $('themeToggle').addEventListener('click', () => {
    const root = document.documentElement;
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    api('setTheme', next).catch(() => {});
  });

  /* 工作区 */
  $('wsAdd').addEventListener('click', () => openWsPop('create'));
  $('wsCreateBtn').addEventListener('click', submitWsPop);
  $('wsName').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitWsPop(); });

  /* 库 / 待整理 静态项 */
  document.querySelectorAll('[data-lib]').forEach(el => {
    el.addEventListener('click', () => {
      const t = el.dataset.lib;
      applyFilter('lib:' + t, t === 'all' ? {} : { type: t });
    });
  });
  document.querySelectorAll('[data-special]').forEach(el => {
    el.addEventListener('click', () => applyFilter('special:' + el.dataset.special, { special: el.dataset.special }));
  });
  // 右键菜单：库 / 待整理 / 媒体区空白
  document.querySelectorAll('[data-lib]').forEach(el => {
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); libMenu(el.dataset.lib, e.clientX, e.clientY); });
  });
  document.querySelectorAll('[data-special]').forEach(el => {
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); specialMenu(el.dataset.special, e.clientX, e.clientY); });
  });
  $('gridWrap').addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (e.target.closest('.card, .lrow, button, input')) return;
    blankMenu(e.clientX, e.clientY);
  });
  // 点击空白区域取消选择（与 Finder 一致）
  $('gridWrap').addEventListener('click', (e) => {
    if (suppressClick) return;
    if (e.target.closest('.card, .lrow, button, input, .group-title, .lhead')) return;
    clearSelection();
  });

  /* 搜索（250ms 防抖）+ 搜索范围切换 */
  let deb = null;
  $('searchInput').addEventListener('input', (e) => {
    $('searchBox').classList.toggle('has-text', !!e.target.value);
    clearTimeout(deb);
    deb = setTimeout(() => {
      state.filters.search = e.target.value.trim();
      runQuery(); // 搜索保留当前筛选，仅追加 search 条件
    }, 250);
  });
  $('searchClear').addEventListener('click', () => {
    $('searchInput').value = '';
    $('searchBox').classList.remove('has-text');
    state.filters.search = '';
    runQuery();
  });
  $('searchField').addEventListener('click', (e) => {
    e.stopPropagation();
    openScopeMenu();
  });

  /* 标签面板（标题栏中央，替代原来的侧栏标签区） */
  $('tagBtn').addEventListener('click', (e) => { e.stopPropagation(); openTagPanel(); });
  $('tagFilter').addEventListener('input', renderTagPanel);
  $('tpUntagged').addEventListener('click', () => {
    applyFilter('special:untagged', { special: 'untagged' });
  });
  $('tpClear').addEventListener('click', () => { applyFilter('lib:all', {}); });
  $('tpNew').addEventListener('click', createTagOnly);

  /* 视图切换：网格 / 列表（只重渲染，不重新查询） */
  $('viewGrid').addEventListener('click', () => applyView('grid'));
  $('viewList').addEventListener('click', () => applyView('list'));

  /* 缩略图尺寸滑块（实时改变 --card-w，纯 CSS 重排） */
  $('zoomSlider').addEventListener('input', (e) => applyCardW(e.target.value));

  /* 无边框窗口缩放热区：交给系统原生缩放 */
  document.querySelectorAll('.rz').forEach(el => {
    el.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      api('winStartResize', el.dataset.edge).catch(() => {});
    });
  });

  /* 双击标题栏空白处 = 最大化 / 还原（Windows 惯例） */
  $('titlebar').addEventListener('dblclick', (e) => {
    if (e.target.closest('button, input, select, .tb-group, .search, .traffic')) return;
    $('winMax').click();
  });

  /* 扫描工具组 */
  $('btnAddFiles').addEventListener('click', () => pickScan('files'));
  $('btnScanFolder').addEventListener('click', () => pickScan('folder'));
  $('btnRescan').addEventListener('click', openSrcPop);
  $('srcAdd').addEventListener('click', addSourceFromPop);
  $('srcRescan').addEventListener('click', rescanSelected);
  $('srcRescanAll').addEventListener('click', async () => {
    const r = await api('rescan');
    if (r && r.started) { toast('全部扫描源重扫已开始'); closePops(); }
    else toast('还没有扫描源', 'err');
  });

  /* 分组方式按钮 */
  $('groupBtn').addEventListener('click', openGroupPop);
  $('groupBtn').addEventListener('contextmenu', (e) => { e.preventDefault(); openGroupPop(); });

  /* 批量删除 */
  $('batchDel').addEventListener('click', () => deleteFilesFlow(selIds()));

  /* 备份：左键立即备份，右键历史/恢复弹层 */
  $('btnBackup').addEventListener('click', doBackup);
  $('btnBackup').addEventListener('contextmenu', (e) => { e.preventDefault(); openBackupPop(); });
  $('backupNowItem').addEventListener('click', () => { closePops(); doBackup(); });

  /* 预览层：关闭 / 左右切换 / 系统中打开 / backdrop 点击关闭 */
  $('lbClose').addEventListener('click', closePreview);
  $('lbPrev').addEventListener('click', () => navPreview(-1));
  $('lbNext').addEventListener('click', () => navPreview(1));
  $('lbOpen').addEventListener('click', () => {
    const item = state.flat[preview.idx];
    if (item) api('openFile', item.id).catch(() => {});
  });
  $('lbDelete').addEventListener('click', () => {
    const item = state.flat[preview.idx];
    if (item) deleteFilesFlow([item.id], true);
  });
  $('lightbox').addEventListener('click', (e) => {
    // 仅点击媒体以外的 backdrop / 舞台空白区域才关闭
    if (e.target.id === 'lbBackdrop' || e.target.id === 'lbStage') closePreview();
  });

  /* 批量操作条 */
  $('batchTag').addEventListener('click', () => { if (togglePop($('tagPop'))) renderTagPop(); });
  $('batchAuthor').addEventListener('click', () => { if (togglePop($('authorPop'))) renderAuthorPop(); });
  $('batchOpen').addEventListener('click', () => {
    for (const id of selIds()) api('openFile', id).catch(() => {});
  });
  $('batchClear').addEventListener('click', clearSelection);

  /* 打标签弹层：新建 */
  $('tagNewBtn').addEventListener('click', createTagFromPop);
  $('tagNewName').addEventListener('keydown', (e) => { if (e.key === 'Enter') createTagFromPop(); });

  /* 设作者弹层：新建并应用 / 清除 */
  $('authorNewBtn').addEventListener('click', createAuthorFromPop);
  $('authorNewName').addEventListener('keydown', (e) => { if (e.key === 'Enter') createAuthorFromPop(); });
  $('authorClear').addEventListener('click', () => applyAuthor(null, null, null));
}

/* ================= 17. 图标补充（右键菜单用，SF Symbols 风格细线） ================= */
const IC = {
  open: '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4 10 14"/><path d="M20 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5"/></svg>',
  eye: '<svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.6"/></svg>',
  reveal: '<svg viewBox="0 0 24 24"><path d="M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="12" cy="14" r="2.4"/><path d="M12.4 16.2 14 18"/></svg>',
  tag: '<svg viewBox="0 0 24 24"><path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.2"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M15 5.6A2.6 2.6 0 0 0 12.4 3H5.6A2.6 2.6 0 0 0 3 5.6v6.8A2.6 2.6 0 0 0 5.6 15"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6M14 11v6"/></svg>',
  minus: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  grid: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M20 11A8 8 0 1 0 18.9 14"/><path d="M20 5v6h-6"/></svg>',
  layers: '<svg viewBox="0 0 24 24"><path d="M12 3 3 8l9 5 9-5z"/><path d="M3 14l9 5 9-5"/></svg>',
  image: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="8.5" cy="10" r="1.6"/><path d="M21 15.5 16 10.5 7 19"/></svg>',
  video: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10 9.5v5l4.5-2.5z"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.6v.4"/></svg>',
  pencil: '<svg viewBox="0 0 24 24"><path d="M4 20h4l10-10a2.1 2.1 0 0 0-3-3L5 17z"/></svg>',
  selectAll: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 12.5 11 15.5 16.5 9"/></svg>',
  square: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/></svg>',
  play: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M10 8.5l6 3.5-6 3.5z"/></svg>',
  size: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2.5"/><path d="M3 9V4.5A1.5 1.5 0 0 1 4.5 3H9M21 15v4.5a1.5 1.5 0 0 1-1.5 1.5H15"/></svg>',
};

/* ================= 18. 确认 / 输入弹层（替代 window.confirm / prompt） ================= */
function showModal(build) {
  return new Promise((resolve) => {
    const box = $('modalBox'), wrap = $('modalWrap');
    box.innerHTML = '';
    wrap.classList.add('show');
    let closed = false;
    const done = (v) => {
      if (closed) return;
      closed = true;
      wrap.classList.remove('show');
      box.innerHTML = '';
      document.removeEventListener('keydown', onKey, true);
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); }
      else if (e.key === 'Enter' && !e.isComposing) {
        const btn = box.querySelector('.m-btn.primary, .m-btn.danger');
        if (btn) { e.preventDefault(); e.stopPropagation(); btn.click(); }
      }
    };
    document.addEventListener('keydown', onKey, true);
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) done(null); }, { once: true });
    build(box, done);
  });
}
async function askConfirm(o) {
  const v = await showModal((box, done) => {
    box.append(h('h3', { text: o.title }));
    if (o.msg) box.append(h('p', { text: o.msg }));
    if (o.detail) box.append(h('div', { class: 'detail', text: o.detail }));
    const acts = h('div', { class: 'm-actions' });
    const cancel = h('button', { class: 'm-btn', text: o.cancel || '取消' });
    cancel.addEventListener('click', () => done(false));
    const ok = h('button', { class: 'm-btn ' + (o.danger ? 'danger' : 'primary'), text: o.confirm || '确定' });
    ok.addEventListener('click', () => done(true));
    acts.append(cancel, ok);
    box.append(acts);
    setTimeout(() => ok.focus(), 40);
  });
  return v === true;
}
async function askText(o) {
  const v = await showModal((box, done) => {
    box.append(h('h3', { text: o.title }));
    if (o.msg) box.append(h('p', { text: o.msg }));
    const inp = h('input', { type: 'text', placeholder: o.placeholder || '', maxlength: '60' });
    inp.value = o.value || '';
    box.append(inp);
    const acts = h('div', { class: 'm-actions' });
    const cancel = h('button', { class: 'm-btn', text: '取消' });
    cancel.addEventListener('click', () => done(null));
    const ok = h('button', { class: 'm-btn primary', text: o.confirm || '确定' });
    ok.addEventListener('click', () => done(inp.value.trim() || null));
    acts.append(cancel, ok);
    box.append(acts);
    setTimeout(() => { inp.focus(); inp.select(); }, 40);
  });
  return v;
}

/* ================= 19. 右键菜单引擎（macOS 风格 + 多级子菜单） =================
   子菜单的交互要点（很容易踩坑）：
   1) 子菜单内部的普通项绝不能绑定「移入就收起」——否则鼠标刚进子菜单它就自我销毁；
   2) 鼠标从父项斜向滑到子菜单需要时间，收起必须延迟，并在进入菜单/子菜单时取消；
   3) 定位用 offsetWidth/offsetHeight（布局尺寸），不受入场动画的 scale 影响。
   ================================================================= */
let ctxEl = null;    // 顶层菜单
let subEl = null;    // 当前展开的子菜单
let subRow = null;   // 子菜单对应的父项
let subTimer = 0;    // 子菜单延迟收起定时器
let kbdEl = null;    // 键盘导航高亮项

function clearSubTimer() { if (subTimer) { clearTimeout(subTimer); subTimer = 0; } }
/* 延迟收起：给鼠标留出斜向移动的时间窗口 */
function queueSubClose(ms = 340) {
  if (!subEl) return;
  clearSubTimer();
  subTimer = setTimeout(() => { subTimer = 0; closeSub(); }, ms);
}
function closeSub() {
  clearSubTimer();
  if (subEl) { subEl.remove(); subEl = null; }
  if (subRow) { subRow.classList.remove('hot'); subRow = null; }
}
function closeCtx() {
  closeSub();
  kbdSet(null);
  if (ctxEl) { ctxEl.remove(); ctxEl = null; }
  document.querySelectorAll('.sf-btn.sf-open').forEach(n => n.classList.remove('sf-open'));
}

function ctxRow(it) {
  const row = h('div', {
    class: 'ctx-item' + (it.danger ? ' danger' : '') + (it.disabled ? ' disabled' : ''),
  });
  if (it.head) { row.className = 'ctx-head'; row.textContent = it.label; return row; }
  if (it.icon) row.insertAdjacentHTML('beforeend', it.icon);
  else row.append(h('span', { class: 'ctx-ico' }));
  row.append(h('span', { class: 'ctx-label', text: it.label }));
  if (it.checked) row.append(h('span', { class: 'ctx-arrow', html: SVG.check }));
  else if (it.sub) row.append(h('span', { class: 'ctx-arrow', html: '<svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>' }));
  else if (it.shortcut) row.append(h('span', { class: 'ctx-key', text: it.shortcut }));
  return row;
}

function buildCtx(items, isSub) {
  const m = h('div', { class: 'ctx-menu glass' + (isSub ? ' sub' : '') });
  for (const it of items) {
    if (!it) continue;
    if (it.sep) { m.append(h('div', { class: 'ctx-sep' })); continue; }
    const row = ctxRow(it);
    if (it.head) { m.append(row); continue; }
    row.dataset.act = '1';
    if (!it.disabled) {
      if (it.sub && it.sub.length) {
        row.dataset.sub = '1';
        row.addEventListener('mouseenter', () => openSub(row, it.sub));
        row.addEventListener('click', (ev) => { ev.stopPropagation(); openSub(row, it.sub); });
      } else {
        // 只有顶层菜单里的普通项，悬停时才延时收起子菜单
        if (!isSub) row.addEventListener('mouseenter', () => queueSubClose());
        row.addEventListener('click', (ev) => {
          ev.stopPropagation();
          closeCtx();
          try { it.onClick && it.onClick(); } catch (e) { console.warn(e); }
        });
      }
    }
    m.append(row);
  }
  // 指针进入菜单本身：取消待执行收起（斜向移动落进子菜单区域时生效）
  m.addEventListener('mouseenter', clearSubTimer);
  m.addEventListener('mouseover', () => kbdSet(null)); // 一旦用鼠标，清掉键盘高亮
  if (!isSub) m.addEventListener('mouseleave', () => queueSubClose(420));
  else m.addEventListener('mouseleave', () => queueSubClose(240));
  return m;
}

/* 把浮层贴着 row 的右侧放置；右侧空间不够就翻到左侧 */
function placeBeside(el, row, gap) {
  const r = row.getBoundingClientRect();
  const w = el.offsetWidth, ht = el.offsetHeight;
  const g = (gap == null ? 3 : gap);
  let left = r.right - g;
  let flipped = false;
  if (left + w > window.innerWidth - 8) { left = r.left - w + g; flipped = true; }
  let top = r.top - 6;
  if (top + ht > window.innerHeight - 8) top = window.innerHeight - ht - 8;
  el.style.left = Math.max(8, left) + 'px';
  el.style.top = Math.max(8, top) + 'px';
  el.classList.toggle('flip-l', flipped);   // 翻到左侧时过渡带换边
}

function openSub(row, items) {
  clearSubTimer();
  if (subRow === row && subEl) return;   // 同一项重复触发不重开（避免闪烁）
  closeSub();
  row.classList.add('hot');
  const sub = buildCtx(items, true);
  document.body.append(sub);             // 先入 DOM 才能量到尺寸
  placeBeside(sub, row);
  subEl = sub; subRow = row;
}

function showCtx(items, x, y) {
  closeCtx(); closePops();
  ctxEl = buildCtx(items, false);
  document.body.append(ctxEl);
  const w = ctxEl.offsetWidth, ht = ctxEl.offsetHeight;
  const left = Math.max(8, Math.min(x, window.innerWidth - w - 8));
  const top = Math.max(8, Math.min(y, window.innerHeight - ht - 8));
  ctxEl.style.left = left + 'px';
  ctxEl.style.top = top + 'px';
  ctxEl.style.transformOrigin = (left === x ? 'left' : 'right') + ' ' + (top === y ? 'top' : 'bottom');
}

/* ---- 键盘导航（↑↓ 移动 / → 展开子菜单 / ← 收起 / Enter 执行 / Esc 关闭） ---- */
function kbdSet(el) {
  if (kbdEl === el) return;
  if (kbdEl) kbdEl.classList.remove('kbd');
  kbdEl = el || null;
  if (kbdEl) kbdEl.classList.add('kbd');
}
function kbdRows() {
  const m = subEl || ctxEl;
  if (!m) return [];
  return Array.from(m.querySelectorAll('.ctx-item[data-act]'))
    .filter(r => !r.classList.contains('disabled'));
}
function kbdMove(dir) {
  const rows = kbdRows();
  if (!rows.length) return;
  const i = rows.indexOf(kbdEl);
  kbdSet(rows[i < 0 ? (dir > 0 ? 0 : rows.length - 1) : (i + dir + rows.length) % rows.length]);
}
function kbdRight() {
  if (kbdEl && kbdEl.dataset.sub === '1') kbdEl.dispatchEvent(new MouseEvent('mouseenter'));
}
function kbdLeft() {
  if (subEl) { const r = subRow; closeSub(); if (r) kbdSet(r); return; }
  closeCtx();
}

document.addEventListener('mousedown', (e) => {
  // 搜索范围按钮交给自身 click 做「再点收起」，这里不抢先关闭
  if (ctxEl && !e.target.closest('.ctx-menu') && !e.target.closest('#searchField')) closeCtx();
});
window.addEventListener('resize', closeCtx);
window.addEventListener('blur', closeCtx);

/* ================= 20. 删除 / 移出库 / 复制 ================= */
async function deleteFilesFlow(ids, afterPreview) {
  ids = (ids || []).filter(Boolean);
  if (!ids.length) return;
  const ok = await askConfirm({
    title: ids.length === 1 ? '移到回收站？' : '把这 ' + ids.length + ' 项移到回收站？',
    msg: '磁盘上的文件会被移到 Windows 回收站（可自行还原），同时从媒体库中移除。',
    confirm: '移到回收站', danger: true,
  });
  if (!ok) return;
  const r = await api('deleteFiles', ids, 'trash');
  if (r && r.ok) {
    toast('已移到回收站：' + r.trashed + ' 项' + (r.failed ? '，' + r.failed + ' 项失败' : ''));
    if (afterPreview) closePreview();
    clearSelection();
    await refreshAll();
    refreshWsCounts();
  }
}
async function unlistFilesFlow(ids) {
  ids = (ids || []).filter(Boolean);
  if (!ids.length) return;
  const r = await api('deleteFiles', ids, 'unlist');
  if (r && r.ok) {
    toast('已从库中移除 ' + r.removed + ' 项（磁盘文件保留）');
    clearSelection();
    await refreshAll();
  }
}
async function deleteFolderFlow(f) {
  const path = f.path || '';
  const ok = await askConfirm({
    title: '移到回收站：整个文件夹？',
    msg: '「' + (f.folderName || f.name || path) + '」整个目录（包含其中的全部文件与子文件夹）会被移到 Windows 回收站，并从媒体库移除。',
    detail: path,
    confirm: '移到回收站', danger: true,
  });
  if (!ok) return;
  const r = await api('deleteFolder', f.id || null, path, 'trash');
  if (r && r.ok) {
    toast('已删除文件夹（' + r.removed + ' 项已移出库）');
    await refreshAll();
    refreshWsCounts();
  } else toast('删除失败：文件夹不存在或已被移动', 'err');
}
async function unlistFolderFlow(f) {
  const ok = await askConfirm({
    title: '从库中移除这个文件夹？',
    msg: '只移除媒体库里的记录，磁盘上的文件不会被删除。',
    detail: f.path || '',
    confirm: '移除记录',
  });
  if (!ok) return;
  const r = await api('deleteFolder', f.id || null, f.path || '', 'unlist');
  if (r && r.ok) { toast('已移除 ' + r.removed + ' 项记录'); await refreshAll(); refreshWsCounts(); }
}
function copyText(txt, msg) {
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = txt;
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    ta.remove();
    toast(ok ? (msg || '已复制') : '复制失败', ok ? 'ok' : 'err');
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(() => toast(msg || '已复制')).catch(fallback);
    } else fallback();
  } catch (_) { fallback(); }
}

/* ================= 21. 分组方式（多分组 + 自定义粒度） ================= */
const GROUP_LABEL = { folder: '按文件夹', date: '按日期', author: '按作者', type: '按类型', none: '不分组' };
const GRAN_SHORT = { year: '年', month: '月', day: '日' };
function updateGroupBtn() {
  const b = $('groupBtn'); if (!b) return;
  const span = b.querySelector('span');
  if (!span) return;
  span.textContent = state.groupBy === 'date'
    ? '按日期·' + (GRAN_SHORT[state.dateGran] || '月')
    : (GROUP_LABEL[state.groupBy] || '按文件夹');
}
function applyGroupBy(gb, gran, persist = true) {
  state.groupBy = gb;
  if (gran) state.dateGran = gran;
  updateGroupBtn();
  if (persist) api('setGroupBy', state.groupBy, state.dateGran).catch(() => {});
  runQuery(true);
}
function groupSubmenuItems() {
  const mk = (gb, label, gran) => ({
    label, checked: state.groupBy === gb && (!gran || state.dateGran === gran),
    onClick: () => applyGroupBy(gb, gran),
  });
  return [
    { head: true, label: '分组方式' },
    mk('folder', '文件夹'),
    mk('none', '不分组'),
    { sep: true },
    { head: true, label: '按日期（自定义粒度）' },
    mk('date', '日期 · 年', 'year'),
    mk('date', '日期 · 月', 'month'),
    mk('date', '日期 · 日', 'day'),
    { sep: true },
    mk('author', '作者'),
    mk('type', '类型（图片/视频）'),
  ];
}
function openGroupPop() {
  closePops();
  const pop = $('groupPop'), a = $('groupBtn').getBoundingClientRect();
  pop.style.left = Math.max(12, Math.min(window.innerWidth - 250, a.right - 210)) + 'px';
  pop.style.top = (a.bottom + 8) + 'px';
  pop.style.bottom = 'auto'; pop.style.transform = 'none'; pop.style.transformOrigin = 'top right';
  renderGroupPop();
  pop.classList.add('show');
}
function renderGroupPop() {
  const list = $('groupPopList'); list.innerHTML = '';
  const row = (label, sub, on) => {
    const r = h('div', { class: 'pop-item' + (on ? ' on' : '') });
    r.append(h('span', { text: label }));
    if (sub) r.append(h('span', { class: 'cnt', text: sub }));
    if (on) r.append(h('span', { class: 'chk', html: SVG.check }));
    return r;
  };
  const pick = (gb, gran) => { applyGroupBy(gb, gran); closePops(); };
  const r1 = row('文件夹', '默认', state.groupBy === 'folder'); r1.addEventListener('click', () => pick('folder')); list.append(r1);
  list.append(h('div', { class: 'pop-title', text: '按日期（粒度可自定义）' }));
  for (const [g, lab] of [['year', '年'], ['month', '月'], ['day', '日']]) {
    const on = state.groupBy === 'date' && state.dateGran === g;
    const r = row('按日期 · ' + lab, '', on);
    r.addEventListener('click', () => pick('date', g));
    list.append(r);
  }
  list.append(h('div', { class: 'pop-title', text: '其他' }));
  for (const [gb, lab] of [['author', '作者'], ['type', '类型（图片/视频）'], ['none', '不分组']]) {
    const r = row(lab, '', state.groupBy === gb);
    r.addEventListener('click', () => pick(gb));
    list.append(r);
  }
}

/* ================= 22. 各类右键菜单 ================= */
function checkedTagState(t, items) { return tagStateOf(t, items); }
function tagSubmenu(items) {
  const tags = (state.sidebar && state.sidebar.tags) || [];
  const out = [{ head: true, label: '标签（✓ 全部已有，— 部分已有）' }];
  if (!tags.length) out.push({ label: '尚无标签', disabled: true });
  for (const t of tags) {
    const st = checkedTagState(t, items);
    out.push({
      label: t.name,
      checked: st === 'all',
      icon: st === 'part'
        ? '<svg viewBox="0 0 24 24"><path d="M7 12h10"/></svg>'
        : '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5.5" fill="' + (t.color || '#0A84FF') + '" stroke="none"/></svg>',
      onClick: () => assignTagToIds(items.map(i => i.id), t, st === 'all' ? 'remove' : 'add'),
    });
  }
  out.push({ sep: true });
  out.push({ label: '全部移除标签…', icon: IC.minus, disabled: !tags.length,
    onClick: () => removeAllTagsFlow(items) });
  out.push({ label: '新建标签并应用…', icon: IC.plus, onClick: () => newTagFlow(items) });
  return out;
}
async function assignTagToIds(ids, t, mode) {
  const r = await api('assignTags', ids, [t.id], mode);
  if (r && r.ok) {
    for (const it of state.flat) {
      if (!ids.includes(it.id)) continue;
      it.tags = it.tags || [];
      if (mode === 'add') { if (!it.tags.some(x => x.id === t.id)) it.tags.push({ id: t.id, name: t.name, color: t.color }); }
      else it.tags = it.tags.filter(x => x.id !== t.id);
    }
    toast(mode === 'add' ? '已应用标签「' + t.name + '」' : '已移除标签「' + t.name + '」');
    updateTagsInDom(ids);
    refreshSidebar();
  }
}
async function removeAllTagsFlow(items) {
  const ids = items.map(i => i.id);
  const tagIds = Array.from(new Set(items.flatMap(i => (i.tags || []).map(t => t.id))));
  if (!tagIds.length) { toast('所选项目没有标签'); return; }
  const ok = await askConfirm({
    title: '移除全部标签？',
    msg: '将从 ' + ids.length + ' 个项目中移除 ' + tagIds.length + ' 个标签。',
    confirm: '全部移除', danger: true,
  });
  if (!ok) return;
  for (const tid of tagIds) await api('assignTags', ids, [tid], 'remove');
  for (const it of items) it.tags = [];
  toast('已移除全部标签');
  updateTagsInDom(ids);
  refreshSidebar();
}
async function newTagFlow(items) {
  const name = await askText({ title: '新建标签', msg: '标签会立即应用到所选项目。', placeholder: '标签名称', confirm: '创建并应用' });
  if (!name) return;
  const palette = ['#0A84FF', '#30D158', '#FF9F0A', '#FF453A', '#BF5AF2', '#64D2FF', '#FFD60A', '#FF375F'];
  const color = palette[Math.abs(hashStr(name)) % palette.length];
  const t = await api('createTag', name, color);
  if (t && t.id != null) {
    await refreshSidebar();
    await assignTagToIds(items.map(i => i.id), t, 'add');
    toast('已创建并应用标签「' + t.name + '」');
  }
}
function authorSubmenu(items) {
  const authors = (state.sidebar && state.sidebar.authors) || [];
  const out = [{ head: true, label: '设为作者' }];
  if (!authors.length) out.push({ label: '尚无作者', disabled: true });
  for (const a of authors) {
    out.push({ label: a.name, shortcut: String(a.count), icon: SVG.person,
      onClick: () => applyAuthorToIds(items.map(i => i.id), a.id, null, a.name) });
  }
  out.push({ sep: true });
  out.push({ label: '新建作者并应用…', icon: IC.plus, onClick: () => newAuthorFlow(items) });
  out.push({ label: '清除作者标记', icon: IC.minus,
    onClick: () => applyAuthorToIds(items.map(i => i.id), null, null, null) });
  return out;
}
async function applyAuthorToIds(ids, authorId, newName, label) {
  const r = await api('assignAuthor', ids, authorId, newName);
  if (r && r.ok) {
    toast(authorId == null && !newName ? '已清除作者标记' : '已设置作者「' + (label || newName) + '」');
    await refreshAll(true);
  }
}
async function newAuthorFlow(items) {
  const name = await askText({ title: '新建作者', msg: '创建后会立即应用到所选项目。', placeholder: '作者名称', confirm: '创建并应用' });
  if (!name) return;
  await applyAuthorToIds(items.map(i => i.id), null, name, name);
}
function sizeSubmenuItems() {
  const cur = Number(getComputedStyle(document.documentElement).getPropertyValue('--card-w').replace('px', '')) || 210;
  const mk = (v, label) => ({ label, checked: Math.abs(cur - v) < 12, onClick: () => applyCardW(v) });
  return [{ head: true, label: '缩略图大小' }, mk(150, '小'), mk(210, '中'), mk(300, '大'), mk(360, '特大'), { sep: true },
    { label: '恢复默认（中）', onClick: () => applyCardW(210) }];
}
function selectAll() {
  if (!state.flat.length) return;
  state.sel = new Set(state.flat.map(i => i.id));
  state.anchor = 0;
  syncSelDom(); refreshBatchBar(); updateCounts();
  if (state.hasMore) {
    toast('已选择已加载的 ' + state.sel.size + ' 项（共 ' + state.total + ' 项，继续向下滚动可加载更多）');
  }
}

function fileMenu(items, x, y) {
  const ids = items.map(i => i.id);
  const one = items.length === 1 ? items[0] : null;
  const openAll = () => { for (const id of ids) api('openFile', id).catch(() => {}); };
  const out = [];
  if (one) {
    out.push({ label: '打开', icon: IC.open, shortcut: '↵', onClick: openAll });
    out.push({ label: '预览', icon: IC.eye, shortcut: 'Space',
      onClick: () => openPreview(state.flat.findIndex(t => t.id === one.id)) });
  } else {
    out.push({ label: '打开 ' + items.length + ' 项', icon: IC.open, onClick: openAll });
  }
  out.push({ label: '在系统中打开', icon: IC.open, onClick: openAll });
  if (one) out.push({ label: '在资源管理器中显示', icon: IC.reveal,
    onClick: () => api('revealFile', one.id).catch(() => {}) });
  out.push({ sep: true });
  out.push({ label: one ? '标签' : '标签（' + items.length + ' 项）', icon: IC.tag, sub: tagSubmenu(items) });
  out.push({ label: '设为作者', icon: SVG.person, sub: authorSubmenu(items) });
  out.push({ sep: true });
  out.push({ label: one ? '复制文件名' : '复制文件名（' + items.length + ' 个）', icon: IC.copy,
    onClick: () => copyText(items.map(i => i.name).join('\n'), '已复制文件名') });
  if (one) out.push({ label: '复制完整路径', icon: IC.copy, onClick: () => copyText(one.path || one.name, '已复制路径') });
  out.push({ sep: true });
  out.push({ label: '从库中移除（保留文件）', icon: IC.minus, onClick: () => unlistFilesFlow(ids) });
  out.push({ label: '移到回收站…', icon: IC.trash, danger: true, onClick: () => deleteFilesFlow(ids) });
  showCtx(out, x, y);
}

function blankMenu(x, y) {
  showCtx([
    { label: '网格视图', icon: IC.grid, checked: state.view === 'grid', onClick: () => applyView('grid') },
    { label: '列表视图', icon: IC.list, checked: state.view === 'list', onClick: () => applyView('list') },
    { sep: true },
    { label: '分组方式', icon: IC.layers, sub: groupSubmenuItems() },
    { label: '缩略图大小', icon: IC.size, sub: sizeSubmenuItems() },
    { sep: true },
    { label: '全选', icon: IC.selectAll, shortcut: 'Ctrl A', onClick: selectAll, disabled: !state.flat.length },
    { label: '取消选择', icon: IC.square, shortcut: 'Esc', onClick: clearSelection, disabled: !state.sel.size },
    { sep: true },
    { label: '添加文件…', icon: IC.plus, onClick: () => pickScan('files') },
    { label: '扫描文件夹…', icon: SVG.folder, onClick: () => pickScan('folder') },
    { label: '重新扫描…', icon: IC.refresh, onClick: openSrcPop },
  ], x, y);
}

function libMenu(which, x, y) {
  showCtx([
    { label: '全部媒体', icon: IC.image, checked: which === 'all', onClick: () => applyFilter('lib:all', {}) },
    { label: '只看图片', icon: IC.image, checked: which === 'image', onClick: () => applyFilter('lib:image', { type: 'image' }) },
    { label: '只看视频', icon: IC.video, checked: which === 'video', onClick: () => applyFilter('lib:video', { type: 'video' }) },
    { sep: true },
    { label: '添加文件…', icon: IC.plus, onClick: () => pickScan('files') },
    { label: '扫描文件夹…', icon: SVG.folder, onClick: () => pickScan('folder') },
    { label: '重新扫描…', icon: IC.refresh, onClick: openSrcPop },
    { sep: true },
    { label: '媒体库统计…', icon: IC.info, onClick: showStats },
  ], x, y);
}
function specialMenu(kind, x, y) {
  showCtx([
    { label: kind === 'untagged' ? '只看无标签' : '只看未识别作者', icon: SVG.tag || IC.tag,
      onClick: () => applyFilter('special:' + kind, { special: kind }) },
    { sep: true },
    { label: '全选当前列表', icon: IC.selectAll, onClick: selectAll },
    { label: '重新扫描…', icon: IC.refresh, onClick: openSrcPop },
  ], x, y);
}
function folderMenu(f, x, y) {
  showCtx([
    { label: '只看此文件夹', icon: SVG.folder, onClick: () => applyFilter('folder:' + f.id, { folderId: f.id, folderPath: f.path }) },
    { label: '在资源管理器中打开', icon: IC.reveal, onClick: () => api('revealPath', f.path) },
    { sep: true },
    { label: '在此文件夹中搜索', icon: IC.info, onClick: () => {
      applyFilter('folder:' + f.id, { folderId: f.id, folderPath: f.path });
      const si = $('searchInput'); si.value = ''; si.focus();
    } },
    { label: '重新扫描此文件夹', icon: IC.refresh, onClick: async () => {
      const r = await api('rescan', [f.path]);
      if (r && r.started) toast('已开始重扫该文件夹');
      else toast('该文件夹不在扫描源中，请用「扫描文件夹」重新添加', 'err');
    } },
    { sep: true },
    { label: '从库中移除记录…', icon: IC.minus, onClick: () => unlistFolderFlow(f) },
    { label: '移到回收站（整个文件夹）…', icon: IC.trash, danger: true, onClick: () => deleteFolderFlow(f) },
  ], x, y);
}
function authorMenu(a, x, y) {
  showCtx([
    { label: '只看该作者', icon: SVG.person, onClick: () => applyFilter('author:' + a.id, { authorId: a.id }) },
    { label: '重命名作者…', icon: IC.pencil, onClick: async () => {
      const name = await askText({ title: '重命名作者', value: a.name, placeholder: '作者名称', confirm: '重命名' });
      if (!name || name === a.name) return;
      await api('renameAuthor', a.id, name);
      toast('已重命名为「' + name + '」');
      await refreshAll(true);
    } },
    { sep: true },
    { label: '全选当前列表', icon: IC.selectAll, onClick: selectAll },
    { label: '删除作者（文件退回未识别）…', icon: IC.trash, danger: true, onClick: async () => {
      const ok = await askConfirm({
        title: '删除作者「' + a.name + '」？',
        msg: '该名下的 ' + a.count + ' 个文件会退回「未识别作者」，磁盘文件不受影响。',
        confirm: '删除作者', danger: true,
      });
      if (!ok) return;
      await api('deleteAuthor', a.id);
      toast('已删除作者');
      await refreshAll();
    } },
  ], x, y);
}
function tagMenu(t, x, y) {
  showCtx([
    { label: '只看此标签', icon: IC.tag, onClick: () => applyFilter('tags:' + t.id, { tagIds: [t.id] }) },
    { label: '叠加到当前筛选', icon: IC.plus, onClick: () => {
      const ids = state.filters.tagIds.includes(t.id) ? state.filters.tagIds : state.filters.tagIds.concat([t.id]);
      applyFilter('tags:' + ids.slice().sort((a, b) => a - b).join(','), { tagIds: ids });
    } },
    { sep: true },
    { label: '重命名标签…', icon: IC.pencil, onClick: async () => {
      const name = await askText({ title: '重命名标签', value: t.name, placeholder: '标签名称', confirm: '重命名' });
      if (!name || name === t.name) return;
      await api('renameTag', t.id, name, null);
      toast('标签已重命名');
      await refreshAll(true);
    } },
    { label: '改颜色…', icon: IC.square, sub: ['#0A84FF', '#30D158', '#FF9F0A', '#FF453A', '#BF5AF2', '#64D2FF', '#FFD60A', '#FF375F'].map(c => ({
      label: c, icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="' + c + '" stroke="none"/></svg>',
      onClick: async () => { await api('renameTag', t.id, null, c); await refreshSidebar(); renderGrid(); toast('颜色已更新'); },
    })) },
    { sep: true },
    { label: '删除标签…', icon: IC.trash, danger: true, onClick: async () => {
      const ok = await askConfirm({
        title: '删除标签「' + t.name + '」？',
        msg: '标签会从 ' + t.count + ' 个文件上移除，磁盘文件不受影响。',
        confirm: '删除标签', danger: true,
      });
      if (!ok) return;
      await api('deleteTag', t.id);
      toast('标签已删除');
      await refreshAll();
    } },
  ], x, y);
}
function showStats() {
  const c = (state.sidebar && state.sidebar.counts) || {};
  const ws = currentWs();
  showCtx([
    { head: true, label: (ws ? ws.name : '') + ' 工作区' },
    { label: '全部媒体 · ' + (c.all || 0) + ' 项', icon: IC.image, disabled: true },
    { label: '图片 · ' + (c.images || 0) + ' 项', icon: IC.image, disabled: true },
    { label: '视频 · ' + (c.videos || 0) + ' 项', icon: IC.video, disabled: true },
    { label: '无标签 · ' + (c.untagged || 0) + ' 项', icon: IC.tag, disabled: true },
    { label: '未识别作者 · ' + (c.unknownAuthor || 0) + ' 项', icon: SVG.person, disabled: true },
    { sep: true },
    { label: '立即备份', icon: IC.copy, onClick: doBackup },
  ], window.innerWidth - 300, 120);
}

/* 卡片/列表行右键：必要时先选中，再弹菜单 */
function onItemContext(item, idx, e) {
  if (!state.sel.has(item.id)) {
    if (!(e.ctrlKey || e.metaKey)) state.sel.clear();
    state.sel.add(item.id);
    state.anchor = idx;
    syncSelDom(); refreshBatchBar(); updateCounts();
  }
  const items = selItems();
  fileMenu(items.length ? items : [item], e.clientX, e.clientY);
}

/* ============================================================
   区块功能索引
   1. 全局状态            state
   2. 工具函数            h / SVG 图标 / hash / fmt*
   3. Toast              toast()
   4. API 封装           bridge() / api()
   5. 启动               init() + pywebviewready + scan-progress/data-changed
   6. 数据加载           refreshAll / refreshSidebar / runQuery
   7. 工作区             渲染/切换(交叉淡入)/新建/重命名/删除弹层
   8. 侧边栏筛选         库/待整理/文件夹/作者/标签(Ctrl 多选 AND) + 面包屑
   9. 媒体网格           骨架屏 / 分组卡片 / 渐变占位 / 时长与RAW徽标 / 空状态
   10. 选择逻辑          单击 / Ctrl 切换 / Shift 范围 / Esc（优先级：预览>弹层>选择）
   11. 批量操作条        浮出/计数/打开/取消选择（窄屏收成纯图标）
   12. 弹层              打标签(新建+应用) / 设作者(新建/选择/清除) / 外部点击关闭
   13. 扫描进度          pickAndScan / rescan / scan-progress 状态栏渲染
   14. 备份              backupNow → toast；fmtBackupName 拆分「工作区::文件名」；历史/恢复弹层
   15. 全屏预览层        openPreview/closePreview/navPreview/renderPreviewMedia（mediaUrl 流式播放）
   16. 静态事件绑定      红绿灯/主题/搜索防抖/工具组/弹层按钮/抽屉遮罩/预览层按钮
   ============================================================ */
