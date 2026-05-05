"""
热加载 + 天气查询演示脚本。

演示内容：
1. 创建 ToolRegistry + HotReloader
2. 启动热加载，自动扫描 tools_store 目录
3. 执行天气查询工具
4. 修改工具文件，观察热重载效果

运行方式：
    cd /home/zhouxiang/myagent
    python -m examples.demo_hot_reload
"""
import asyncio
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from myagent.tools.registry import ToolRegistry
from myagent.tools.loader import HotReloader


async def main():
    print("=" * 60)
    print("🔥 工具热加载 + 天气查询演示")
    print("=" * 60)

    # 1. 创建注册中心
    registry = ToolRegistry()

    # 2. 定义重载回调
    def on_reload(tool, event):
        print(f"\n[通知] 工具 {tool.name} ({event})")
        print(f"  描述: {tool.description}")
        print(f"  参数: {tool.parameters_schema}")

    # 3. 创建热加载器
    tools_store_dir = str(Path(project_root) / "myagent" / "tools" / "tools_store")
    reloader = HotReloader(
        registry,
        watch_dir=tools_store_dir,
        poll_interval=5.0,  # 演示用 5 秒间隔
        on_reload=on_reload,
        safe_mode=False,  # 天气工具用到 urllib，关闭安全检查
    )

    # 4. 启动热加载（首次立即扫描）
    print(f"\n📂 监控目录: {tools_store_dir}")
    print("⏳ 启动热加载...")
    await reloader.start()

    # 5. 查看已加载的工具
    print(f"\n📋 已注册工具: {registry.list_names()}")
    print(f"📁 已追踪文件: {reloader.watched_files}")

    # 6. 执行天气查询
    weather_tool = registry.get("query_weather")
    if weather_tool:
        print("\n" + "-" * 40)
        print("🌤️  执行天气查询: city=Beijing")
        print("-" * 40)
        result = await weather_tool.execute(city="Beijing")
        print(result.content)
        if result.metadata:
            print(f"(metadata: {result.metadata})")
    else:
        print("⚠️  未找到天气查询工具")

    # 7. 等待观察热重载（可以手动修改 weather_tool.py 测试）
    print("\n" + "-" * 40)
    print("💡 提示: 修改 weather_tool.py 后等待 5 秒可观察热重载")
    print("   按 Ctrl+C 退出")
    print("-" * 40)

    try:
        # 等待 30 秒观察
        await asyncio.sleep(30)
    except KeyboardInterrupt:
        pass

    # 8. 停止热加载
    await reloader.stop()
    print("\n✅ 演示结束")


if __name__ == "__main__":
    asyncio.run(main())