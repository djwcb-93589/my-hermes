"""
本地 fixture 网站的页面内容。

每个函数返回一段 HTML 字符串,由 ``server.py`` 按 path 分发。页面刻意写得
语义清晰(显式 role / aria-label),让可访问性树稳定生成可操作的 ref,
避免测试因页面结构抖动而失效。

设计要点:
- ``/`` 和 ``/article?name=`` 用不同内容,测 ref 跨页重新编号。
- ``/form`` GET 提交到 ``/result``,测 type/select/press 完整表单链路。
- ``/cookie-set`` 用 Set-Cookie 头写 cookie,``/cookie-check`` 读回,
  测跨 navigate 的 cookie 持久(需要真实 HTTP,data: 做不到)。
- ``/appear?delay=N`` 页面加载后 JS 延迟 N 毫秒插入目标文本,测 wait_for_text
  的"成功"路径(而非超时)。
- ``/slow?delay=N`` 服务器侧延迟 N 毫秒才响应,测 wait_for_load_state /
  wait 超时 / wait 取消。
"""

from __future__ import annotations

from urllib.parse import quote


def home_page() -> str:
    """首页:覆盖 navigate/snapshot/get_text 基础 + 各类控件。

    含 link(跳 article)、button(弹 alert)、input、select、heading、
    长段落。控件都带显式 label,ref 稳定。
    """
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>Fixture 首页</title></head>
<body>
  <header>
    <h1>Fixture 测试首页</h1>
    <nav aria-label="主导航">
      <a href="/article?name=alpha">文章 Alpha</a>
      <a href="/article?name=beta">文章 Beta</a>
      <a href="/form">表单页</a>
      <a href="/long">长页面</a>
    </nav>
  </header>
  <main>
    <h2>控件演示</h2>
    <p>这是一个用于测试的本地页面。段落里的文字应当能被 get_text 连贯读出。</p>
    <label>搜索词 <input type="text" aria-label="搜索框" name="q"></label>
    <button id="alert-btn" type="button" onclick="alert('弹窗内容')">弹窗按钮</button>
    <label>选择水果
      <select aria-label="水果选择" id="fruit">
        <option value="apple">苹果</option>
        <option value="banana">香蕉</option>
        <option value="cherry">樱桃</option>
      </select>
    </label>
    <a href="/cookie-set">设置 Cookie</a>
    <a href="/cookie-check">检查 Cookie</a>
  </main>
</body>
</html>"""


def article_page(name: str) -> str:
    """文章页:不同 name 内容不同,测 click 跳转和 ref 跨页重置。

    name 出现在标题、正文按钮和链接里,两篇文章的 ref 行集合必然不同 --
    alpha 页有"跳转 Beta"链接和"Alpha 专属"按钮,beta 页反之。
    """
    safe = name
    other = "beta" if name == "alpha" else "alpha"
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>文章 {safe}</title></head>
<body>
  <header><h1>文章:{safe}</h1>
    <a href="/">返回首页</a>
  </header>
  <main>
    <h2>{safe} 的简介</h2>
    <p>这是文章 {safe} 的正文。它的内容和别的文章不同,用于验证跨页 ref 重新编号。</p>
    <h2>{safe} 的细节</h2>
    <p>{safe} 细节段落。Agent 通过 get_text 能读到这里的话。</p>
    <button type="button" id="{safe}-btn">{safe} 专属按钮</button>
    <a href="/article?name={other}">跳转 {other}</a>
  </main>
</body>
</html>"""


def search_page(q: str | None) -> str:
    """搜索结果页:GET 表单目标。显示搜索词和结果列表。

    q 为空时提示无结果;有 q 时显示结果 link,测 press Enter 提交后落地。
    """
    if not q:
        return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>搜索</title></head>
<body>
  <h1>搜索页</h1>
  <form action="/search" method="get" aria-label="搜索表单">
    <input type="text" aria-label="搜索框" name="q">
    <button type="submit">搜索</button>
  </form>
  <p>请输入搜索词</p>
</body>
</html>"""
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>搜索结果:{q}</title></head>
<body>
  <h1>搜索结果:{q}</h1>
  <form action="/search" method="get" aria-label="搜索表单">
    <input type="text" aria-label="搜索框" name="q" value="{q}">
    <button type="submit">搜索</button>
  </form>
  <ul>
    <li><a href="/article?name=alpha">关于 {q} 的结果一</a></li>
    <li><a href="/article?name=beta">关于 {q} 的结果二</a></li>
  </ul>
</body>
</html>"""


def form_page() -> str:
    """表单页:input + select + textarea + submit,GET 提交到 /result。"""
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>表单页</title></head>
<body>
  <h1>表单页</h1>
  <form action="/result" method="get" aria-label="示例表单">
    <label>姓名 <input type="text" aria-label="姓名" name="name"></label>
    <label>城市
      <select aria-label="城市" name="city">
        <option value="beijing">北京</option>
        <option value="shanghai">上海</option>
        <option value="shenzhen">深圳</option>
      </select>
    </label>
    <label>备注 <textarea aria-label="备注" name="note"></textarea></label>
    <button type="submit">提交</button>
  </form>
</body>
</html>"""


def result_page(params: dict[str, str]) -> str:
    """表单结果页:回显查询参数,验证提交成功。"""
    name = params.get("name", "")
    city = params.get("city", "")
    note = params.get("note", "")
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>提交结果</title></head>
<body>
  <h1>提交结果</h1>
  <p>姓名:{name}</p>
  <p>城市:{city}</p>
  <p>备注:{note}</p>
  <a href="/form">返回表单</a>
</body>
</html>"""


def cookie_set_page() -> str:
    """cookie 写入页:由 server 在响应头加 Set-Cookie,本页只是提示。

    cookie 名固定为 fixture_session,值固定为 fixed-value,便于断言。
    """
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>Cookie 已设置</title></head>
<body>
  <h1>Cookie 已设置</h1>
  <p>服务器已通过 Set-Cookie 头写入 fixture_session=fixed-value。</p>
  <a href="/cookie-check">检查 Cookie</a>
  <a href="/">返回首页</a>
</body>
</html>"""


def cookie_check_page() -> str:
    """cookie 检查页:JS 读取并显示 document.cookie。

    若 fixture_session 存在则显示"已保持",否则"已丢失"。
    """
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>Cookie 检查</title></head>
<body>
  <h1>Cookie 检查</h1>
  <p id="cookie-status">检查中...</p>
  <script>
    var has = document.cookie.indexOf('fixture_session=') !== -1;
    document.getElementById('cookie-status').textContent =
      has ? 'Cookie 已保持: fixture_session 存在' : 'Cookie 已丢失';
  </script>
  <a href="/">返回首页</a>
</body>
</html>"""


def long_page() -> str:
    """长页面:几百行内容,测 scroll 后内容变化 + get_text 整页截断。

    每段标号,滚动后出现的段号不同;整页文本远超 max_chars 默认值。
    """
    paragraphs = "\n".join(
        f'<p id="p{i}">第 {i} 段:这是长页面的第 {i} 个段落,内容用于测试滚动与文本截断。</p>'
        for i in range(1, 121)
    )
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>长页面</title></head>
<body>
  <h1>长页面</h1>
  {paragraphs}
</body>
</html>"""


def same_url_state_page() -> str:
    """同 URL pushState 页:JS 用 history.pushState 改 state 不改 URL。

    测 back/forward 能区分"地址相同但 history.state 不同"的位置,
    不把它们误判成无历史。
    """
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>同 URL 状态页</title></head>
<body>
  <h1>同 URL 状态页</h1>
  <p id="state-display">初始状态</p>
  <button id="push-state-btn" type="button"
          onclick="history.pushState({step:1}, ''); document.getElementById('state-display').textContent='已推进状态'">
    推进状态
  </button>
  <a href="/">返回首页</a>
</body>
</html>"""


def iframe_page() -> str:
    """iframe 页:嵌入一个子页面,测 iframe 边界。

    iframe 指向 /article?name=iframe-inner,父页有自己的按钮。
    """
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>iframe 页</title></head>
<body>
  <h1>iframe 页</h1>
  <button id="parent-btn" type="button">父页按钮</button>
  <iframe src="/article?name=iframe-inner" title="内嵌文章" width="400" height="200"></iframe>
  <a href="/">返回首页</a>
</body>
</html>"""


def dialog_page() -> str:
    """弹窗页:button 触发 alert,测弹窗处理。

    Playwright 默认自动 dismiss alert,这里测点击触发 alert 的按钮不卡死,
    且页面有可验证的副作用(alert 后改了某元素文本)。
    """
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>弹窗页</title></head>
<body>
  <h1>弹窗页</h1>
  <p id="after-alert">点击前</p>
  <button id="alert-btn" type="button"
          onclick="alert('一个弹窗'); document.getElementById('after-alert').textContent='点击后'">
    触发弹窗
  </button>
  <a href="/">返回首页</a>
</body>
</html>"""


def download_page() -> str:
    """下载页:链接到 /download/file,服务器返回 Content-Disposition 附件。"""
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>下载页</title></head>
<body>
  <h1>下载页</h1>
  <a href="/download/file">下载文件</a>
  <a href="/">返回首页</a>
</body>
</html>"""


def slow_page() -> str:
    """慢加载页:内容由服务器延迟返回(延迟在 server 侧控制),本页本身普通。

    页面加载后立即可读,用于配合 wait_for_load_state 的成功路径。
    """
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>慢加载页</title></head>
<body>
  <h1>慢加载页</h1>
  <p>这个页面由服务器延迟一段时间后才返回。wait_for_load_state 应能等到它就绪。</p>
</body>
</html>"""


def appear_page(delay_ms: int) -> str:
    """延迟出现页:页面加载后 JS 延迟 delay_ms 毫秒插入目标文本和按钮。

    测 wait_for_text / wait_for_ref 的"成功"路径:页面起初没有目标,
    等待期间目标出现,等待应成功而非超时。
    """
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>延迟出现页</title></head>
<body>
  <h1>延迟出现页</h1>
  <p id="placeholder">目标尚未出现</p>
  <script>
    setTimeout(function() {{
      var p = document.getElementById('placeholder');
      p.textContent = '延迟目标已出现';
      var btn = document.createElement('button');
      btn.id = 'late-btn';
      btn.textContent = '延迟按钮';
      btn.setAttribute('aria-label', '延迟按钮');
      document.body.appendChild(btn);
    }}, {delay_ms});
  </script>
</body>
</html>"""


def not_found_page(path: str) -> str:
    """404 页。"""
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>未找到</title></head>
<body>
  <h1>404</h1>
  <p>路径 {quote(path)} 不存在。</p>
</body>
</html>"""
