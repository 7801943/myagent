"""内置工具集合：文件读写。"""
from myagent.tools.builtin.file_diff import file_diff
from myagent.tools.builtin.file_edit import file_edit, file_edit_table
from myagent.tools.builtin.file_read import file_read
from myagent.tools.builtin.file_query import file_query
from myagent.tools.builtin.file_write import file_write

__all__ = ["file_read", "file_query", "file_write", "file_edit", "file_edit_table", "file_diff"]
