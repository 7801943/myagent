"""
兼容性垫片：wrapper.py 已合并入 base.py。
所有功能由 FunctionTool / make_tool 提供。
"""
from myagent.tools.base import FunctionTool as _FunctionTool

# 向后兼容
def wrap_function(func, *, name=None, description=None):
    """已弃用：请使用 FunctionTool(func, name=..., description=...) 或 @make_tool。"""
    return _FunctionTool(func, name=name, description=description)

__all__ = ["wrap_function"]