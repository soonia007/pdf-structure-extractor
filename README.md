# pdf-structure-extractor (`pdfstruct`)

โปรแกรมอ่านไฟล์ PDF แล้วแปลงเป็น **JSON** ที่บอกโครงสร้างของเอกสาร — ข้อความ, หัวข้อ (heading + level),
ย่อหน้า, รายการ (list), ตาราง, รูปภาพ, header/footer, ลิงก์, annotation, bookmarks, form fields และ metadata
โดยออกแบบให้ **เสถียรที่สุด**: ใช้หลาย engine สำรองกันรายหน้า, แยก process พร้อม timeout,
ไม่กลืน error, และให้ผลลัพธ์เหมือนเดิมทุกครั้ง (deterministic)

- วิธีการอ่าน PDF แบบต่าง ๆ และเหตุผลที่เลือก → [docs/METHODS.md](docs/METHODS.md)
- แผนการทำงานแบ่งเป็น Phase → [docs/ROADMAP.md](docs/ROADMAP.md)
- ตัวแปรที่เก็บ + **กำหนดตัวแปรที่ต้องการเอง (spec)** → [docs/FIELDS.md](docs/FIELDS.md)
- **Template: เรียนรู้จาก PDF ตัวอย่างแล้วจำเอกสารได้เอง** → [docs/TEMPLATES.md](docs/TEMPLATES.md)
- **อ่านเอกสารสแกน (OCR ไทย+อังกฤษ)** → [docs/OCR.md](docs/OCR.md)
- **แอปบนเครื่อง (หน้าต่างแอป Windows / macOS / Linux)** → [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md)
- ดีไซน์หน้าแอป → [docs/APP_DESIGN.md](docs/APP_DESIGN.md)
- JSON Schema ของผลลัพธ์ → [schema/document.schema.json](schema/document.schema.json)

## ใช้งานแบบแอป (ง่ายที่สุด)

ดาวน์โหลดแอปที่ build แล้วจากแท็บ **Actions → Build desktop app** (หรือหน้า **Releases**)
แล้วเปิด `pdfstruct.exe` (Windows) หรือ `pdfstruct.app` (macOS) ได้ทันทีโดยไม่ต้องติดตั้ง Python
หรือรันจากซอร์สด้วยคำสั่ง `pip install -e ".[app]" && pdfstruct-app` ดูรายละเอียดใน [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md)

## ติดตั้ง (แบบ command line / Python library)

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

# Prompt สำเร็จรูปสำหรับนำข้อมูลไปใช้กับ AI (summary / validate / spreadsheet / database / code / qa / map)
pdfstruct invoice.pdf --auto-template --prompt validate

# ทุกตาราง (รวมข้ามหน้าแล้ว) เป็นไฟล์ Excel
pdfstruct statement.pdf --excel statement.xlsx -o statement.json

# ดึงเฉพาะตัวแปรที่ต้องการ (กำหนดใน spec)
pdfstruct invoice.pdf --spec examples/invoice.yaml --fields-only --pretty

# ดู JSON Schema
pdfstruct --schema
```

Exit code: `0` = สำเร็จ, `1` = สำเร็จบางส่วน (บางหน้าอ่านไม่ได้ หรือหาตัวแปร `required` ไม่เจอ),
`2` = error (ไม่ใช่ PDF, รหัสผิด, spec ผิด, ...)

## กำหนดตัวแปรที่ต้องการ

```yaml
# my-spec.yaml
fields:
  - name: invoice_no
    label: ["เลขที่", "Invoice No"]
    required: true
  - name: issue_date
    type: date            # "5 มีนาคม 2569" -> "2026-03-05"
    label: "วันที่"
  - name: total
    type: number          # "1,236.50 บาท" -> 1236.5
    label: ["ยอดรวมทั้งสิ้น", "Total"]
  - name: items
    type: table
    columns: { "รายการ": name, "จำนวน": { key: qty, type: integer } }
```

```bash
pdfstruct invoice.pdf --spec my-spec.yaml --fields-only --pretty
```

```json
{ "fields": { "invoice_no": "TX-0042", "issue_date": "2026-03-05", "total": 1001.0,
              "items": [ { "name": "สินค้า ก", "qty": 1 }, { "name": "สินค้า ข", "qty": 4 } ] } }
```

ดูวิธีทั้งหมด (`label` / `regex` / `columns` / `source`) และชนิดข้อมูลได้ที่ [docs/FIELDS.md](docs/FIELDS.md)

## ให้โปรแกรมเรียนรู้ template เอง

```bash
pdfstruct template learn acme_invoice inv1.pdf inv2.pdf     # เรียนรู้จากตัวอย่าง -> templates/acme_invoice.template.json
pdfstruct new.pdf --auto-template --fields-only --pretty    # จำได้ว่าเป็นเอกสารแบบไหน แล้วดึงค่าให้
```

หรือ **ครอปเอง** ในแอป: ลากกรอบบนหน้า PDF แล้วตั้งชื่อตัวแปร ([docs/DESKTOP_APP.md](docs/DESKTOP_APP.md#ครอปตัวแปรเอง-สอนด้วยตัวเอง))

ข้อความที่เหมือนกันทุกใบจะใช้เป็นตัวจำเอกสาร (anchor) ส่วนค่าที่เปลี่ยนไปในแต่ละใบจะเป็นตัวแปร
ตารางจะถูกจับคู่จากหัวคอลัมน์ ไฟล์ template ไม่เก็บค่าจริงของเอกสาร ดูรายละเอียดใน [docs/TEMPLATES.md](docs/TEMPLATES.md)

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
  "schema_version": "1.4.0",
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
  fields.py      # ตัวแปรที่ผู้ใช้กำหนด (spec): label / regex / table / source + แปลงชนิด
  templates.py   # เรียนรู้ template จากตัวอย่าง, เก็บ, จับคู่เอกสารใหม่
  prompts.py     # ส่งออก JSON/CSV และ Prompt สำเร็จรูปสำหรับ AI
  kv.py          # หาป้าย/ค่าจากตำแหน่งบนหน้า, ตารางไม่มีเส้น
  ocr.py         # OCR หน้าสแกนด้วย Tesseract
  export.py      # รวมตารางข้ามหน้า, ส่งออก Excel/CSV แบบกำหนดชนิดข้อมูลตายตัว
  app/           # แอปบนเครื่อง: server.py (API ภายในเครื่อง), desktop.py (หน้าต่าง), static/ (UI)
packaging/       # build แอปด้วย PyInstaller
  models.py      # JSON model (pydantic) = schema
  cli.py         # command line
tests/           # สร้าง PDF ทดสอบด้วย reportlab ตอนรัน (ไม่เก็บไฟล์ binary)
docs/            # METHODS.md, ROADMAP.md, FIELDS.md, TEMPLATES.md, APP_DESIGN.md, DESKTOP_APP.md
examples/        # ตัวอย่าง spec
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
