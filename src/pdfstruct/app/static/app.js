/* pdfstruct desktop UI — vanilla JS, no build step, works offline.
   All document text is inserted with textContent (never innerHTML). */
'use strict';

const view = document.getElementById('view');
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

// ------------------------------------------------------------------ helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'class') el.className = v;
    else if (k === 'style') el.style.cssText = v;
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
// replaceChildren() neither flattens arrays nor skips null/false: always go through put()
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}
const SVG_NS = 'http://www.w3.org/2000/svg';
function icon(d, size = 18, color = 'currentColor') {
  const s = document.createElementNS(SVG_NS, 'svg');
  s.setAttribute('width', size); s.setAttribute('height', size); s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('fill', 'none'); s.setAttribute('stroke', color); s.setAttribute('stroke-width', '2');
  s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round'); s.setAttribute('aria-hidden', 'true');
  for (const part of [].concat(d)) { const p = document.createElementNS(SVG_NS, 'path'); p.setAttribute('d', part); s.append(p); }
  return s;
}
const I = {
  back: 'M15 18l-6-6 6-6', plus: 'M12 5v14M5 12h14', upload: ['M12 16V4M7 9l5-5 5 5', 'M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3'],
  check: 'M5 12l5 5 9-11', lock: ['M6 11h12v10H6z', 'M8 11V7a4 4 0 0 1 8 0v4'], warn: ['M12 9v4M12 17h.01', 'M10.3 3.9L2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'],
  left: 'M15 18l-6-6 6-6', right: 'M9 18l6-6-6-6',
};

async function api(method, path, body, headers = {}) {
  const opts = { method, headers: { ...headers }, credentials: 'same-origin' };
  if (body !== undefined) {
    if (body instanceof Blob || typeof body === 'string') opts.body = body;
    else { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  }
  const res = await fetch('/api' + path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.message) || res.statusText);
  return data;
}

let toastTimer = null;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}
function store(key, val) {
  try { if (val === undefined) return localStorage.getItem(key); localStorage.setItem(key, val); } catch { return null; }
  return null;
}
const fmtSize = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB');
const pct = (x) => Math.round(x * 100) + '%';
const fold = (s) => String(s || '').replace(/\s+/g, '').toLowerCase();

function statusOf(s) {
  if (s.error) return ['bad', 'อ่านไม่ได้'];
  if (s.status === 'failed') return ['bad', 'อ่านไม่ได้'];
  if (s.missing_required && s.missing_required.length) return ['warn', 'ขาดตัวแปรบังคับ'];
  if (s.needs_ocr) return ['warn', 'ต้อง OCR'];
  if (s.status === 'partial') return ['warn', 'อ่านได้บางส่วน'];
  return ['ok', 'สำเร็จ'];
}
const statusEl = (s) => { const [k, label] = statusOf(s); return h('span', { class: 'status ' + k }, label); };

async function uploadFiles(files, autoTemplate) {
  const results = [];
  for (const f of files) {
    if (!/\.pdf$/i.test(f.name) && f.type !== 'application/pdf') { toast(`ข้าม ${f.name}: ไม่ใช่ไฟล์ PDF`); continue; }
    const r = await api('POST', `/documents?auto_template=${autoTemplate ? 1 : 0}`, f,
      { 'X-Filename': encodeURIComponent(f.name), 'Content-Type': 'application/pdf' });
    results.push(r);
  }
  return results;
}

function specDialog(name, yamlText, onSaved) {
  const err = h('div', { class: 'note bad', hidden: true });
  const area = h('textarea', { class: 'input', rows: 20, 'aria-label': 'spec YAML', spellcheck: 'false' });
  area.value = yamlText;
  const dlg = h('dialog', { 'aria-labelledby': 'dlg-title' },
    h('div', { class: 'dlg' },
      h('h2', { class: 'h', id: 'dlg-title' }, 'แก้ spec: ', h('span', { class: 'mono' }, name)),
      h('p', { class: 'muted small', style: 'margin:0' }, 'เมื่อบันทึก Template จะถูกตั้งเป็น "แก้ spec เอง" การเรียนรู้รอบต่อไปจะไม่ทับ spec นี้ (ดู docs/FIELDS.md)'),
      area, err,
      h('div', { class: 'row', style: 'justify-content:flex-end' },
        h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'ยกเลิก'),
        h('button', { class: 'btn primary', type: 'button', onclick: async () => {
          try {
            const res = await fetch(`/api/templates/${encodeURIComponent(name)}/spec`, { method: 'PUT', body: area.value, credentials: 'same-origin' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.message);
            dlg.close(); toast('บันทึก spec แล้ว'); onSaved && onSaved(data);
          } catch (e) { err.hidden = false; err.textContent = e.message; }
        } }, 'บันทึก spec'))));
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg); dlg.showModal(); area.focus();
}

// ------------------------------------------------------------------ home
async function renderHome() {
  let auto = store('autoTemplate') !== '0';
  const fileInput = h('input', { type: 'file', id: 'pick', accept: 'application/pdf,.pdf', multiple: true, class: 'file-input' });
  const tableBox = h('div', {});
  const statsBox = h('section', { class: 'stats', 'aria-label': 'สรุป' });
  const pending = [];

  const drop = h('section', { class: 'drop', 'aria-label': 'อัปโหลดไฟล์' },
    h('div', { class: 'drop-icon' }, icon(I.upload, 28, '#1F4FC9')),
    h('div', { class: 'drop-title' }, 'ลากไฟล์ PDF มาวางที่นี่'),
    h('div', { class: 'muted' }, 'หรือเลือกไฟล์จากเครื่อง · หลายไฟล์พร้อมกันได้ · ไฟล์อยู่ในเครื่องนี้เท่านั้น'),
    h('div', { class: 'row', style: 'justify-content:center' },
      h('label', { for: 'pick', class: 'btn primary' }, 'เลือกไฟล์'), fileInput,
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: auto, onchange: (e) => { auto = e.target.checked; store('autoTemplate', auto ? '1' : '0'); } }), 'จำ Template อัตโนมัติ')));

  async function handle(files) {
    if (!files.length) return;
    for (const f of files) pending.push(f.name);
    await refresh();
    try {
      const res = await uploadFiles([...files], auto);
      toast(`อ่านเสร็จ ${res.length} ไฟล์`);
      if (res.length === 1 && !res[0].error) location.hash = '#/doc/' + res[0].id;
    } catch (e) { toast('อัปโหลดไม่สำเร็จ: ' + e.message); }
    pending.length = 0;
    await refresh();
  }
  fileInput.addEventListener('change', () => handle(fileInput.files));
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); handle(e.dataTransfer.files); });

  async function refresh() {
    const [docs, tpls] = await Promise.all([api('GET', '/documents'), api('GET', '/templates')]);
    const readable = docs.filter((d) => !d.error);
    const matched = readable.filter((d) => d.template).length;
    const review = docs.filter((d) => statusOf(d)[0] !== 'ok').length;
    put(statsBox, 
      h('div', { class: 'card stat' }, h('span', { class: 'muted small' }, 'เอกสารในรอบนี้'), h('b', {}, docs.length)),
      h('div', { class: 'card stat' }, h('span', { class: 'muted small' }, 'จับคู่ Template ได้'), h('b', {}, readable.length ? pct(matched / readable.length) : '–')),
      h('div', { class: 'card stat' }, h('span', { class: 'muted small' }, 'ต้องตรวจสอบ'), h('b', { style: review ? 'color:#9A4A05' : '' }, review)),
      h('div', { class: 'card stat' }, h('span', { class: 'muted small' }, 'Template ทั้งหมด'), h('b', {}, tpls.length)));

    const rows = [
      ...pending.map((n) => h('tr', {}, h('td', {}, h('b', {}, n)), h('td', {}, '–'), h('td', {}, '–'),
        h('td', {}, h('span', { class: 'status run' }, h('span', { class: 'spin', 'aria-hidden': 'true' }), ' กำลังอ่าน…')), h('td', {}))),
      ...docs.map((d) => h('tr', {},
        h('td', {}, h('div', { style: 'font-weight:600' }, d.filename),
          h('div', { class: 'muted small' }, d.error ? d.error.message : `${d.page_count} หน้า · ${fmtSize(d.size_bytes)}`)),
        h('td', {}, d.template ? h('span', { class: 'pill accent' }, `${d.template} · ${pct(d.template_score || 0)}`) : h('span', { class: 'pill' }, d.error ? '–' : 'ไม่ตรง Template')),
        h('td', {}, d.fields_total ? `${d.fields_found} / ${d.fields_total}` : '–'),
        h('td', {}, statusEl(d)),
        h('td', { style: 'text-align:right' },
          d.error ? h('button', { class: 'btn sm danger', type: 'button', onclick: async () => { await api('DELETE', '/documents/' + d.id); refresh(); } }, 'ลบ')
            : h('a', { href: '#/doc/' + d.id, style: 'font-weight:600' }, d.template ? 'เปิดดู' : 'เปิดดู / สอน')))),
    ];
    put(tableBox, rows.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 't', style: 'min-width:720px' },
        h('thead', {}, h('tr', {}, ['ไฟล์', 'Template', 'ตัวแปรที่ได้', 'สถานะ', ''].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, rows)))
      : h('div', { class: 'empty' }, 'ยังไม่มีเอกสาร อัปโหลด PDF เพื่อเริ่มต้น'));
  }

  put(view, 
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', {}, 'อ่านเอกสาร PDF'), h('p', {}, 'อัปโหลด PDF ระบบจะดึงโครงสร้าง จำว่าเป็นเอกสารแบบไหน แล้วดึงตัวแปรให้อัตโนมัติ')),
      h('a', { class: 'btn', href: '#/teach' }, icon(I.plus, 16), 'สร้าง Template ใหม่')),
    drop, statsBox,
    h('section', { class: 'card', 'aria-labelledby': 'recent' },
      h('div', { class: 'card-head' }, h('h2', { id: 'recent' }, 'เอกสารล่าสุด'), h('span', { class: 'muted small' }, 'เอกสารจะถูกล้างเมื่อปิดแอป · Template เก็บถาวร')),
      tableBox));
  await refresh();
}

// ------------------------------------------------------------------ viewer
const FIELD_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function cropDialog({ docId, doc, region, templates, onSaved }) {
  const stem = (doc.source.filename || 'document').replace(/\.pdf$/i, '').replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/^[^A-Za-z0-9]+/, '') || 'my_template';
  const preview = h('div', { class: 'note', style: 'background:var(--soft);white-space:pre-line;min-height:44px', 'aria-live': 'polite' }, 'กำลังอ่านค่าในกรอบ…');
  const nameIn = h('input', { class: 'input', id: 'cf-name', placeholder: 'เช่น invoice_no', style: 'width:100%' });
  const typeIn = h('select', { class: 'input', id: 'cf-type' },
    [['string', 'ข้อความ (string)'], ['number', 'ตัวเลข (number)'], ['integer', 'จำนวนเต็ม (integer)'], ['date', 'วันที่ (date)'], ['boolean', 'ใช่/ไม่ใช่ (boolean)'], ['table', 'ตาราง (table)']]
      .map(([v, l]) => h('option', { value: v }, l)));
  const patIn = h('input', { class: 'input mono', id: 'cf-pat', placeholder: 'ไม่บังคับ เช่น [A-Z]+-\\d+', style: 'width:100%' });
  const reqIn = h('input', { type: 'checkbox', checked: true, style: 'width:18px;height:18px;accent-color:#1F4FC9' });
  const current = doc.extraction.template;
  const tplSel = h('select', { class: 'input', id: 'cf-tpl' },
    templates.map((t) => h('option', { value: t.name, selected: t.name === current }, t.name)),
    h('option', { value: '__new__', selected: !current }, '+ สร้าง Template ใหม่'));
  const newTpl = h('input', { class: 'input', value: stem, 'aria-label': 'ชื่อ Template ใหม่', style: 'width:100%' });
  const newBox = h('div', { class: 'stack', style: 'gap:6px' }, h('span', { class: 'small muted' }, 'ชื่อ Template ใหม่ (A–Z 0–9 _ . -)'), newTpl);
  const err = h('div', { class: 'note bad', hidden: true });
  const syncNew = () => { newBox.hidden = tplSel.value !== '__new__'; };
  tplSel.addEventListener('change', syncNew); syncNew();

  let timer = null;
  async function refresh() {
    try {
      const r = await api('POST', `/documents/${docId}/region-preview`, { region, type: typeIn.value, pattern: patIn.value.trim() || null });
      if (r.found) {
        put(preview, h('b', {}, 'ค่าที่อ่านได้: '), Array.isArray(r.value) ? `ตาราง ${r.value.length} แถว (คอลัมน์: ${Object.keys(r.value[0] || {}).join(', ')})` : String(r.value),
          r.raw && String(r.raw) !== String(r.value) ? h('div', { class: 'small muted' }, `ข้อความในกรอบ: “${r.raw}”`) : null);
        preview.className = 'note ok';
      } else {
        put(preview, h('b', {}, 'ยังอ่านค่าไม่ได้: '), r.error || '', r.raw ? h('div', { class: 'small' }, `ข้อความในกรอบ: “${r.raw}”`) : null);
        preview.className = 'note warn';
      }
    } catch (e) { put(preview, e.message); preview.className = 'note bad'; }
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(refresh, 250); };
  typeIn.addEventListener('change', refresh); patIn.addEventListener('input', later);

  const field = (label, forId, input, hint) => h('div', { class: 'stack', style: 'gap:6px;flex:1 1 220px' },
    h('label', { for: forId, class: 'lbl' }, label), input, hint ? h('span', { class: 'small muted' }, hint) : null);
  const dlg = h('dialog', { 'aria-labelledby': 'cf-title' }, h('div', { class: 'dlg' },
    h('h2', { class: 'h', id: 'cf-title' }, 'ตัวแปรใหม่จากกรอบที่เลือก'),
    h('p', { class: 'muted small', style: 'margin:0' }, `หน้า ${region.page} · เอกสารแบบเดียวกันจะถูกอ่านจากตำแหน่งนี้`),
    preview,
    h('div', { class: 'row', style: 'align-items:flex-start' },
      field('ชื่อตัวแปร', 'cf-name', nameIn, 'ภาษาอังกฤษ ตัวเลข และ _ เช่น total_amount'),
      field('ชนิดข้อมูล', 'cf-type', typeIn)),
    h('div', { class: 'row', style: 'align-items:flex-start' },
      field('รูปแบบที่ต้องตรง (regex)', 'cf-pat', patIn, 'ใช้เมื่ออยากดึงเฉพาะบางส่วนของข้อความในกรอบ'),
      h('label', { class: 'check', style: 'flex:0 0 auto;align-self:center' }, reqIn, 'ต้องมีค่า (required)')),
    h('div', { class: 'row', style: 'align-items:flex-start' }, field('บันทึกลง Template', 'cf-tpl', tplSel), h('div', { style: 'flex:1 1 220px' }, newBox)),
    err,
    h('div', { class: 'row', style: 'justify-content:flex-end' },
      h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'ยกเลิก'),
      h('button', { class: 'btn primary', type: 'button', onclick: async () => {
        const name = nameIn.value.trim();
        const tpl = tplSel.value === '__new__' ? newTpl.value.trim() : tplSel.value;
        const problem = !FIELD_RE.test(name) ? 'ชื่อตัวแปรใช้ได้เฉพาะ A–Z a–z 0–9 _ และห้ามขึ้นต้นด้วยตัวเลข'
          : !NAME_RE.test(tpl) ? 'ชื่อ Template ใช้ได้เฉพาะ A–Z a–z 0–9 _ . -' : null;
        if (problem) { err.hidden = false; err.textContent = problem; return; }
        const f = { name, type: typeIn.value, region, required: reqIn.checked, description: 'ครอปจากหน้าจอ' };
        if (patIn.value.trim()) f.pattern = patIn.value.trim();
        try {
          await api('POST', `/templates/${encodeURIComponent(tpl)}/fields`, { field: f, doc_id: docId });
          await api('POST', `/documents/${docId}/apply`, { template: tpl });
          dlg.close(); toast(`เพิ่มตัวแปร ${name} ลง ${tpl} แล้ว`); onSaved();
        } catch (e) { err.hidden = false; err.textContent = e.message; }
      } }, 'บันทึกตัวแปร'))));
  dlg.addEventListener('close', () => { dlg.remove(); onSaved.cancel && onSaved.cancel(); });
  document.body.append(dlg); dlg.showModal(); nameIn.focus(); refresh();
}

async function renderViewer(id, startPage = 1) {
  const data = await api('GET', '/documents/' + id);
  const s = data.summary, doc = data.document;
  if (!doc) {
    put(view, h('a', { href: '#/', class: 'btn', style: 'align-self:flex-start' }, icon(I.back), 'กลับ'),
      h('h1', { style: 'margin:0' }, s.filename), h('div', { class: 'note bad' }, `${s.error.error}: ${s.error.message}`));
    return;
  }
  const st = { page: Math.min(startPage, Math.max(1, data.document ? data.document.pages.length : 1)), tab: 'fields',
    show: { fields: true, tables: true, blocks: false }, edits: { ...data.edits }, hot: null, crop: false, draft: null };
  const templates = await api('GET', '/templates');
  const nPages = doc.pages.length;
  const paneBox = h('section', { class: 'pane', 'aria-label': 'หน้าเอกสาร' });
  const sideBox = h('aside', { class: 'side card', 'aria-label': 'ผลการอ่าน' });

  function fieldBoxes(page) {
    const out = [];
    for (const [name, f] of Object.entries(doc.fields)) {
      if (f.page !== page.number) continue;
      if (f.bbox) { out.push({ name, bbox: f.bbox, region: true, found: f.found }); continue; }
      if (!f.found || Array.isArray(f.value) || !f.raw) continue;
      const raw = fold(f.raw);
      const blk = page.blocks.find((b) => fold(b.text).includes(raw));
      if (blk) out.push({ name, bbox: blk.bbox });
    }
    return out;
  }
  function boxEl(kind, bbox, page, tag, key) {
    const W = page.width || 1, H = page.height || 1;
    const el = h('div', { class: `box ${kind}${st.hot === key ? ' hot' : ''}`, 'data-key': key || '',
      style: `left:${(bbox.x0 / W) * 100}%;top:${(bbox.top / H) * 100}%;width:${((bbox.x1 - bbox.x0) / W) * 100}%;height:${((bbox.bottom - bbox.top) / H) * 100}%` },
    tag ? h('span', { class: 'box-tag' }, tag) : null);
    return el;
  }

  function drawPane() {
    const page = doc.pages[st.page - 1];
    const toggles = [['fields', 'ตัวแปร'], ['tables', 'ตาราง'], ['blocks', 'บล็อกข้อความ']].map(([k, label]) =>
      h('label', { class: 'check', style: 'min-height:36px;padding:0 12px;border-radius:999px;background:#fff;border:1px solid var(--line)' },
        h('input', { type: 'checkbox', checked: st.show[k], onchange: (e) => { st.show[k] = e.target.checked; drawPane(); } }), label));
    const boxes = [];
    if (st.show.blocks) page.blocks.forEach((b) => boxes.push(boxEl('block', b.bbox, page, null)));
    if (st.show.tables) page.tables.forEach((t) => boxes.push(boxEl('table', t.bbox, page, `ตาราง ${t.n_rows}×${t.n_cols}`)));
    if (st.show.fields) fieldBoxes(page).forEach((f) => boxes.push(boxEl(f.region ? 'field region' : 'field', f.bbox, page, f.found === false ? f.name + ' (ไม่พบ)' : f.name, f.name)));
    if (st.draft) boxes.push(h('div', { class: 'box draft', style: `left:${st.draft.x0 * 100}%;top:${st.draft.top * 100}%;width:${(st.draft.x1 - st.draft.x0) * 100}%;height:${(st.draft.bottom - st.draft.top) * 100}%` }));
    const img = h('img', { src: `/api/documents/${id}/pages/${st.page}.png?scale=2`, alt: `หน้า ${st.page} ของ ${s.filename}`, draggable: 'false' });
    const paper = h('div', { class: 'paper' + (st.crop ? ' cropping' : '') }, img, boxes);
    if (st.crop) attachCrop(paper, page);
    const cropBtn = h('button', { class: 'btn sm' + (st.crop ? ' primary' : ''), type: 'button', 'aria-pressed': String(st.crop),
      onclick: () => { st.crop = !st.crop; st.draft = null; drawPane(); } },
    icon(['M6 2v14a2 2 0 0 0 2 2h14', 'M18 22V8a2 2 0 0 0-2-2H2'], 16), st.crop ? 'กำลังครอป (กดเพื่อออก)' : 'ครอปตัวแปร');
    put(paneBox,
      h('div', { class: 'row small' }, cropBtn, h('b', {}, 'แสดงกรอบ:'), toggles, h('span', { class: 'grow' }),
        h('span', { class: 'row', style: 'gap:6px;flex-wrap:nowrap' },
          h('button', { class: 'btn sm', type: 'button', 'aria-label': 'หน้าก่อน', disabled: st.page <= 1, onclick: () => { st.page--; draw(); } }, icon(I.left, 16)),
          h('span', { class: 'muted', style: 'white-space:nowrap' }, `หน้า ${st.page} / ${nPages}`),
          h('button', { class: 'btn sm', type: 'button', 'aria-label': 'หน้าถัดไป', disabled: st.page >= nPages, onclick: () => { st.page++; draw(); } }, icon(I.right, 16)))),
      page.needs_ocr ? h('div', { class: 'note warn' }, 'หน้านี้ไม่มีชั้นข้อความ (น่าจะเป็นภาพสแกน) ต้อง OCR ซึ่งยังไม่รองรับในเวอร์ชันนี้') : null,
      st.crop ? h('div', { class: 'note', style: 'background:#F3EEFF;color:#4C1D95' }, 'ลากกรอบครอบค่าที่ต้องการบนหน้าเอกสาร ระบบจะแสดงค่าที่อ่านได้ให้ตรวจก่อนบันทึก (กรอบสีม่วง = ตัวแปรที่ครอปไว้)') : null,
      h('div', { class: 'stage' }, paper));
  }

  function attachCrop(paper, page) {
    let start = null;
    const at = (e) => {
      const r = paper.getBoundingClientRect();
      return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
    };
    const rectOf = (a, b) => ({ x0: Math.min(a.x, b.x), top: Math.min(a.y, b.y), x1: Math.max(a.x, b.x), bottom: Math.max(a.y, b.y) });
    paper.addEventListener('pointerdown', (e) => { if (e.button !== 0) return; e.preventDefault(); paper.setPointerCapture(e.pointerId); start = at(e); });
    paper.addEventListener('pointermove', (e) => {
      if (!start) return;
      const r = rectOf(start, at(e));
      let d = paper.querySelector('.box.draft');
      if (!d) { d = h('div', { class: 'box draft' }); paper.append(d); }
      d.style.cssText = `left:${r.x0 * 100}%;top:${r.top * 100}%;width:${(r.x1 - r.x0) * 100}%;height:${(r.bottom - r.top) * 100}%`;
    });
    paper.addEventListener('pointerup', (e) => {
      if (!start) return;
      const r = rectOf(start, at(e)); start = null;
      if (r.x1 - r.x0 < 0.01 || r.bottom - r.top < 0.006) { paper.querySelector('.box.draft')?.remove(); return; }
      const region = { page: page.number, x0: +r.x0.toFixed(4), top: +r.top.toFixed(4), x1: +r.x1.toFixed(4), bottom: +r.bottom.toFixed(4) };
      st.draft = region;
      const done = () => renderViewer(id, st.page);
      done.cancel = () => { st.draft = null; drawPane(); };
      cropDialog({ docId: id, doc, region, templates, onSaved: done });
    });
  }

  function fieldCard(name, f) {
    const isTable = Array.isArray(f.value) || (f.value === null && f.method === 'table');
    const card = h('div', { class: 'fcard' + (f.found ? '' : ' missing'),
      onmouseenter: () => { st.hot = name; drawPane(); }, onmouseleave: () => { st.hot = null; drawPane(); } },
    h('div', { class: 'row', style: 'justify-content:space-between' },
      h('div', { class: 'row', style: 'gap:8px' }, h('span', { class: 'mono' }, name),
        h('span', { class: 'pill' + (isTable ? ' table' : '') }, isTable ? 'table' : typeof f.value === 'number' ? 'number'
          : typeof f.value === 'boolean' ? 'boolean' : /^\d{4}-\d{2}-\d{2}$/.test(String(f.value)) ? 'date' : 'string')),
      h('span', { class: 'row small muted', style: 'gap:6px' }, f.found ? `หน้า ${f.page ?? '–'} · ${(f.method || '').startsWith('region') ? ((f.method || '').includes('moved') ? 'ครอป (ขยับตามป้าย)' : 'ครอป') : f.method}` : 'ไม่พบ',
        doc.extraction.template ? h('button', { class: 'btn sm', type: 'button', style: 'min-height:32px;padding:0 10px', 'aria-label': `ลบตัวแปร ${name} ออกจาก Template`,
          onclick: async (e) => {
            e.stopPropagation();
            if (!confirm(`ลบตัวแปร "${name}" ออกจาก Template "${doc.extraction.template}"?`)) return;
            await api('DELETE', `/templates/${encodeURIComponent(doc.extraction.template)}/fields/${encodeURIComponent(name)}`);
            await api('POST', `/documents/${id}/apply`, { template: doc.extraction.template });
            toast('ลบตัวแปรแล้ว'); renderViewer(id, st.page);
          } }, 'ลบ') : null)));
    if (isTable && Array.isArray(f.value)) {
      const cols = f.value.length ? Object.keys(f.value[0]) : [];
      card.append(h('div', { class: 'table-wrap' }, h('table', { class: 't small' },
        h('thead', {}, h('tr', {}, cols.map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', {}, f.value.map((r) => h('tr', {}, cols.map((c) => h('td', {}, r[c] ?? '–'))))))));
    } else {
      const val = name in st.edits ? st.edits[name] : f.value;
      const input = h('input', { class: 'input', type: 'text', value: val ?? '', 'aria-label': 'ค่าของ ' + name,
        oninput: (e) => { st.edits[name] = e.target.value; } });
      card.append(input);
      if (f.raw) card.append(h('div', { class: 'muted small' }, `ข้อความต้นฉบับ: “${f.raw}”`));
      if (!f.found && f.error) card.append(h('div', { class: 'small', style: 'color:#7C2D12' }, f.error));
    }
    return card;
  }

  function drawSide() {
    const tabs = [['fields', 'ตัวแปร'], ['structure', 'โครงสร้าง'], ['json', 'JSON']];
    const tablist = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'มุมมองผลลัพธ์' },
      tabs.map(([k, label]) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(st.tab === k), onclick: () => { st.tab = k; drawSide(); } }, label)));
    let panel;
    if (st.tab === 'fields') {
      const entries = Object.entries(doc.fields);
      const found = entries.filter(([, f]) => f.found).length;
      panel = h('div', { class: 'tabpanel', role: 'tabpanel' },
        entries.length ? h('div', { class: 'row small', style: 'justify-content:space-between' },
          h('span', { class: 'muted' }, `ได้ ${found} จาก ${entries.length} ตัวแปร`),
          doc.extraction.missing_required_fields.length
            ? h('span', { class: 'status warn' }, 'ขาด: ' + doc.extraction.missing_required_fields.join(', '))
            : h('span', { class: 'status ok' }, 'ไม่มีตัวแปรบังคับที่ขาด')) : null,
        entries.length ? entries.map(([n, f]) => fieldCard(n, f)) :
          h('div', { class: 'note warn' }, 'เอกสารนี้ยังไม่มีตัวแปร กด ', h('b', {}, 'ครอปตัวแปร'), ' แล้วลากกรอบบนหน้าเอกสารเพื่อกำหนดเอง หรือเลือก Template ด้านบน หรือ ',
            h('a', { href: '#/teach?doc=' + id }, 'ให้ระบบเรียนรู้อัตโนมัติ')),
        entries.length ? h('button', { class: 'btn', type: 'button', onclick: async () => {
          await api('PUT', `/documents/${id}/edits`, st.edits); toast('บันทึกการแก้ไขแล้ว (มีผลกับไฟล์ JSON ที่ส่งออก)');
        } }, 'บันทึกการแก้ไข') : null);
    } else if (st.tab === 'structure') {
      const page = doc.pages[st.page - 1];
      const warns = [...doc.warnings, ...page.warnings];
      panel = h('div', { class: 'tabpanel', role: 'tabpanel' },
        h('div', { class: 'mini' }, h('div', {}, h('span', { class: 'muted small' }, 'บล็อก'), h('b', {}, page.blocks.length)),
          h('div', {}, h('span', { class: 'muted small' }, 'ตาราง'), h('b', {}, page.tables.length)),
          h('div', {}, h('span', { class: 'muted small' }, 'รูปภาพ'), h('b', {}, page.images.length))),
        page.blocks.length ? page.blocks.map((b) => h('div', { class: 'blk' }, h('span', { class: 'k' }, b.type + (b.heading_level ? ' ' + b.heading_level : '')),
          h('span', { class: 'grow', style: 'min-width:0;white-space:pre-line' }, b.text), h('span', { class: 'mono muted small' }, b.id)))
          : h('div', { class: 'muted' }, 'ไม่มีบล็อกข้อความในหน้านี้'),
        warns.length ? h('div', { class: 'note warn' }, h('b', {}, 'คำเตือน'), h('ul', { style: 'margin:6px 0 0;padding-left:18px' }, warns.map((w) => h('li', {}, w))))
          : h('div', { class: 'note ok' }, 'ไม่มีคำเตือน'));
    } else {
      const text = JSON.stringify(doc, null, 2);
      panel = h('div', { class: 'tabpanel', role: 'tabpanel' }, h('pre', { class: 'json' }, text),
        h('button', { class: 'btn', type: 'button', onclick: async () => {
          try { await navigator.clipboard.writeText(text); toast('คัดลอก JSON แล้ว'); } catch { toast('คัดลอกไม่ได้ ใช้ปุ่ม "ส่งออก JSON" แทน'); }
        } }, 'คัดลอก JSON'));
    }
    put(sideBox, tablist, panel);
  }
  function draw() { drawPane(); drawSide(); }

  const select = h('select', { class: 'input', 'aria-label': 'เลือก Template' },
    h('option', { value: '' }, 'เลือก Template…'), templates.map((t) => h('option', { value: t.name, selected: t.name === doc.extraction.template }, t.name)));
  const ex = doc.extraction;
  put(view, 
    h('header', { class: 'vhead' },
      h('a', { href: '#/', class: 'btn', 'aria-label': 'กลับหน้าหลัก', style: 'width:44px;padding:0' }, icon(I.back)),
      h('div', { class: 'grow', style: 'min-width:240px' }, h('h1', {}, s.filename),
        h('div', { class: 'row small muted', style: 'gap:8px' },
          `${nPages} หน้า · PDF ${doc.source.pdf_version || '?'} · ${ex.engines_used.join(', ')} · ${ex.duration_ms} ms`,
          ex.template ? h('span', { class: 'pill accent' }, `Template: ${ex.template} · ${(ex.template_score || 0).toFixed(2)}`) : null,
          statusEl(s))),
      h('div', { class: 'row' }, select,
        h('button', { class: 'btn', type: 'button', onclick: async () => {
          if (!select.value) return toast('เลือก Template ก่อน');
          await api('POST', `/documents/${id}/apply`, { template: select.value }); toast('ใช้ Template แล้ว'); renderViewer(id);
        } }, 'ใช้ Template'),
        h('a', { class: 'btn', href: '#/teach?doc=' + id }, 'ใช้เป็นตัวอย่าง Template'),
        h('a', { class: 'btn primary', href: `/api/documents/${id}/export`, download: '' }, 'ส่งออก JSON'))),
    h('div', { class: 'viewer' }, paneBox, sideBox));
  draw();
}

// ------------------------------------------------------------------ teach
async function renderTeach(query) {
  const pre = query.get('doc');
  const docs = (await api('GET', '/documents')).filter((d) => !d.error);
  const existing = (await api('GET', '/templates')).map((t) => t.name);
  const chosen = new Set(pre ? [pre] : []);
  const nameIn = h('input', { class: 'input', id: 'tname', value: '', placeholder: 'เช่น acme_invoice', style: 'width:100%;max-width:360px' });
  const descIn = h('input', { class: 'input', id: 'tdesc', value: '', placeholder: 'เช่น ใบแจ้งหนี้จาก ACME', style: 'width:100%;max-width:420px' });
  const nameHint = h('div', { class: 'small muted', 'aria-live': 'polite' });
  const counter = h('div', { class: 'small muted' });
  const samplesBox = h('div', { class: 'stack' });
  const stepsBox = h('section', { class: 'card card-pad stack', 'aria-label': 'ความคืบหน้าการเรียนรู้' });
  const resultBox = h('div', { class: 'stack' });
  const startBtn = h('button', { class: 'btn primary', type: 'button' }, 'เริ่มเรียนรู้');
  const more = h('input', { type: 'file', id: 'more', accept: 'application/pdf,.pdf', multiple: true, class: 'file-input' });

  function validate() {
    const n = nameIn.value.trim();
    const ok = NAME_RE.test(n);
    nameHint.textContent = !n ? 'ตั้งชื่อด้วยตัวอักษรอังกฤษ ตัวเลข _ . -' : !ok ? 'ชื่อใช้ได้เฉพาะ A–Z a–z 0–9 _ . - (ไม่เกิน 64 ตัว)'
      : existing.includes(n) ? 'มี Template นี้แล้ว: จะเพิ่มตัวอย่างและเรียนรู้ใหม่จากตัวอย่างทั้งหมด' : 'Template ใหม่';
    startBtn.disabled = !(ok && chosen.size);
    counter.textContent = `เลือกแล้ว ${chosen.size} ไฟล์ · แนะนำ 2–3 ไฟล์ขึ้นไป เพื่อแยกตัวแปรออกจากข้อความคงที่`;
  }
  nameIn.addEventListener('input', validate);

  function drawSamples() {
    put(samplesBox, 
      docs.length ? docs.map((d) => h('label', { class: 'sample' },
        h('input', { type: 'checkbox', checked: chosen.has(d.id), style: 'width:18px;height:18px;accent-color:#1F4FC9',
          onchange: (e) => { e.target.checked ? chosen.add(d.id) : chosen.delete(d.id); validate(); } }),
        h('span', { class: 'grow', style: 'min-width:0;word-break:break-all' }, d.filename),
        h('span', { class: 'muted small' }, `${d.page_count} หน้า`)))
        : h('div', { class: 'muted small' }, 'ยังไม่มีเอกสาร เพิ่มไฟล์ตัวอย่างด้านล่าง'),
      h('label', { for: 'more', class: 'btn', style: 'border-style:dashed;color:var(--accent)' }, '+ เพิ่มไฟล์ตัวอย่าง'), more,
      counter);
  }
  more.addEventListener('change', async () => {
    try {
      const res = await uploadFiles([...more.files], false);
      for (const r of res) { if (!r.error) { docs.unshift(r); chosen.add(r.id); } else toast(`${r.filename}: ${r.error.message}`); }
      drawSamples(); validate();
    } catch (e) { toast(e.message); }
  });

  const STEPS = ['อ่านไฟล์ตัวอย่าง', 'เทียบตัวอย่าง แยกตัวแปร / ข้อความคงที่', 'ทดสอบ Template กับตัวอย่าง'];
  function drawSteps(job, n) {
    // server steps: 0..n-1 reading, n comparing, n+1 testing, total = done
    const phase = !job ? -1 : job.done && !job.error ? 3 : job.step < n ? 0 : job.step === n ? 1 : 2;
    put(stepsBox, 
      h('ol', { class: 'steps' }, STEPS.map((label, i) => h('li', { class: phase > i ? 'done' : phase === i ? 'now' : '', 'aria-current': phase === i ? 'step' : null },
        h('span', { class: 'dot' }, phase > i ? icon(I.check, 14, '#fff') : String(i + 1)),
        h('span', {}, h('div', { class: 'lbl' }, label), h('div', { class: 'sub' }, phase === i && job ? job.message : phase > i ? 'เสร็จ' : 'รอ'))))),
      h('div', { class: 'bar', role: 'progressbar', 'aria-label': 'ความคืบหน้ารวม', 'aria-valuemin': '0', 'aria-valuemax': '100',
        'aria-valuenow': String(job ? Math.round((job.done ? 1 : job.step / job.total) * 100) : 0) },
      h('i', { style: `width:${job ? (job.done ? 100 : (job.step / job.total) * 100) : 0}%` })),
      job && job.error ? h('div', { class: 'note bad' }, job.error) : null);
  }

  function drawResult(res) {
    const t = res.template;
    const nameById = Object.fromEntries(docs.map((d) => [d.id, d.filename]));
    const fields = t.spec.fields || [];
    put(resultBox, 
      t.samples < 2 ? h('div', { class: 'note warn' }, 'เรียนรู้จากตัวอย่างเพียงไฟล์เดียว จึงแยกข้อความคงที่ (เช่น เลขภาษีของผู้ขาย) ออกจากตัวแปรไม่ได้ แนะนำให้เพิ่มตัวอย่างอีก') : null,
      h('section', { class: 'card' },
        h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, 'ตัวแปรที่เรียนรู้ได้'), h('div', { class: 'muted small' }, `จาก ${t.samples} ตัวอย่าง · ${t.anchors} anchors`)),
          h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: () => specDialog(t.name, t.spec_yaml) }, 'แก้ spec (YAML)'),
            h('a', { class: 'btn primary', href: '#/templates' }, 'ไปคลัง Template'))),
        fields.length ? h('div', { class: 'table-wrap' }, h('table', { class: 't', style: 'min-width:760px' },
          h('thead', {}, h('tr', {}, ['ชื่อตัวแปร', 'ป้าย / คอลัมน์', 'ชนิด', 'เจอใน', 'บังคับ', 'รูปแบบ'].map((x) => h('th', { scope: 'col' }, x)))),
          h('tbody', {}, fields.map((f) => {
            const sStat = t.stats[f.name] || { found_in: 0, samples: t.samples };
            const ratio = sStat.samples ? sStat.found_in / sStat.samples : 0;
            const where = f.columns ? Object.keys(f.columns).join(' · ') : (f.label || [f.regex || f.source]).join(' / ');
            return h('tr', {}, h('td', { class: 'mono' }, f.name), h('td', {}, where),
              h('td', {}, h('span', { class: 'pill' + (f.type === 'table' ? ' table' : '') }, f.type || 'string')),
              h('td', {}, h('div', { class: 'row', style: 'gap:8px;flex-wrap:nowrap' }, h('div', { class: 'bar ' + (ratio === 1 ? 'ok' : 'warn'), style: 'width:64px' }, h('i', { style: `width:${ratio * 100}%` })), `${sStat.found_in}/${sStat.samples}`)),
              h('td', {}, f.required ? 'ใช่' : '–'), h('td', { class: 'mono small' }, f.pattern || '—'));
          })))) : h('div', { class: 'empty' }, 'ไม่พบป้าย:ค่า หรือตารางในตัวอย่าง ลองเขียน spec เอง (แก้ spec)')),
      h('section', { class: 'card card-pad stack' }, h('h2', { class: 'h' }, 'ข้อความคงที่ (Anchors)'),
        h('p', { class: 'muted small', style: 'margin:0' }, 'เจอทุกใบ ใช้จำว่าเอกสารใหม่เป็นแบบนี้หรือไม่'),
        h('div', { class: 'chips' }, t.anchors_list.map((a) => h('span', { class: 'chip' }, a)))),
      h('section', { class: 'card card-pad stack' }, h('h2', { class: 'h' }, 'ทดสอบกับตัวอย่าง'),
        res.checks.map((c) => h('div', { class: 'row' }, h('span', { class: 'grow' }, nameById[c.doc_id] || c.doc_id),
          h('span', { class: 'status ' + (c.found === c.total ? 'ok' : 'warn') }, `หาเจอ ${c.found}/${c.total}`)))));
  }

  startBtn.addEventListener('click', async () => {
    const name = nameIn.value.trim();
    const ids = [...chosen];
    startBtn.disabled = true; put(resultBox, );
    try {
      const { job } = await api('POST', `/templates/${encodeURIComponent(name)}/learn`, { doc_ids: ids, description: descIn.value.trim() });
      let state;
      do {
        await new Promise((r) => setTimeout(r, 250));
        state = await api('GET', '/jobs/' + job);
        drawSteps(state, ids.length);
      } while (!state.done);
      if (state.result) { drawResult(state.result); toast(`บันทึก Template "${name}" แล้ว`); if (!existing.includes(name)) existing.push(name); }
    } catch (e) { toast(e.message); }
    validate();
  });

  put(view, 
    h('header', { class: 'page-head' }, h('div', {}, h('h1', {}, 'สอน Template'),
      h('p', {}, 'เลือก PDF ตัวอย่างที่เป็นเอกสารแบบเดียวกัน ระบบจะหาตัวแปร ชนิดข้อมูล และตารางให้เอง'))),
    h('section', { class: 'card card-pad stack' },
      h('div', { class: 'row', style: 'align-items:flex-start;gap:18px' },
        h('div', { class: 'stack grow', style: 'gap:6px' }, h('label', { for: 'tname', class: 'lbl' }, 'ชื่อ Template'), nameIn, nameHint),
        h('div', { class: 'stack grow', style: 'gap:6px' }, h('label', { for: 'tdesc', class: 'lbl' }, 'คำอธิบาย (ไม่บังคับ)'), descIn)),
      h('div', { class: 'row' }, startBtn)),
    stepsBox,
    h('div', { class: 'split' },
      h('aside', { class: 'left' },
        h('section', { class: 'card card-pad stack' }, h('h2', { class: 'h' }, 'ไฟล์ตัวอย่าง'), samplesBox),
        h('div', { class: 'note ok row', style: 'flex-wrap:nowrap;align-items:flex-start' }, icon(I.lock),
          h('span', {}, 'Template เก็บเฉพาะชนิดข้อมูลและ hash ของค่า ไม่เก็บค่าจริงของเอกสาร'))),
      h('div', { class: 'right' }, resultBox)));
  drawSamples(); drawSteps(null, 0); validate();
}

// ------------------------------------------------------------------ templates
async function renderTemplates() {
  const list = await api('GET', '/templates');
  const docs = (await api('GET', '/documents')).filter((d) => !d.error);
  const matchBox = h('div', { class: 'stack' });
  const pick = h('select', { class: 'input', 'aria-label': 'เลือกเอกสารที่จะทดสอบ' },
    docs.length ? docs.map((d) => h('option', { value: d.id }, d.filename)) : h('option', { value: '' }, 'ยังไม่มีเอกสาร'));

  const cards = list.map((t) => h('article', { class: 'card tcard' },
    h('div', { class: 'row', style: 'justify-content:space-between;align-items:flex-start' },
      h('div', { style: 'min-width:0' }, h('h2', { class: 'h mono' }, t.name), h('div', { class: 'muted small' }, t.description || '—')),
      h('span', { class: 'pill ' + (t.manual_spec || !t.auto_fields ? 'manual' : 'accent') },
        t.manual_spec ? 'แก้ spec เอง' : !t.auto_fields ? 'ครอปเอง' : 'เรียนรู้อัตโนมัติ')),
    h('div', { class: 'mini' }, h('div', {}, h('span', { class: 'muted small' }, 'ตัวอย่าง'), h('b', {}, t.samples)),
      h('div', {}, h('span', { class: 'muted small' }, 'ตัวแปร'), h('b', {}, t.fields)),
      h('div', {}, h('span', { class: 'muted small' }, 'Anchors'), h('b', {}, t.anchors))),
    t.found_rate !== null ? h('div', { class: 'stack', style: 'gap:6px' },
      h('div', { class: 'row small', style: 'justify-content:space-between' }, h('span', { class: 'muted' }, 'หาตัวแปรเจอในตัวอย่าง'), h('b', {}, pct(t.found_rate))),
      h('div', { class: 'bar ' + (t.found_rate >= 0.9 ? 'ok' : 'warn') }, h('i', { style: `width:${t.found_rate * 100}%` }))) : null,
    h('div', { class: 'row', style: 'border-top:1px solid var(--line-2);padding-top:12px' },
      h('a', { class: 'btn sm', href: '#/teach' }, 'เพิ่มตัวอย่าง'),
      h('button', { class: 'btn sm', type: 'button', onclick: async () => { const d = await api('GET', '/templates/' + encodeURIComponent(t.name)); specDialog(t.name, d.spec_yaml, () => renderTemplates()); } }, 'แก้ spec'),
      h('a', { class: 'btn sm', href: `/api/templates/${encodeURIComponent(t.name)}/spec.yaml`, download: '' }, 'ส่งออก YAML'),
      h('button', { class: 'btn sm danger', type: 'button', onclick: async () => {
        if (!confirm(`ลบ Template "${t.name}"? ย้อนกลับไม่ได้`)) return;
        await api('DELETE', '/templates/' + encodeURIComponent(t.name)); toast('ลบแล้ว'); renderTemplates();
      } }, 'ลบ'),
      h('span', { class: 'grow' }), h('span', { class: 'muted small' }, 'อัปเดต ' + (t.updated_at || '').slice(0, 10)))));

  async function runMatch() {
    if (!pick.value) return;
    const res = await api('POST', `/documents/${pick.value}/match`);
    put(matchBox, res.length ? res.map((m) => h('div', { class: 'match' },
      h('span', { class: 'mono' }, m.template),
      h('div', { class: 'bar' }, h('i', { style: `width:${m.score * 100}%;background:${m.score >= 0.6 ? '#1F4FC9' : '#A3A9B5'}` })),
      h('span', { class: 'small', style: 'flex:0 0 150px' }, `${m.score.toFixed(2)} · anchors ${m.anchors_found}/${m.anchors_total}`),
      m.score >= 0.6 ? h('button', { class: 'btn sm', type: 'button', onclick: async () => {
        await api('POST', `/documents/${pick.value}/apply`, { template: m.template }); location.hash = '#/doc/' + pick.value;
      } }, 'ใช้ Template นี้') : h('span', { class: 'pill' }, 'ต่ำกว่าเกณฑ์')))
      : h('div', { class: 'muted' }, 'ยังไม่มี Template'));
  }

  put(view, 
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', {}, 'คลัง Template'), h('p', {}, 'แบบเอกสารที่ระบบจำได้ เอกสารใหม่จะถูกจับคู่กับ Template ที่คะแนนสูงสุด (เกณฑ์ 0.60)')),
      h('a', { class: 'btn primary', href: '#/teach' }, icon(I.plus, 16), 'สอน Template ใหม่')),
    list.length ? h('section', { class: 'cards', 'aria-label': 'รายการ Template' }, cards)
      : h('div', { class: 'card empty' }, 'ยังไม่มี Template ', h('a', { href: '#/teach' }, 'สอน Template แรก')),
    h('section', { class: 'card card-pad stack', 'aria-labelledby': 'mt' },
      h('div', { class: 'row', style: 'justify-content:space-between' },
        h('div', {}, h('h2', { class: 'h', id: 'mt' }, 'ทดสอบการจับคู่'), h('div', { class: 'muted small' }, 'เลือกเอกสารที่อัปโหลดแล้ว เพื่อดูว่าตรงกับ Template ไหน')),
        h('div', { class: 'row' }, pick, h('button', { class: 'btn', type: 'button', disabled: !docs.length, onclick: runMatch }, 'ทดสอบ'))),
      matchBox));
}

// ------------------------------------------------------------------ router
async function route() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, qs] = raw.split('?');
  const query = new URLSearchParams(qs || '');
  const section = path.startsWith('/doc/') ? 'home' : path.slice(1) || 'home';
  document.querySelectorAll('.nav a').forEach((a) => (a.dataset.nav === section ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  try {
    if (path.startsWith('/doc/')) await renderViewer(path.slice(5));
    else if (path === '/teach') await renderTeach(query);
    else if (path === '/templates') await renderTemplates();
    else await renderHome();
  } catch (e) {
    put(view, h('div', { class: 'note bad' }, 'เกิดข้อผิดพลาด: ' + e.message), h('a', { href: '#/', class: 'btn', style: 'align-self:flex-start' }, 'กลับหน้าหลัก'));
  }
  view.focus({ preventScroll: true });
}
window.addEventListener('hashchange', route);
api('GET', '/info').then((i) => { document.getElementById('version').textContent = `v${i.version} · Template: ${i.store}`; }).catch(() => {});
route();
