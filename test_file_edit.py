import asyncio
from pathlib import Path
from docx import Document

# Create a sample docx
doc = Document()
doc.add_paragraph("Hello world, this is a test document.")
doc.save("test_edit.docx")

import sys
sys.path.append("/home/zhouxiang/myagent")

from myagent.tools.builtin.file_tools import file_edit

async def main():
    result = await file_edit(
        path="test_edit.docx",
        target_content="test document",
        replacement_content="test document",
        comment="This is my agent comment"
    )
    print("Result:")
    print(result.content)
    if result.is_error:
        print("Error details:", result.content)
        
asyncio.run(main())
