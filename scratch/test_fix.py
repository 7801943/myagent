"""Minimal verification test for the FunctionTool.__init__() fix.

Tests _compute_entry_point logic and the new process_runner handling,
without importing the full myagent package.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Direct imports - avoid pulling in the full package
from myagent.tools.base import BaseTool, FunctionTool, ToolResult
from myagent.tools.registry import ToolRegistry
from myagent.tools.loader import ToolLoader


def test_entry_point():
    """Test 1: Verify _compute_entry_point produces importable entry."""
    from myagent.tools.hot_reloader import HotReloader
    
    registry = ToolRegistry()
    reloader = HotReloader(registry, safe_mode=False)
    
    tool = reloader._load_tool_from_dir({
        'entry_files': [Path('myagent/tools/tools_store/search/search_tool.py')],
        'name': 'search',
        'dir': Path('myagent/tools/tools_store/search'),
    })
    assert tool is not None, "Tool failed to load"
    print(f'[OK] Tool loaded: name={tool.name}')
    
    entry = tool._entry_point
    assert entry is not None, "_entry_point is None!"
    print(f'[OK] _entry_point={entry}')
    
    # Verify importable
    module_path, attr_name = entry.rsplit(':', 1)
    import importlib
    mod = importlib.import_module(module_path)
    obj = getattr(mod, attr_name)
    print(f'[OK] Importable: {module_path}:{attr_name} -> {type(obj).__name__}')
    
    # CRITICAL: should NOT be a class that requires args
    assert not isinstance(obj, type), f"Entry resolves to a class {obj}, will fail!"
    assert callable(obj), f"Entry is not callable: {type(obj)}"
    print(f'[OK] Is a callable function: {obj.__name__}')
    return obj


def test_process_runner_logic(obj):
    """Test 2: Simulate process_runner's new instantiation logic."""
    # This is the NEW logic from process_runner.py
    if isinstance(obj, type):
        tool = obj()
        print(f'[OK] Class instantiated: {type(tool).__name__}')
    elif isinstance(obj, BaseTool):
        tool = obj
        print(f'[OK] Already BaseTool instance')
    else:
        tool = FunctionTool(obj)
        print(f'[OK] FunctionTool wrapper created for: {obj.__name__}')
    
    # Try executing
    import asyncio
    result = asyncio.run(tool.execute(query="test query", max_results=1))
    print(f'[OK] Execute returned: is_error={result.is_error}')
    print(f'     content[:80]={result.content[:80]}')
    return True


def test_weather_tool():
    """Test 3: Weather tool entry point."""
    from myagent.tools.hot_reloader import HotReloader
    
    registry = ToolRegistry()
    reloader = HotReloader(registry, safe_mode=False)
    
    tool = reloader._load_tool_from_dir({
        'entry_files': [Path('myagent/tools/tools_store/weather/weather_tool.py')],
        'name': 'weather',
        'dir': Path('myagent/tools/tools_store/weather'),
    })
    assert tool is not None, "Weather tool failed to load"
    print(f'[OK] Weather tool loaded: name={tool.name}')
    print(f'[OK] _entry_point={tool._entry_point}')
    
    entry = tool._entry_point
    module_path, attr_name = entry.rsplit(':', 1)
    import importlib
    mod = importlib.import_module(module_path)
    obj = getattr(mod, attr_name)
    assert callable(obj) and not isinstance(obj, type)
    print(f'[OK] Importable as function: {obj.__name__}')


if __name__ == '__main__':
    print("=== Test 1: Entry point computation ===")
    obj = test_entry_point()
    
    print("\n=== Test 2: Process runner instantiation logic ===")
    test_process_runner_logic(obj)
    
    print("\n=== Test 3: Weather tool ===")
    test_weather_tool()
    
    print("\n✅ All tests passed!")