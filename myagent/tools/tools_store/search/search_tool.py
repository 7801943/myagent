"""
SearXNG 互联网搜索工具。
通过本地 SearXNG 实例 (JSON API) 实现互联网检索。
支持：关键词搜索、分类搜索、分页、结果格式化输出。

SearXNG 服务地址通过环境变量 SEARXNG_BASE_URL 配置（默认 http://localhost:8080）。
"""
import asyncio
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional


_DEFAULT_TIMEOUT = 15
_DEFAULT_MAX_RESULTS = 10


def _get_base_url() -> str:
    """从环境变量获取 SearXNG 地址，不暴露端口到代码中。"""
    return os.environ.get("SEARXNG_BASE_URL", "http://localhost:8080")


async def internet_search(
    query: str,
    categories: Optional[str] = None,
    language: str = "zh-CN",
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> str:
    """通过 SearXNG 进行互联网搜索。

    Args:
        query: 搜索关键词。
        categories: 搜索类别，支持 general/images/news/videos/music/files/it/science 等。为 None 时全类别搜索。
        language: 搜索语言偏好，默认 zh-CN。
        max_results: 最大返回结果数，默认 10。
    """

    base_url = _get_base_url()

    # 1) 构建请求 URL
    params = {
        "q": query,
        "format": "json",
        "language": language,
    }
    if categories:
        params["categories"] = categories

    url = f"{base_url.rstrip('/')}/search?{urllib.parse.urlencode(params)}"

    # 2) 发起 HTTP 请求（非阻塞）
    def _fetch():
        req = urllib.request.Request(url, headers={"User-Agent": "SearXNG-SearchTool/1.0"})
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            return resp.read().decode("utf-8")

    try:
        raw = await asyncio.to_thread(_fetch)
    except urllib.error.URLError as e:
        return f"❌ 搜索请求失败: {e}"
    except Exception as e:
        return f"❌ 未知错误: {e}"

    # 3) 解析 JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"❌ 返回数据不是有效的 JSON。原始内容:\n{raw[:500]}"

    results = data.get("results", [])

    if not results:
        return f"🔍 搜索「{query}」未找到相关结果。"

    # 4) 截取所需条数
    results = results[:max_results]

    # 5) 格式化输出
    lines = [
        f"🔍 搜索关键词: {query}",
        f"   找到约 {data.get('number_of_results', len(results))} 条结果，以下展示前 {len(results)} 条:",
        "",
    ]

    for i, item in enumerate(results, start=1):
        title   = item.get("title", "（无标题）")
        link    = item.get("url", "（无链接）")
        content = item.get("content", "（无摘要）").strip()
        engine  = item.get("engine", "")
        score   = item.get("score", 0)

        lines.append(f"  [{i}] {title}")
        lines.append(f"      🔗 {link}")
        if content:
            lines.append(f"      📝 {content}")
        lines.append(f"      ⚙️  引擎: {engine}  |  评分: {score:.2f}")
        lines.append("")

    return "\n".join(lines)