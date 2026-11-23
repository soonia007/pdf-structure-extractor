# pdf-structure-extractor (`pdfstruct`)

โปรแกรมอ่านไฟล์ PDF แล้วแปลงเป็น **JSON** ที่บอกโครงสร้างของเอกสาร — ข้อความ, หัวข้อ (heading + level),
ย่อหน้า, รายการ (list), ตาราง, รูปภาพ, header/footer, ลิงก์, annotation, bookmarks, form fields และ metadata
โดยออกแบบให้ **เสถียรที่สุด**: ใช้หลาย engine สำรองกันรายหน้า, แยก process พร้อม timeout,
ไม่กลืน error, และให้ผลลัพธ์เหมือนเดิมทุกครั้ง (deterministic)

- วิธีการอ่าน PDF แบบต่าง ๆ และเหตุผลที่เลือก → [docs/METHODS.md](docs/METHODS.md)
- แผนการทำงานแบ่งเป็น Phase → [docs/ROADMAP.md](docs/ROADMAP.md)
- JSON Schema ของผลลัพธ์ → [schema/document.schema.json](schema/document.schema.json)

## ติดตั้ง

```bash
git clone https://github.com/soonia007/pdf-structure-extractor.git
cd pdf-structure-extractor
pip install -e ".[dev]"
```

ต้องการ Python 3.10+

## ใช้งาน

```bash
# ไฟล์เดียว
pdfstruct input.pdf -o output.json --pretty

# ทั้งโฟลเดอร์ (ได้ .json คู่กับแต่ละ .pdf)
pdfstruct ./pdfs -o ./out

# PDF ไม่น่าไว้ใจ / ไฟล์ใหญ่: แยก process + timeout
pdfstruct input.pdf --isolate --timeout 60

# PDF ที่มีรหัสผ่าน
pdfstruct secret.pdf --password 1234

# ดู JSON Schema
pdfstruct --schema
```

Exit code: `0` = สำเร็จ, `1` = สำเร็จบางส่วน (บางหน้าอ่านไม่ได้), `2` = error (ไม่ใช่ PDF, รหัสผิด, ...)

ใช้ใน Python:

```python
from pdfstruct import extract, extract_isolated

doc = extract("input.pdf")
for page in doc.pages:
    for block in page.blocks:
        print(page.number, block.type, block.heading_level, block.text)
print(doc.model_dump_json(indent=2))
```

## ตัวอย่างผลลัพธ์ (ย่อ)

```json
{
  "schema_version": "1.0.0",
  "source": { "filename": "sample.pdf", "size_bytes": 4821, "sha256": "…", "pdf_version": "1.4", "encrypted": false, "page_count": 2 },
  "metadata": { "Title": "Sample Report", "Author": "Tester" },
  "outline": [ { "title": "Introduction", "level": 1, "page": 1 } ],
  "form_fields": [],
  "pages": [
    {
      "number": 1, "width": 595.28, "height": 841.89, "rotation": 0,
      "engine": "pdfplumber", "needs_ocr": false,
      "text": "…",
      "blocks": [
        { "id": "p1-b1", "type": "header",    "text": "ACME Corp - Quarterly Report", "bbox": { "x0": 72, "top": 801, "x1": 196, "bottom": 811 } },
        { "id": "p1-b2", "type": "heading",   "heading_level": 1, "text": "Introduction", "font": "Helvetica-Bold", "font_size": 18 },
        { "id": "p1-b3", "type": "paragraph", "text": "This is the first paragraph…" },
        { "id": "p1-b6", "type": "list_item", "text": "• First point" }
      ],
      "tables": [ { "id": "p1-t1", "n_rows": 3, "n_cols": 3, "rows": [["Item","Qty","Price"],["Apple","3","10.00"],["Banana","5","4.50"]] } ],
      "images": [],
      "links": [ { "uri": "https://example.com" } ],
      "warnings": []
    }
  ],
  "warnings": [],
  "extraction": { "tool_version": "0.1.0", "status": "ok", "engines_used": ["pdfplumber"], "pages_failed": [], "duration_ms": 85 }
}
```

พิกัดทั้งหมดเป็นหน่วย point (1/72 นิ้ว) โดยจุดเริ่มต้นอยู่ **มุมซ้ายบน** ของหน้า

## โครงสร้างโปรเจกต์

```
src/pdfstruct/
  extractor.py   # pipeline หลัก + fallback engines + isolation
  layout.py      # words → lines → blocks → ประเภทบล็อก
  normalize.py   # ทำความสะอาดข้อความ (Unicode, ไทย, ligature, cid)
  models.py      # JSON model (pydantic) = schema
  cli.py         # command line
tests/           # สร้าง PDF ทดสอบด้วย reportlab ตอนรัน (ไม่เก็บไฟล์ binary)
docs/            # METHODS.md, ROADMAP.md
schema/          # JSON Schema ที่ generate จาก models
```

## พัฒนา

```bash
pytest
ruff check src tests
pdfstruct --schema > schema/document.schema.json   # เมื่อแก้ models.py
```

## License

MIT
