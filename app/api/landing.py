"""根路径落地页。

为什么需要它：README 顶部把公网地址当作"已上线"的证据，但 `/` 原本没有任何路由，
直接落到 404。点开链接看到 `{"detail":"Not Found"}`，很容易被理解成"项目挂了"，
而不是"这个服务没有首页"。

这一页只做一件事：让人在三秒内知道这是什么、怎么调、去哪里看文档。
刻意不引任何外部资源（CDN/字体/图片）——服务器直连外网不稳定，
一个转不出来的页面比没有页面更糟，所以 CSS 全部内联。

注意注册顺序：这条路由必须排在 `/{code}` 之前，否则 `/` 会被当成短码去查库。
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["landing"])

# 用 __HOST__ 占位而不是 str.format：CSS 里全是花括号，
# format 会把它们当占位符炸掉。replace 是这里唯一安全的选择。
_LANDING_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ShortLink · 高并发短链服务</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --border: #262b35;
    --fg: #e6e9ef;
    --muted: #9aa4b2;
    --accent: #4f9cf9;
    --ok: #3fb950;
    --code-bg: #10131a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 48px 20px 64px;
    background: var(--bg);
    color: var(--fg);
    font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC",
          Roboto, "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 26px; letter-spacing: -0.3px; margin: 0 0 8px; }
  .tag {
    display: inline-block;
    font-size: 12px;
    color: var(--ok);
    border: 1px solid rgba(63, 185, 80, 0.35);
    background: rgba(63, 185, 80, 0.08);
    border-radius: 999px;
    padding: 2px 10px;
    vertical-align: middle;
    margin-left: 8px;
    font-weight: 500;
  }
  .lead { color: var(--muted); margin: 0 0 32px; }
  h2 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--muted);
    font-weight: 600;
    margin: 32px 0 12px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
  }
  .row {
    display: flex;
    gap: 14px;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
    align-items: baseline;
  }
  .row:last-child { border-bottom: 0; padding-bottom: 0; }
  .row:first-child { padding-top: 0; }
  .m {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 12.5px;
    min-width: 92px;
    color: var(--accent);
    font-weight: 600;
  }
  .d { color: var(--muted); font-size: 13.5px; }
  .d code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12.5px;
    color: var(--fg);
  }
  pre {
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
    overflow-x: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12.5px;
    line-height: 1.6;
    margin: 0;
    color: #cfd6e4;
  }
  pre .c { color: #6b7688; }
  pre .s { color: #7ee787; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .links { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 4px; }
  .btn {
    display: inline-block;
    padding: 7px 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--fg);
    font-size: 13px;
    background: var(--panel);
  }
  .btn:hover { border-color: var(--accent); text-decoration: none; }
  footer {
    margin-top: 40px;
    padding-top: 18px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 12.5px;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>ShortLink<span class="tag">online</span></h1>
  <p class="lead">
    高并发短链服务 · 业务只有两个接口，其余精力投入缓存策略与并发优化。
  </p>

  <h2>API</h2>
  <div class="card">
    <div class="row">
      <span class="m">POST</span>
      <span class="d"><code>/api/shorten</code> — 长链换短码，返回 code、short_url、long_url</span>
    </div>
    <div class="row">
      <span class="m">GET</span>
      <span class="d"><code>/{code}</code> — 302 跳转回原链（访问埋点写入 Redis）</span>
    </div>
    <div class="row">
      <span class="m">GET</span>
      <span class="d"><code>/health</code> — 健康检查</span>
    </div>
    <div class="row">
      <span class="m">GET</span>
      <span class="d"><code>/internal/cache-stats</code> — 命中率 / 回源次数 / 熔断状态</span>
    </div>
  </div>

  <h2>试一试</h2>
  <pre><span class="c"># 生成短链</span>
curl -X POST __HOST__/api/shorten \\
  -H <span class="s">'Content-Type: application/json'</span> \\
  -d <span class="s">'{"url":"https://github.com/SaberAliceLOL/shortlink"}'</span>

<span class="c"># → {"code":"101JA","short_url":"__HOST__/101JA","long_url":"..."}</span>

<span class="c"># 访问 /101JA 会 302 跳转回原链</span></pre>

  <h2>文档</h2>
  <div class="links">
    <a class="btn" href="/docs">Swagger UI</a>
    <a class="btn" href="/redoc">ReDoc</a>
    <a class="btn" href="/health">Health</a>
    <a class="btn" href="/internal/cache-stats">Cache stats</a>
  </div>

  <footer>
    缓存层：Cache Aside + 逻辑过期（互斥锁可切换）+ 水位守卫防穿透。
    <a href="https://github.com/SaberAliceLOL/shortlink">源码与设计决策记录 →</a>
  </footer>
</div>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing(request: Request) -> HTMLResponse:
    """服务首页。host 从请求头取，本地与公网共用同一份页面。

    Nginx 已透传 Host 头，所以这里拿到的是用户实际访问的地址
    （公网是 101.200.80.105，本地是 localhost:8000），示例 curl 不会指错。
    """
    host = request.headers.get("host", "localhost:8000")
    return HTMLResponse(content=_LANDING_HTML.replace("__HOST__", f"http://{host}"))
