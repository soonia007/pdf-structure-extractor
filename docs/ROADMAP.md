# Roadmap — แบ่ง Phase การทำงาน

สถานะ: ✅ เสร็จ · 🚧 กำลังทำ · ⏳ ยังไม่เริ่ม

## Phase 1 — Foundation: อ่านได้ทุกไฟล์ ไม่พัง ✅

เป้าหมาย: รับ PDF อะไรก็ได้แล้วได้ JSON ที่ valid เสมอ (หรือ error ที่ชัดเจน)

- [x] ตรวจไฟล์ก่อน parse (magic bytes `%PDF-`, ขนาดไฟล์, sha256)
- [x] รองรับ PDF เข้ารหัส (`--password`) และ error `EncryptedPDFError`
- [x] metadata, outline/bookmarks, form fields, links, annotations (pypdf non-strict)
- [x] ข้อความรายหน้า + fallback 3 engine (pdfplumber → pypdfium2 → pypdf)
- [x] JSON model (pydantic) + JSON Schema + `schema_version`
- [x] CLI: ไฟล์เดียว / ทั้งโฟลเดอร์, exit code 0/1/2
- [x] `extract_isolated`: child process + hard timeout
- [x] Test: ไฟล์ปกติ, ไม่ใช่ PDF, เข้ารหัส, ไฟล์ขาด (truncated), หน้าว่าง, determinism, schema

**เกณฑ์ผ่าน:** ไม่มี uncaught exception บนชุดทดสอบ, JSON ผ่าน schema ทุกไฟล์

## Phase 2 — Layout & Structure 🚧 (พื้นฐานเสร็จแล้ว)

เป้าหมาย: บอกได้ว่าข้อความไหนคือ heading / ย่อหน้า / list / ตาราง / header-footer

- [x] words → lines → blocks (ตามระยะห่าง ฟอนต์ ขนาด)
- [x] heading + level จากขนาดฟอนต์ทั้งเอกสาร, bold detection
- [x] list item (bullet, ตัวเลข, ก. ข.)
- [x] header/footer จากข้อความซ้ำในขอบบน/ล่าง
- [x] ตาราง (แบบมีเส้น) และไม่ซ้ำข้อความตารางใน blocks
- [x] รูปภาพ (ตำแหน่ง, ขนาด px)
- [x] แยกบรรทัดเป็นชิ้น (segment) ตามช่องว่าง และไม่รวมข้อความคอลัมน์ซ้าย/ขวาเป็นบล็อกเดียว
- [x] ตารางไม่มีเส้น (จัดแนวตามหัวตาราง หยุดที่บรรทัดยอดรวม)
- [x] ตารางที่ต่อหน้าถัดไปโดยไม่มีหัวตาราง
- [ ] merged cells
- [ ] อ่าน Tagged PDF structure tree ถ้ามี (แม่นกว่า heuristic)
- [ ] สร้าง tree ของ section (heading → children) นอกเหนือจาก flat list
- [ ] Caption ของรูป/ตาราง

**เกณฑ์ผ่าน:** ความแม่นของ heading/table ≥ 90% บน corpus ทดสอบ (วัดด้วย F1)

## Phase 3 — Scanned PDF & ภาษาไทย 🚧 (OCR ใช้งานได้แล้ว)

เป้าหมาย: อ่านเอกสารสแกนและ PDF ไทยที่ฟอนต์เสียได้

- [x] OCR หน้า `needs_ocr = true` ด้วย Tesseract `tha+eng` (render ผ่าน pypdfium2 ที่ 300 DPI) — [OCR.md](OCR.md)
- [x] เก็บ `ocr_confidence` ต่อหน้า และ `engine: "ocr"`; ความมั่นใจของตัวแปรลดตาม OCR
- [x] ต่อสระ/วรรณยุกต์ไทยที่ OCR แยกออกมาให้เป็นคำเดียว
- [x] รวม Tesseract + ภาษาไทยในแอป Windows
- [ ] Pre-processing ภาพ: deskew, denoise, binarize
- [ ] Map Thai PUA (U+F700–U+F71A) → Unicode, reorder วรรณยุกต์/สระ
- [x] Auto-switch เป็น OCR เมื่อตัวอักษรเพี้ยนเกิน 30%
- [ ] (optional) PyThaiNLP สำหรับตัดคำ

**เกณฑ์ผ่าน:** CER ภาษาไทย ≤ 5% บนชุดเอกสารสแกนตัวอย่าง

## Phase 4 — Semantic: เข้าใจ "ประเภท" และ "ข้อมูล" ของเอกสาร 🚧 (ส่วน rule-based เสร็จแล้ว)

เป้าหมาย: บอกได้ว่าเอกสารเป็นแบบไหน และดึงข้อมูลสำคัญออกมาเป็น field

- [ ] จัดประเภทเอกสาร (ใบแจ้งหนี้, ใบเสร็จ, สัญญา, รายงาน, แบบฟอร์มราชการ ...)
- [x] Spec (YAML/JSON) ให้ผู้ใช้กำหนดตัวแปรที่ต้องการ → `fields` ([docs/FIELDS.md](FIELDS.md))
- [x] วิธีหาค่าแบบ rule-based: `label` (บรรทัดเดียวกัน/บรรทัดถัดไป/ช่องตาราง), `regex`, `columns` (ตาราง), `source`
- [x] แปลงชนิด: number (ลบด้วยวงเล็บ, เลขไทย), date (พ.ศ., เดือนไทย), boolean, integer, table
- [x] `required` / `default` / `pattern` / `pages` / `all` และบันทึกที่มาของทุกค่า (`raw`, `page`, `method`)
- [x] `include` เลือกส่วนของโครงสร้างที่จะส่งออก
- [x] Output `fields` แยกจากชั้น layout (ชั้น layout ยัง deterministic เสมอ)
- [x] **Template learning**: เรียนรู้ spec จาก PDF ตัวอย่าง, แยกตัวแปรออกจากข้อความคงที่, เรียนรู้เพิ่มทีละไฟล์ ([TEMPLATES.md](TEMPLATES.md))
- [x] จำเอกสารใหม่ด้วย anchors แล้วใช้ template ที่ตรงที่สุด (`--auto-template`)
- [ ] เรียนรู้จากการแก้ค่าของผู้ใช้ในแอป (feedback loop) → ปรับ label/pattern อัตโนมัติ
- [ ] ตรวจสอบค่าข้ามตัวแปร: ยอดรวมตรงกับผลรวมรายการ, checksum เลขผู้เสียภาษี 13 หลัก
- [x] หาค่าตามตำแหน่ง (ขวา/ใต้ป้าย) และให้คะแนนการจับคู่ป้าย; เรียนรู้ `position` และ `confidence`
- [x] แยกป้าย/ค่าจากข้อความที่คงที่ข้ามตัวอย่าง (ใช้กับเอกสารสแกนได้)
- [x] ตารางรายการที่ต่อหลายหน้าโดยไม่มีหัวตารางซ้ำ
- [ ] (optional) Vision-LLM fallback พร้อมอ้างอิง bbox ที่มาของทุกค่า

**เกณฑ์ผ่าน:** field accuracy ≥ 95% ต่อ template ที่รองรับ

## Phase 5 — Production & Reliability ⏳

เป้าหมาย: ใช้งานจริงในระบบ ปริมาณมาก เสถียร ตรวจสอบได้

- [x] แอปบนเครื่องตามดีไซน์ใน [APP_DESIGN.md](APP_DESIGN.md): อัปโหลด, ดูผลบนหน้า PDF, สอน template, คลัง template ([DESKTOP_APP.md](DESKTOP_APP.md))
- [x] Build แอป Windows / macOS / Linux ด้วย PyInstaller ใน GitHub Actions
- [ ] Code signing (Windows Authenticode, Apple notarization) เพื่อไม่ให้ระบบเตือนตอนเปิดแอป
- [ ] REST API (FastAPI) + job queue สำหรับไฟล์ใหญ่
- [ ] Batch แบบขนาน (process pool) + จำกัด memory/เวลา ต่อไฟล์
- [ ] Regression corpus + golden JSON, diff ใน CI ทุก PR
- [ ] Fuzz testing (ไฟล์เสีย/แปลก) — ต้องไม่มี crash
- [ ] Logging/metrics: เวลาต่อหน้า, อัตรา fallback, อัตรา OCR
- [ ] Docker image (รวม Tesseract + ภาษาไทย)
- [ ] เอกสาร API, versioning ของ schema (semver: เพิ่ม field = minor, เปลี่ยน/ลบ = major)
