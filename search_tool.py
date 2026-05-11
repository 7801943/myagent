"""
SearXNG 互联网搜索工具模块
============================
基于本地 SearXNG 实例 (JSON API) 实现互联网检索功能。
支持：关键词搜索、分类搜索、分页、结果格式化输出。
"""

import json
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional


# ──────────────────── 默认配置 ────────────────────
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT  = 15          # 请求超时（秒）
DEFAULT_MAX_RESULTS = 10       # 默认返回结果数


# ──────────────────── 核心搜索函数 ────────────────────
def internet_search(
    query: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    categories: Optional[str] = None,
    language: str = "zh-CN",
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
    format_output: bool = True,
) -> str | list[dict]:
    """
    通过本地 SearXNG 实例进行互联网搜索。

    参数
    ----
    query : str
        搜索关键词。
    base_url : str
        SearXNG 服务地址，默认 http://localhost:8080。
    categories : str, optional
        搜索类别，支持: general, images, news, videos, music, files, it, science 等。
        为 None 时表示全类别搜索（默认）。
    language : str
        搜索语言偏好，默认 "zh-CN"。
    max_results : int
        最大返回结果数，默认 10。
    timeout : int
        HTTP 请求超时秒数，默认 15。
    format_output : bool
        True  → 返回格式化的可读字符串（默认）。
        False → 返回原始结果列表（list[dict]），便于程序进一步处理。

    返回
    ----
    str 或 list[dict]，由 format_output 决定。

    示例
    ----
    >>> # 简单搜索（返回格式化文本）
    >>> print(internet_search("Python 教程"))
    >>>
    >>> # 获取原始 JSON 数据
    >>> results = internet_search("Python 教程", format_output=False)
    >>> for r in results:
    ...     print(r["title"], r["url"])
    >>>
    >>> # 指定分类搜索新闻
    >>> print(internet_search("最新科技新闻", categories="news"))
    """

    # 1) 构建请求 URL
    params = {
        "q": query,
        "format": "json",
        "language": language,
    }
    if categories:
        params["categories"] = categories

    url = f"{base_url.rstrip('/')}/search?{urllib.parse.urlencode(params)}"

    # 2) 发起 HTTP 请求
    req = urllib.request.Request(url, headers={"User-Agent": "SearXNG-SearchTool/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
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
        if format_output:
            return f"🔍 搜索「{query}」未找到相关结果。"
        return []

    # 4) 截取所需条数
    results = results[:max_results]

    # 5) 按需返回
    if not format_output:
        return results

    # 6) 格式化输出
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


# ──────────────────── 便捷函数 ────────────────────
def search_images(query: str, **kwargs) -> str | list[dict]:
    """搜索图片"""
    return internet_search(query, categories="images", **kwargs)


def search_news(query: str, **kwargs) -> str | list[dict]:
    """搜索新闻"""
    return internet_search(query, categories="news", **kwargs)


def search_videos(query: str, **kwargs) -> str | list[dict]:
    """搜索视频"""
    return internet_search(query, categories="videos", **kwargs)


# ──────────────────── 直接运行测试 ────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  SearXNG 搜索工具 — 测试")
    print("=" * 60)

    # 测试 1：常规搜索
    print("\n【测试 1】常规搜索")
    print(internet_search("Python 机器学习 入门"))

    # 测试 2：原始数据模式
    print("\n【测试 2】原始数据模式")
    raw_results = internet_search("SearXNG", format_output=False)
    print(f"  共获取 {len(raw_results)} 条结果")
    if raw_results:
        print(f"  第一条标题: {raw_results[0].get('title', 'N/A')}")
