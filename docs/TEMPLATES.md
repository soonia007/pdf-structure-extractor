# Template: เรียนรู้จาก PDF ตัวอย่าง แล้วจำได้เองครั้งต่อไป

แทนที่จะเขียน spec เอง (ดู [FIELDS.md](FIELDS.md)) ให้โปรแกรม **เรียนรู้** จาก PDF ตัวอย่าง
ที่เป็นเอกสารแบบเดียวกัน เช่น ใบแจ้งหนี้จากผู้ขายรายเดียวกันหลาย ๆ ใบ

```
 PDF ตัวอย่าง 1..n ──► observe ──► หา label:value, ตาราง, หัวข้อ, ชนิดข้อมูล
                                          │
                                          ▼
                          build template (รวมทุกตัวอย่าง)
                          ├─ ค่าที่ "เปลี่ยน" ทุกใบ  → fields (ตัวแปร)
                          ├─ ข้อความที่ "เหมือนเดิม" ทุกใบ → anchors (ใช้จำเอกสาร)
                          └─ ตารางที่หัวคอลัมน์ตรงกัน  → table field
                                          │
                                          ▼
                     templates/<name>.template.json   (JSON อ่านได้ เก็บใน git ได้)
                                          │
 PDF ใหม่ ──► match (คะแนนจาก anchors) ──► ใช้ template ที่คะแนนสูงสุด ──► fields
```

## คำสั่ง

```bash
# 1) เรียนรู้จากตัวอย่าง (ยิ่งหลายใบยิ่งแม่น แนะนำอย่างน้อย 2-3 ใบ)
pdfstruct template learn acme_invoice inv1.pdf inv2.pdf --description "ใบแจ้งหนี้ ACME"

# 2) เพิ่มตัวอย่างภายหลัง ระบบจะเรียนรู้ใหม่จากตัวอย่างทั้งหมด (ไฟล์ซ้ำจะถูกข้าม)
pdfstruct template learn acme_invoice inv3.pdf

# 3) ดู / แก้ spec ที่เรียนรู้ได้
pdfstruct template show acme_invoice > acme.yaml      # แก้ชื่อตัวแปร ชนิดข้อมูล required ได้ตามต้องการ
pdfstruct template set-spec acme_invoice acme.yaml    # ใช้ spec ที่แก้เอง (การเรียนรู้ครั้งต่อไปจะไม่ทับ)

# 4) จำเอกสารใหม่และดึงค่าอัตโนมัติ
pdfstruct template match new.pdf                      # ดูคะแนนเทียบทุก template
pdfstruct new.pdf --auto-template --fields-only --pretty
pdfstruct ./inbox --auto-template --fields-only -o ./out   # ทั้งโฟลเดอร์ (แต่ละไฟล์เลือก template ของตัวเอง)

pdfstruct template list
pdfstruct template delete acme_invoice
```

ตัวเลือก `--store DIR` (หรือตัวแปรแวดล้อม `PDFSTRUCT_TEMPLATES`) ใช้กำหนดโฟลเดอร์เก็บ template (ค่าเริ่มต้น `./templates`)
และ `--threshold 0.6` คือคะแนนขั้นต่ำที่จะถือว่าเอกสารตรงกับ template

```python
from pdfstruct import TemplateStore, extract, extract_auto

store = TemplateStore("templates")
store.learn("acme_invoice", [extract("inv1.pdf"), extract("inv2.pdf")])
doc = extract_auto("new.pdf", store)
doc.extraction.template, doc.extraction.template_score   # ('acme_invoice', 1.0)
doc.fields["grand_total"].value                          # 10.0
```

## เรียนรู้อย่างไร

| ขั้น | กฎ |
|------|----|
| หา label:value | บรรทัดแบบ `ป้าย: ค่า`, `ป้าย ค่าตัวเลข` (เช่น `ยอดรวมทั้งสิ้น 1,001.00 บาท`) หรือ `ป้าย:` แล้วค่าอยู่บรรทัดถัดไป บรรทัดต้องยาวไม่เกิน 90 ตัวอักษรและป้ายไม่เกิน 6 คำ |
| ชนิดข้อมูล | date → boolean → number/integer → string ส่วนเลขยาว 10 หลักขึ้นไป (เลขภาษี เบอร์โทร) ถือเป็น string |
| รูปแบบ (pattern) | ค่าที่เป็นรหัส เช่น `INV-2026-001` จะได้ pattern `[A-Z]+\-\d+\-\d+` เพื่อกันการจับค่าผิด |
| ตัวแปร vs ข้อความคงที่ | ถ้ามี 2 ตัวอย่างขึ้นไปและค่าเหมือนกันทุกใบ (เช่นเลขภาษีของผู้ขาย) จะถือเป็น **anchor** ไม่ใช่ตัวแปร |
| ต้องเจอบ่อยแค่ไหน | ตัวแปรต้องเจออย่างน้อยครึ่งหนึ่งของตัวอย่าง ถ้าเจอครบทุกใบจะตั้ง `required: true` |
| ตาราง | หัวตารางที่ไม่ใช่ตัวเลขทุกช่อง และชนิดของแต่ละคอลัมน์ดูจากข้อมูลทุกแถว |
| ชื่อตัวแปร | ป้ายภาษาอังกฤษแปลงเป็น `snake_case` ส่วนป้ายภาษาไทยที่รู้จัก (เลขที่, วันที่, ยอดรวมทั้งสิ้น, เลขประจำตัวผู้เสียภาษี …) ได้ชื่ออังกฤษ นอกนั้นเป็น `field_2`, `field_3` … |
| การจำเอกสาร | คะแนน = สัดส่วนของ anchors ที่พบในเอกสารใหม่ (ถ้าจำนวนหน้าห่างจากตัวอย่างมากจะลดคะแนนลง) |

## ความปลอดภัยและความเสถียร

- **ไม่เก็บค่าจริงของเอกสาร** ไฟล์ template เก็บเฉพาะ hash ของค่า, ชนิด, และ pattern
  (มี test ยืนยัน) จึงเก็บใน git หรือแชร์ได้โดยไม่รั่วข้อมูลลูกค้า แต่ anchors (ข้อความคงที่) ยังอยู่ในไฟล์
- บันทึกไฟล์แบบ atomic (เขียนไฟล์ชั่วคราวก่อนแล้วจึงแทนที่) ถ้าโปรแกรมล่มกลางทาง template เดิมไม่เสีย
- ไฟล์ template ที่เสียจะถูกข้ามตอน `list`/`match` ไม่ทำให้ทั้งระบบล้ม
- ชื่อ template ถูกตรวจ (กัน path traversal เช่น `../`)
- spec ที่แก้มือ (`set-spec`) จะไม่ถูกการเรียนรู้ทับ แต่ anchors และตัวอย่างยังอัปเดตตามปกติ
- ผลเป็น deterministic: ตัวอย่างชุดเดิมได้ template เดิมเสมอ

## ข้อจำกัดตอนนี้

- ป้ายกับค่าต้องอยู่บรรทัดเดียวกันหรือบรรทัดถัดไป (ยังไม่รองรับป้ายกับค่าที่อยู่คนละคอลัมน์ของหน้า)
- ถ้าตัวอย่างมีใบเดียว ข้อความคงที่ (เช่นเลขภาษีผู้ขาย) จะยังถูกนับเป็นตัวแปร
- เอกสารสแกนต้องรอ OCR ใน Phase 3
