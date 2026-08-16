/* Joytag 管理单页共享逻辑（替代已删除的 React 前端） */

// 与 backend/services/collectors/countries.py 的 EU_COUNTRIES 保持一致（跨语言副本，改国家两处同步）
const EU_COUNTRIES = ['DE', 'FR', 'NL', 'UK', 'IT', 'ES'];

/* tab 唯一权威源：admin.html 的路由（currentHash/activateTab）与 renderTabs 导航共用，
   新增 tab 只改此处（并在 admin.html 注册 TABS[id]） */
const TAB_ORDER = ['overview', 'pending', 'tags', 'collect', 'rules', 'anchors', 'blocked', 'audit', 'dsar'];
const TAB_LABELS = {
  overview: '概览', pending: '待审核', tags: '标签库', collect: '采集', rules: '规则',
  anchors: '锚点库', blocked: '拦截记录', audit: '审计', dsar: 'DSAR',
};

/* 读取 cookie（CSRF 双提交：joytag_csrf cookie → X-CSRF-Token 头） */
function getCookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

/* 统一 API 封装（会话 cookie + CSRF；401 → 跳 Keycloak 登录）；429 提示限流。
   抛出的 Error 带 status 属性，调用方可按 403 等状态渲染空态。 */
async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, credentials: 'same-origin' };
  opts.headers = {};
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  if (method !== 'GET') {
    opts.headers['X-CSRF-Token'] = getCookie('joytag_csrf');
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (res.status === 401) {
    // next 带 hash，登录回来恢复当前 tab
    location.href = '/auth/login?next=' + encodeURIComponent(location.pathname + location.hash);
    throw new Error('未登录，正在跳转登录页…');
  }
  const err = (msg) => { const e = new Error(msg); e.status = res.status; return e; };
  if (res.status === 429) throw err(data && data.detail || '请求过于频繁，请稍后再试');
  if (!res.ok) throw err(data && data.detail || `请求失败 (${res.status})`);
  return data;
}

/* HTML 转义：所有渲染到表格/列表的动态文本必须过此函数 */
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let toastTimer = null;
function toast(msg, ok = true) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'toast show ' + (ok ? 'ok' : 'err');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}

/* 单页 tab 导航：brand + TAB_ORDER 的 hash 链接（id/文案单一来源见文件头），
   active 为当前 tab 的 hash */
function renderTabs(active) {
  const nav = document.getElementById('nav');
  if (!nav) return;
  nav.innerHTML =
    '<a class="brand" href="#overview">Joytag</a>' +
    TAB_ORDER.map(hash =>
      `<a href="#${hash}" class="${active === hash ? 'active' : ''}">${TAB_LABELS[hash] || hash}</a>`
    ).join('');
}

/* cursor 分页辅助：stack 记录各页游标（字符串透传，不区分类型），第 0 页为 null */
function createPager(loadFn) {
  const stack = [null];
  let index = 0;
  return {
    async goFirst() {
      stack.length = 1;
      index = 0;
      await loadFn(null);
    },
    async next(nextOffset) {
      if (nextOffset === null || nextOffset === undefined) return;
      stack.push(nextOffset);
      index++;
      await loadFn(nextOffset);
    },
    async prev() {
      if (index <= 0) return;
      index--;
      await loadFn(stack[index]);
    },
    canPrev() { return index > 0; },
    page() { return index + 1; },
  };
}

/* offset 分页助手（blocked/audit/dsar 共用）：接口均为 limit+offset、返回 total。
   旧实现三处手写且 audit/dsar 误用「本页实际条数」而非 PAGE_SIZE 算下一页，
   接口返回不足 limit 条的中间页会跳页——统一在此处用 PAGE_SIZE 计算。 */
function createOffsetPager(loadFn, pageSize) {
  pageSize = pageSize || 20;
  const stack = [];          // 已访问页的 offset（用于后退）
  let currentOffset = 0;
  return {
    async goFirst() {
      stack.length = 0;
      currentOffset = 0;
      await loadFn(0);
    },
    async next(nextOffset) {
      if (nextOffset === null || nextOffset === undefined) return;
      stack.push(currentOffset);
      currentOffset = nextOffset;
      await loadFn(currentOffset);
    },
    async prev() {
      if (!stack.length) return;
      currentOffset = stack.pop();
      await loadFn(currentOffset);
    },
    canPrev() { return stack.length > 0; },
    page() { return stack.length + 1; },
    computeNext(total) {
      return (currentOffset + pageSize) < total ? currentOffset + pageSize : null;
    },
  };
}

/* 行尾操作按钮：加载中态，防重复提交 */
function withLoading(btn, fn) {
  return async function () {
    if (btn.disabled) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '处理中…';
    try {
      await fn();
    } catch (e) {
      toast(e.message, false);
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  };
}

/* 生成国家筛选下拉（含"全部"选项） */
function countryOptions(selected) {
  return '<option value="">全部国家</option>' +
    EU_COUNTRIES.map(c => `<option value="${c}"${c === selected ? ' selected' : ''}>${c}</option>`).join('');
}
