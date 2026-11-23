# ตัวแปรที่เก็บ และการกำหนดตัวแปรที่ต้องการเอง

มี 2 ส่วน:

- **A. ตัวแปรมาตรฐาน**: โปรแกรมเก็บให้อัตโนมัติจากทุกเอกสาร
- **B. ตัวแปรที่คุณกำหนดเอง**: เขียนไฟล์ spec (YAML/JSON) บอกว่าต้องการค่าอะไร แล้วผลจะออกมาใน `fields`

---

## A. ตัวแปรมาตรฐาน (เก็บอัตโนมัติ)

### ระดับเอกสาร

| ตัวแปร | ความหมาย |
|--------|----------|
| `schema_version` | เวอร์ชันของรูปแบบ JSON |
| `source.filename` / `size_bytes` / `sha256` | ชื่อไฟล์ ขนาด และ hash (ใช้ตรวจว่าเป็นไฟล์เดิมหรือไม่) |
| `source.pdf_version` / `encrypted` / `page_count` | เวอร์ชัน PDF, เข้ารหัสหรือไม่, จำนวนหน้า |
| `metadata.*` | Title, Author, Subject, Keywords, Creator, Producer, CreationDate, ModDate ... |
| `outline[]` | bookmark/สารบัญ: `title`, `level`, `page` |
| `form_fields[]` | ช่องฟอร์ม PDF ที่กรอกได้: `name`, `type` (Tx/Btn/Ch/Sig), `value` |
| `fields{}` | ตัวแปรที่กำหนดเองจาก spec (ส่วน B) |
| `warnings[]` | ปัญหาระดับเอกสาร |
| `extraction.*` | `status` (ok/partial/failed), `engines_used`, `pages_failed`, `missing_required_fields`, `duration_ms`, `tool_version`, `started_at` |

### ระดับหน้า (`pages[]`)

| ตัวแปร | ความหมาย |
|--------|----------|
| `number`, `width`, `height`, `rotation` | เลขหน้า, ขนาด (point), การหมุน |
| `engine` | engine ที่อ่านหน้านี้ได้ (pdfplumber / pypdfium2 / pypdf) |
| `text`, `char_count` | ข้อความทั้งหน้า และจำนวนตัวอักษร |
| `needs_ocr` | `true` = หน้าน่าจะเป็นภาพสแกน ไม่มีชั้นข้อความ |
| `blocks[]` | `id`, `type` (heading / paragraph / list_item / header / footer), `text`, `bbox`, `font`, `font_size`, `bold`, `heading_level` |
| `tables[]` | `id`, `bbox`, `n_rows`, `n_cols`, `rows` (array 2 มิติ) |
| `images[]` | `id`, `bbox`, `width_px`, `height_px` |
| `links[]` | `uri` (ลิงก์ภายนอก) หรือ `dest_page` (ลิงก์ภายในเอกสาร), `bbox` |
| `annotations[]` | `subtype` (Text, Highlight, FreeText ...), `contents`, `bbox` |
| `warnings[]` | ปัญหาเฉพาะหน้านี้ (engine ล้ม, ฟอนต์เพี้ยน, ต้อง OCR) |

`bbox` = `{x0, top, x1, bottom}` หน่วย point จุดเริ่มต้นอยู่มุมซ้ายบน
ดู schema เต็มได้ที่ [`schema/document.schema.json`](../schema/document.schema.json)

---

## B. กำหนดตัวแปรที่ต้องการเอง (spec)

```bash
pdfstruct invoice.pdf --spec examples/invoice.yaml --fields-only --pretty   # ดูเฉพาะตัวแปร (เหมาะกับลองทดสอบ spec)
pdfstruct invoice.pdf --spec examples/invoice.yaml -o out.json              # โครงสร้างเต็ม + fields
pdfstruct ./pdfs --spec examples/invoice.yaml --fields-only -o ./out        # ทั้งโฟลเดอร์
```

```python
from pdfstruct import extract
doc = extract("invoice.pdf", spec="examples/invoice.yaml")
doc.fields["total"].value      # 1236.5
```

### โครงสร้างไฟล์ spec

```yaml
version: 1
include: [metadata, pages.tables]   # (ไม่บังคับ) เลือกส่วนของโครงสร้างที่จะส่งออก
fields:
  - name: total                     # ชื่อตัวแปร (a-z, 0-9, _)
    type: number                    # string | number | integer | date | boolean | table
    label: ["ยอดรวม", "Total"]      # วิธีหาค่า (เลือก 1 วิธี ดูตารางด้านล่าง)
    pattern: '[\d,]+\.\d{2}'        # (ไม่บังคับ) ค่าต้องตรง regex นี้
    pages: [1, 2]                   # (ไม่บังคับ) ค้นเฉพาะหน้านี้
    all: false                      # true = เอาทุกค่าที่เจอเป็น list
    required: true                  # หาไม่เจอ = แจ้งใน missing_required_fields และ exit code 1
    default: 0                      # ค่าเมื่อหาไม่เจอ
    description: ยอดรวมสุทธิ        # คำอธิบาย (ไม่มีผลต่อการทำงาน)
```

### วิธีหาค่า (เลือกใช้อย่างใดอย่างหนึ่งต่อ 1 ตัวแปร)

| วิธี | ใช้เมื่อ | ตัวอย่าง | ทำงานอย่างไร |
|------|---------|----------|-------------|
| `label` | ค่ามีป้ายกำกับ เช่น "เลขที่: 123" | `label: ["เลขที่", "No."]` | หาป้าย แล้วลองตามลำดับ: (1) ข้อความต่อท้ายในบรรทัดเดียวกัน → (2) บรรทัดถัดไป → (3) ช่องขวาในตาราง → (4) ช่องใต้ในตาราง โดยใช้ค่าแรกที่ผ่าน `pattern` และแปลง `type` ได้ |
| `regex` | ค่ามีรูปแบบชัดเจน | `regex: '\b(\d{13})\b'` | ค้นทั้งเอกสาร ถ้ามี group จะใช้ group 1 |
| `columns` | ตารางรายการ (ใช้กับ `type: table`) | ดูด้านล่าง | หาตารางที่มีหัวคอลัมน์ตรงทุกตัว (ไม่สนตัวพิมพ์เล็กใหญ่และช่องว่าง) แล้วรวมแถวจากทุกหน้า |
| `source` | ค่าที่มีอยู่แล้วในผลลัพธ์ | `source: metadata.Title`, `source: form_fields.0.value` | อ่านจาก JSON ตาม path |

```yaml
  - name: items
    type: table
    columns:
      "รายการ": name                          # หัวคอลัมน์ในเอกสาร : ชื่อ key ใน JSON
      "จำนวน": { key: qty, type: integer }
      "ราคา":  { key: price, type: number }
# ผลลัพธ์: [{"name": "สินค้า ก", "qty": 1, "price": 500.0}, ...]
```

### การแปลงชนิดข้อมูล (`type`)

| type | รับได้ | ผลลัพธ์ |
|------|-------|---------|
| `string` | ข้อความใดก็ได้ที่ไม่ว่าง | `"INV-001"` |
| `number` | `1,234.50 บาท`, `฿ 99`, `(500.00)` (ค่าติดลบ), เลขไทย `๑,๐๐๐` | `1234.5` |
| `integer` | เหมือน number แต่ต้องเป็นจำนวนเต็ม | `3` |
| `date` | `15/03/2026`, `2026-03-15`, `5 มีนาคม 2569`, `5 มี.ค. 69`, `๕ มีนาคม ๒๕๖๙`, `15 Mar 2026`, `March 15, 2026` | `"2026-03-15"` (แปลง พ.ศ. → ค.ศ. อัตโนมัติ) |
| `boolean` | yes/no, true/false, ✓, ☑/☐, ใช่/ไม่ใช่, มี/ไม่มี | `true` |

> วันที่แบบตัวเลขจะอ่านเป็น **วัน/เดือน/ปี** (แบบไทยและยุโรป) ไม่ใช่ เดือน/วัน/ปี แบบอเมริกา

### ผลลัพธ์ของแต่ละตัวแปร

```json
"fields": {
  "total": {
    "name": "total", "found": true, "value": 1236.5,
    "raw": "1,236.50 THB", "page": 1, "method": "label:same-line", "error": null
  }
}
```

`raw` และ `page` ใช้ตรวจย้อนได้ว่าค่ามาจากตรงไหนของเอกสาร ส่วน `method` บอกว่าหาเจอด้วยวิธีใด
ถ้าหาไม่เจอจะได้ `found: false` พร้อม `error` ที่บอกเหตุผล

### ความเสถียร

- spec ถูกตรวจก่อนเริ่มอ่าน PDF: ชื่อซ้ำ, regex ผิด, ใช้หลายวิธีในตัวแปรเดียว จะได้ `SpecError` (exit code 2)
- ถ้าตัวแปรหนึ่งหาไม่เจอหรือเกิด error ตัวแปรอื่นและโครงสร้างเอกสารยังออกมาครบ
- ผลเป็น rule-based ทั้งหมด: spec เดิมกับ PDF เดิมให้ผลเหมือนเดิมทุกครั้ง

ตัวอย่างเต็ม: [`examples/invoice.yaml`](../examples/invoice.yaml)
