"""极简内嵌管理单页路由（替代已删除的 React 前端与多页 HTML）。

2026-08 单页化：全部管理功能合并进 static/admin.html（9 个 hash tab）。
/admin 经 Keycloak SSO 会话守卫（require_admin_page），未登录 302 /auth/login。
/transparency 为公开透明度披露，免认证——披露本身就是义务；披露正文（单一内容源
services/transparency.py 渲染的纯文本）+ 轻量 DSAR 提交表单（浏览器直达 Art.15/17/21，
无需手工构造 JSON；提交打到 /v1/dsar/request，同源无 CORS）。
旧多页子路径（/admin/anchors 等 8 条）302 到对应 hash tab，保运维书签可用。
"""
import html as html_mod
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from services.auth import require_admin_page
from services.transparency import render_transparency_text, DSAR_SUBMISSION

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter(include_in_schema=False)


@router.get("/")
async def root():
    return RedirectResponse("/admin")


@router.get("/admin")
async def admin_home(_auth=Depends(require_admin_page)):
    return FileResponse(STATIC_DIR / "admin.html")


# 旧多页管理子路径（单页化前存在，已删除）→ 302 到对应 hash tab，保旧书签不 404
_LEGACY_TAB_PATHS = {
    "/admin/anchors": "anchors",
    "/admin/audit": "audit",
    "/admin/blocked": "blocked",
    "/admin/collect": "collect",
    "/admin/dsar": "dsar",
    "/admin/pending": "pending",
    "/admin/rules": "rules",
    "/admin/tags": "tags",
}


def _legacy_shim(tab: str):
    async def shim(_auth=Depends(require_admin_page)):
        return RedirectResponse(f"/admin#{tab}")
    return shim


for _path, _tab in _LEGACY_TAB_PATHS.items():
    router.get(_path)(_legacy_shim(_tab))


# 轻量 DSAR 表单页（零依赖，系统字体，与 admin.html 同工程风格）
_DSAR_FORM_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Joytag 透明度披露与数据主体权利</title>
<style>
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 880px;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.6; color: #222; }
  pre { white-space: pre-wrap; border: 1px solid #ddd; padding: 1rem; background: #fafafa; font-size: 13px; }
  h2 { margin-top: 2rem; }
  form { border: 1px solid #ccc; padding: 1rem; background: #f7f7f7; }
  label { display: block; margin: .6rem 0 .2rem; }
  input, select, textarea { width: 100%; padding: .4rem; box-sizing: border-box;
                            font: inherit; border: 1px solid #999; }
  button { margin-top: .8rem; padding: .5rem 1.2rem; font: inherit; cursor: pointer; }
  #dsar-result { margin-top: .8rem; white-space: pre-wrap; }
  .ok { color: #0a6a0a; } .err { color: #a00; }
</style>
</head>
<body>
<h1>Joytag 透明度披露与数据主体权利</h1>
<pre>{TEXT}</pre>

<h2>提交数据主体请求（DSAR）</h2>
<p>您也可以在此直接提交 GDPR 请求（等价于向 {ENDPOINT} 发送 JSON；同一 IP 每小时限 {RATE_LIMIT} 次）。</p>
<form id="dsar-form">
  <label for="f-type">请求类型</label>
  <select id="f-type" name="request_type" required>
    <option value="access">访问权（Art.15）access</option>
    <option value="erasure">删除权（Art.17）erasure</option>
    <option value="objection">反对权（Art.21）objection</option>
  </select>
  <label for="f-contact">联系方式（必填，用于 {DEADLINE} 天内回复）</label>
  <input id="f-contact" name="contact" type="text" required minlength="3" maxlength="500"
         placeholder="邮箱或其他联系方式">
  <label for="f-note">请求说明（可选，如涉及的词条）</label>
  <textarea id="f-note" name="subject_note" maxlength="2000" rows="3"></textarea>
  <button type="submit">提交请求</button>
</form>
<div id="dsar-result"></div>

<script>
(function () {
  var form = document.getElementById('dsar-form');
  var resultEl = document.getElementById('dsar-result');
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    resultEl.textContent = '提交中…';
    resultEl.className = '';
    var payload = {
      request_type: document.getElementById('f-type').value,
      contact: document.getElementById('f-contact').value.trim(),
      subject_note: document.getElementById('f-note').value.trim(),
    };
    fetch('{ENDPOINT}', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok) { throw new Error(data.detail || ('HTTP ' + resp.status)); }
        return data;
      });
    }).then(function (data) {
      resultEl.className = 'ok';
      resultEl.textContent = '已受理。请保存工单号：' + data.ticket_id + '\\n' + data.message;
    }).catch(function (e) {
      resultEl.className = 'err';
      resultEl.textContent = '提交失败：' + e.message;
    });
  });
})();
</script>
</body>
</html>
"""


@router.get("/transparency")
async def transparency():
    """公开透明度披露（GDPR Art.14(5)(b) 通知 + DSA Art.27 摘要 + AI Act Art.50 声明 + DSAR 受理）。

    披露正文 = services/transparency.py 单一内容源渲染的纯文本（<pre> 原样呈现），
    页面附带轻量 DSAR 提交表单（POST /v1/dsar/request，免认证）。
    机器可读 JSON 版见 /v1/transparency（同一内容源）。
    """
    # 模板内 JS/CSS 含大量花括号，不用 .format()，改 token 替换避免转义地狱
    page = (_DSAR_FORM_PAGE
            .replace("{TEXT}", html_mod.escape(render_transparency_text()))
            .replace("{ENDPOINT}", DSAR_SUBMISSION["endpoint"])
            .replace("{RATE_LIMIT}", DSAR_SUBMISSION["rate_limit"].split("/")[0])
            .replace("{DEADLINE}", str(DSAR_SUBMISSION["response_deadline_days"])))
    return HTMLResponse(page)
