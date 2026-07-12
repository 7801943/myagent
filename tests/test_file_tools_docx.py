import asyncio

from docx import Document
from docx.enum.text import WD_COLOR_INDEX

from myagent.tools.api import generate_schema
from myagent.tools.builtin.file_edit import file_edit, file_edit_table
from myagent.tools.builtin.file_read import file_read
from myagent.tools.builtin.file_write import file_write


def run_tool(coro):
    return asyncio.run(coro)


def test_file_tool_path_schema_allows_workspace_paths():
    for fn in (file_read, file_write, file_edit, file_edit_table):
        description = generate_schema(fn)["properties"]["path"]["description"]
        assert "绝对路径" in description
        assert "workspace 可见路径" in description
        assert "工具层解析" in description


def test_file_read_docx_outputs_paragraphs_and_tables_in_body_order(tmp_path):
    path = tmp_path / "ordered.docx"
    doc = Document()
    doc.add_paragraph("Before table")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Alpha"
    table.cell(1, 1).text = "42"
    doc.add_paragraph("After table")
    doc.save(path)

    result = run_tool(file_read(str(path)))

    assert not result.is_error, result.content
    assert result.metadata["format"] == "docx"
    before = result.content.index("Before table")
    table_header = result.content.index("[表格 1]")
    row_1 = result.content.index("| Name | Value |")
    row_2 = result.content.index("| Alpha | 42 |")
    after = result.content.index("After table")
    assert before < table_header < row_1 < row_2 < after


def test_file_read_docx_table_only_document(tmp_path):
    path = tmp_path / "table_only.docx"
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Left"
    table.cell(0, 1).text = "Right"
    doc.save(path)

    result = run_tool(file_read(str(path)))

    assert not result.is_error, result.content
    assert "[表格 1]" in result.content
    assert "| Left | Right |" in result.content


def test_file_read_docx_line_range_can_slice_table_output(tmp_path):
    path = tmp_path / "slice.docx"
    doc = Document()
    doc.add_paragraph("Intro")
    table = doc.add_table(rows=2, cols=1)
    table.cell(0, 0).text = "First"
    table.cell(1, 0).text = "Second"
    doc.add_paragraph("Outro")
    doc.save(path)

    result = run_tool(file_read(str(path), start_line_or_page=2, end_line_or_page=4))

    assert not result.is_error, result.content
    assert "Intro" not in result.content
    assert "2 | [表格 1]" in result.content
    assert "3 | | First |" in result.content
    assert "4 | | Second |" in result.content
    assert "Outro" not in result.content


def test_file_edit_docx_can_highlight_table_cell_text(tmp_path):
    path = tmp_path / "table_edit.docx"
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "建设规模"
    table.cell(0, 1).text = "本期建设连云港地区级新一代调度系统，46台服务器。"
    doc.save(path)

    target = "本期建设连云港地区级新一代调度系统，46台服务器。"
    result = run_tool(file_edit(
        str(path),
        target_content=target,
        replacement_content=target,
        highlight="yellow",
    ))

    assert not result.is_error, result.content
    assert result.metadata["matched_lines"] == [2]
    updated = Document(str(path))
    runs = updated.tables[0].cell(0, 1).paragraphs[0].runs
    assert any(run.text == target and run.font.highlight_color == WD_COLOR_INDEX.YELLOW for run in runs)


def test_file_edit_docx_line_range_disambiguates_table_match(tmp_path):
    path = tmp_path / "line_range.docx"
    doc = Document()
    doc.add_paragraph("Target")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Target"
    doc.save(path)

    result = run_tool(file_edit(
        str(path),
        target_content="Target",
        replacement_content="Changed",
        start_line=3,
        end_line=3,
    ))

    assert not result.is_error, result.content
    assert result.metadata["matched_lines"] == [3]
    updated = Document(str(path))
    assert updated.paragraphs[0].text == "Target"
    assert updated.tables[0].cell(0, 1).text == "Changed"


def test_file_edit_docx_accepts_string_line_numbers_for_table_header(tmp_path):
    path = tmp_path / "string_line_numbers.docx"
    doc = Document()
    doc.add_paragraph("1.工程概况及建设规模")
    table = doc.add_table(rows=1, cols=3)
    table.cell(0, 0).text = "序号"
    table.cell(0, 1).text = "工程名称"
    table.cell(0, 2).text = "建设规模"
    doc.save(path)

    result = run_tool(file_edit(
        str(path),
        target_content="建设规模",
        replacement_content="建设规模",
        start_line="3",
        end_line="3",
        highlight="yellow",
    ))

    assert not result.is_error, result.content
    assert result.metadata["matched_lines"] == [3]
    updated = Document(str(path))
    body_runs = updated.paragraphs[0].runs
    header_runs = updated.tables[0].cell(0, 2).paragraphs[0].runs
    assert all(run.font.highlight_color is None for run in body_runs)
    assert any(
        run.text == "建设规模" and run.font.highlight_color == WD_COLOR_INDEX.YELLOW
        for run in header_runs
    )


def test_file_edit_docx_no_match_explains_display_table_text(tmp_path):
    path = tmp_path / "display_text.docx"
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Key"
    table.cell(0, 1).text = "Value"
    doc.save(path)

    result = run_tool(file_edit(
        str(path),
        target_content="| Key | Value |",
        replacement_content="Changed",
    ))

    assert result.is_error
    assert "展示层" in result.content
    assert "start_line/end_line" in result.content
    assert "目标单元格" in result.content
