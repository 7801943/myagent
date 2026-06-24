---
name: office-automation
description: 处理 Word、Excel、CSV 等办公文档的读取、编辑、和生成
allowed-tools:
  - file_read
  - file_edit
  - file_query
  - file_write
  - file_edit_table
---

# Office 文档自动化指南

## 适用场景

- 读取、创建或编辑 Word 文档（.docx）
- 读取、创建或编辑 Excel 工作簿（.xlsx / .xls）
- file_query子代理可以使用智能体处理文件内容查询，返回查询相关的引用段落和结果总结。
- file_write仅具备写入文本类文件功能（txt,md等）

## 工具优先级

- 读取文件内容时，优先使用 file_read，在上下文占比不多的情况下，应该优先读取所有行内容，不应该有截断（max_lines < 总行数），除非用户明确要求或文件特别大。
- 对已有 DOCX/XLSX 做精确修改时，优先使用 file_edit 或 file_edit_table。
- 需要生成复杂文档、批量处理或格式控制时，再使用 cli_execute 执行 Python 脚本。注意，用户可能会受到安全策略影响，不允许执行python脚本，你需要向用户说明。

## Word 操作建议

- 使用 python-docx 创建或编辑 .docx。
- 保留原文件时先写入新文件，再向用户说明输出路径。
- 修改已有内容前先读取文件，确认目标文本、表格位置和格式需求。

示例：

```python
from docx import Document

doc = Document()
doc.add_heading("标题", level=1)
doc.add_paragraph("正文内容")
doc.save("output.docx")
```

## Excel 操作建议

- 使用 openpyxl 处理 .xlsx，使用 csv 标准库处理 .csv。
- 修改单元格前先确认 sheet 名称、表头和数据范围。
- 对含公式或格式的工作簿，尽量只改目标单元格，避免重写整表。

示例：

```python
import openpyxl

wb = openpyxl.load_workbook("input.xlsx")
ws = wb.active
for row in ws.iter_rows(values_only=True):
    print(row)
wb.save("output.xlsx")
```

## 输出习惯

- 明确说明读取或生成的文件路径。
- 对表格数据给出简洁摘要，必要时列出关键行列。
- 如果用户要求批注、标色、替换文本或编辑表格，优先使用已有文件工具完成。

## 注意事项

- file_query处理速度较慢，除非涉及多个文件，否则优先使用file_read。
- 文件路径应该使用工作区虚拟路径，如 private/report.docx 或 public/report.docx。
- public/ 是公共协同目录，agent 只能读取，不能写入或修改。
- 不允许使用工具访问工作目录以外的目录。
