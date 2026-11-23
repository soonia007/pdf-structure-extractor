# วิธีการอ่าน PDF — เปรียบเทียบและข้อแนะนำ

PDF ไม่ได้เก็บ "โครงสร้างเอกสาร" (หัวข้อ ย่อหน้า ตาราง) ไว้ตรง ๆ ส่วนใหญ่เก็บแค่
"วาดตัวอักษร X ด้วยฟอนต์ Y ที่ตำแหน่ง (x, y)" ดังนั้นการแปลงเป็น JSON ที่มีโครงสร้าง
ต้อง **อนุมาน (infer)** โครงสร้างจากตำแหน่ง ฟอนต์ และขนาด

## 1. วิธีหลักในการอ่าน PDF

| # | วิธี | ใช้กับ | ข้อดี | ข้อเสีย | เครื่องมือ |
|---|------|--------|------|---------|-----------|
| 1 | **Text-layer extraction** (อ่านชั้นข้อความโดยตรง) | PDF ที่สร้างจากโปรแกรม (Word, Excel, ระบบออกเอกสาร) | เร็ว แม่นยำ 100% ถ้าฟอนต์มี ToUnicode, ได้พิกัดและฟอนต์ | ไม่ได้ลำดับการอ่าน/โครงสร้างให้ ต้องคำนวณเอง | pdfplumber / pdfminer.six, pypdfium2, PyMuPDF, pypdf |
| 2 | **Layout analysis แบบ heuristic** (จัดกลุ่มคำ → บรรทัด → บล็อก, ดูขนาดฟอนต์) | ต่อจากวิธีที่ 1 | Deterministic, อธิบายได้, ไม่ต้องใช้ GPU | ต้องจูนกับเอกสารที่หลายคอลัมน์/ซับซ้อน | โค้ดใน `layout.py` |
| 3 | **Table extraction** (ใช้เส้นตาราง/การจัดแนวข้อความ) | เอกสารที่มีตาราง | ได้ข้อมูลเป็นแถว/คอลัมน์ | ตารางไม่มีเส้นตรวจยาก | pdfplumber, Camelot, Tabula |
| 4 | **Tagged PDF / Structure tree** (อ่าน `/StructTreeRoot`) | PDF ที่สร้างแบบ accessible (PDF/UA) | ได้โครงสร้างจริงจากผู้สร้าง แม่นที่สุด | เอกสารส่วนน้อยมี tag ที่ถูกต้อง | pikepdf, PyMuPDF |
| 5 | **OCR** (render เป็นภาพแล้วอ่านตัวอักษร) | PDF สแกน / ภาพถ่าย / ฟอนต์เสีย | อ่านได้ทุกอย่างที่ "มองเห็น" | ช้า มีความผิดพลาด ต้องติดตั้ง engine | Tesseract (`tha+eng`), OCRmyPDF, PaddleOCR, EasyOCR |
| 6 | **ML layout models** (ตรวจจับ heading/table/figure จากภาพหน้า) | เอกสารซับซ้อน หลากหลาย layout | แม่นกับ layout ที่ heuristic ทำไม่ได้ | หนัก ต้องใช้โมเดล ผลลัพธ์อาจเปลี่ยนตามเวอร์ชันโมเดล | Docling, Unstructured, LayoutParser, Marker, Surya |
| 7 | **Vision-LLM** (ส่งภาพหน้าให้ LLM สรุปเป็น JSON) | ดึงข้อมูลเชิงความหมาย (key-value, ใบแจ้งหนี้) | ยืดหยุ่นสูงมาก เข้าใจบริบท | ไม่ deterministic, มีต้นทุน, อาจ "แต่ง" ข้อมูล | Claude, GPT-4o, Gemini |
| 8 | **Form fields (AcroForm)** | แบบฟอร์ม PDF ที่กรอกได้ | ได้ชื่อฟิลด์และค่าตรง ๆ | ใช้ได้เฉพาะฟอร์มที่เป็น AcroForm | pypdf, pikepdf |

## 2. แนวทางที่โปรเจกต์นี้เลือก (เน้นความเสถียร)

**Hybrid pipeline แบบ "deterministic ก่อน, ฉลาดทีหลัง":**

```
            ┌──────────────── validate (magic bytes, size, encryption) ───────────────┐
            ▼                                                                         │
  pypdf (non-strict) ──► metadata, outline, form fields, links, annotations           │
            │                                                                         │
  ต่อหน้า:  pdfplumber ──(ล้ม)──► pypdfium2 ──(ล้ม)──► pypdf ──(ล้ม)──► page ว่าง+warning
            │
            ▼
  words → lines → blocks → classify (heading / paragraph / list / header / footer)
            │
            ▼
  ไม่มีชั้นข้อความ? ──► needs_ocr = true ──► (Phase 3) OCR ภาษาไทย+อังกฤษ
            │
            ▼
  JSON ตาม schema (มี schema_version) + warnings + status ok/partial/failed
```

### หลักการความเสถียร (Stability principles)

1. **หลาย engine + fallback รายหน้า** — หน้าที่พังไม่ทำให้ทั้งไฟล์พัง
2. **แยก process + timeout** (`extract_isolated` / `--isolate`) — ป้องกัน parser ค้าง/กินแรม
3. **ไม่กลืน error** — ทุกปัญหาอยู่ใน `warnings` และ `extraction.status`
4. **Deterministic** — รันซ้ำได้ผลเหมือนเดิม (มี test ยืนยัน) ไม่ใช้ ML ในเส้นทางหลัก
5. **Schema มีเวอร์ชัน** — `schema_version` + JSON Schema ใน `schema/` ตรวจใน CI
6. **Normalize ข้อความ** — NFC, ligature, control chars, `(cid:N)`, สระอำภาษาไทย
7. **ตรวจจับข้อความเสีย** — นับ Private-Use-Area chars และ glyph ที่ map ไม่ได้ → แจ้งเตือนให้ใช้ OCR
8. **Regression corpus** — (Phase 5) ชุด PDF จริงพร้อม JSON ที่คาดหวัง เทียบทุกครั้งที่แก้โค้ด

## 3. ปัญหาเฉพาะ PDF ภาษาไทยที่ต้องระวัง

| ปัญหา | อาการ | วิธีแก้ |
|-------|-------|--------|
| ฟอนต์ไม่มี ToUnicode map | ได้ตัวอักษรขยะ / Private Use Area (U+F700–U+F71A) | map PUA → Unicode ไทย หรือใช้ OCR |
| สระอำแตกเป็น `ํ` + `า` | ค้นหาคำไม่เจอ | normalize (ทำแล้วใน `normalize.py`) |
| วรรณยุกต์/สระลอยสลับลำดับ | `ก่ี` แทน `กี่` | reorder ตามกฎ Thai Unicode (Phase 3) |
| ไม่มีช่องว่างระหว่างคำ | ตัดคำไม่ได้ | ใช้ PyThaiNLP ถ้าต้องการ word-level (optional) |
| OCR ภาษาไทย | Tesseract ต้องใช้ `tha` traineddata | `apt install tesseract-ocr-tha` |
