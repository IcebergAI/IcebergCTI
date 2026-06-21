/* Iceberg — Alpine component registry.
 *
 * Every interactive component is registered here via Alpine.data() inside an
 * `alpine:init` listener. This file is loaded (deferred) BEFORE the Alpine
 * script in base.html, so the listener is in place when the CSP build dispatches
 * `alpine:init` on startup.
 *
 * Why registered components rather than inline x-data objects: the portal runs
 * the @alpinejs/csp build (no eval), which lets the app ship a strict
 * `script-src 'self'` CSP (no 'unsafe-inline'/'unsafe-eval'). The CSP build's
 * expression interpreter evaluates directive *attributes* (x-text, @click, :class,
 * ternaries, assignments, method calls — all fine) but it CANNOT parse an inline
 * x-data object literal that defines methods/getters. So any component with
 * behaviour lives here as a native-JS factory; server data is passed through the
 * `x-data="factory({ ...{{ value|tojson }}... })"` argument (a plain data object
 * literal, which the interpreter handles).
 *
 * KIND_ORDER and KIND_CLASS are the single source of truth for facet order and
 * the chip colour class — keep in sync with models.TagKind and iceberg.css (.k-*).
 */
const KIND_ORDER = ['ACTOR', 'CAMPAIGN', 'MALWARE', 'TECHNIQUE', 'SECTOR', 'TOPIC'];
const KIND_CLASS = {
  ACTOR: 'k-actor', CAMPAIGN: 'k-campaign', MALWARE: 'k-malware',
  TECHNIQUE: 'k-technique', SECTOR: 'k-sector', TOPIC: 'k-topic',
};
const NAMED_KINDS = ['ACTOR', 'MALWARE', 'CAMPAIGN'];

/* Read server state from a <script type="application/json"> island by id.
 * Server HTML/SVG/text is passed this way (not through x-data attributes):
 * the CSP-build expression parser does NOT decode \uXXXX escapes, but Jinja's
 * tojson \u-escapes < > & ' — so any string with those chars would corrupt if
 * parsed as an expression. JSON.parse decodes \u correctly. */
function readJSON(id) {
  const el = document.getElementById(id);
  if (!el) return {};
  try { return JSON.parse(el.textContent); } catch { return {}; }
}

document.addEventListener('alpine:init', () => {
  /* ---- App shell: ⌘K command palette (base.html) ------------------------- */
  Alpine.data('appShell', (jumpItems) => ({
    cmdOpen: false, cmdQ: '', cmdItems: jumpItems || [], cmdActive: 0,
    get cmdResults() {
      const q = this.cmdQ.trim().toLowerCase();
      const m = q ? this.cmdItems.filter(i => i.label.toLowerCase().includes(q) || i.group.toLowerCase().includes(q)) : this.cmdItems;
      if (this.cmdActive >= m.length) this.cmdActive = Math.max(0, m.length - 1);
      return m;
    },
    openCmd() { this.cmdOpen = true; this.cmdQ = ''; this.cmdActive = 0; this.$nextTick(() => this.$refs.cmdInput?.focus()); },
    cmdGo() { const r = this.cmdResults[this.cmdActive]; if (r) window.location.href = r.href; },
    cmdMove(d) { const n = this.cmdResults.length; if (n) this.cmdActive = (this.cmdActive + d + n) % n; },
  }));

  /* ---- Report editor (report_edit.html) ---------------------------------- */
  Alpine.data('reportEditor', (dataId) => ({
    ...readJSON(dataId),  // body, kj, ka, gaps, previewHtml, reportId
    tab: 'cite', insertOpen: false, justSaved: false,
    dirty: false, saving: false, timer: null, saveTimer: null,
    markDirty() { this.dirty = true; this.scheduleSave(); },
    schedule() { this.dirty = true; clearTimeout(this.timer); this.timer = setTimeout(() => this.refresh(), 350); this.scheduleSave(); },
    scheduleSave() { clearTimeout(this.saveTimer); this.saveTimer = setTimeout(() => this.autosave(), 1200); },
    async autosave() {
      const form = document.getElementById('reportform');
      if (!form) return;
      this.saving = true;
      try {
        const res = await fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Requested-With': 'fetch' } });
        this.dirty = !res.ok;
      } catch { /* leave dirty; manual Save remains available */ }
      this.saving = false;
    },
    async refresh() {
      const res = await fetch('/api/preview/product', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          report_id: this.reportId,
          body_md: this.body,
          key_judgements: this.kj,
          key_assumptions: this.ka,
          intelligence_gaps: this.gaps,
        }),
      });
      if (res.ok) { this.previewHtml = (await res.json()).html; }
    },
    // The CSP build prohibits x-html; render the (server-sanitised) preview HTML
    // through a ref in native JS instead, re-run reactively by x-effect.
    renderPreview() { if (this.$refs.preview) this.$refs.preview.innerHTML = this.previewHtml; },
    // Insert an embed token at the cursor and notify x-model (dispatch 'input')
    // so the live preview refreshes; then close the Insert menu.
    insertToken(token) {
      const t = document.querySelector('textarea[name="body_md"]');
      this.insertOpen = false;
      if (!t) return;
      const before = t.value.slice(0, t.selectionStart);
      const lead = (before && !before.endsWith('\n')) ? '\n\n' : '';
      t.setRangeText(`${lead}${token}\n`, t.selectionStart, t.selectionEnd, 'end');
      t.dispatchEvent(new Event('input', { bubbles: true }));
      t.focus();
    },
    insertDiamond(id) { this.insertToken(`[[diamond:${id}]]`); },
    insertFigure(id) { this.insertToken(`[[figure:${id}]]`); },
    insertAch(id) { this.insertToken(`[[ach:${id}]]`); },
    insertAttack() { this.insertToken('[[attack]]'); },
  }));

  /* ---- Citation autosave form (report_edit.html) ------------------------- */
  Alpine.data('citationForm', () => ({
    error: false,
    async save() {
      this.error = false;
      try {
        const res = await fetch(this.$el.action, { method: 'POST', body: new FormData(this.$el), headers: { 'X-Requested-With': 'fetch' } });
        this.error = !res.ok;
      } catch { this.error = true; }
    },
  }));

  /* ---- Reports library filter (reports_list.html) ------------------------ */
  // ok($el) reads the row's data-* attributes (avoids passing titles — which may
  // contain < > & ' — through the CSP expression parser).
  Alpine.data('reportsFilter', () => ({
    q: '', status: '', level: '',
    ok(el) {
      const d = el.dataset;
      return (!this.q || d.t.includes(this.q.toLowerCase()))
        && (!this.status || d.s === this.status)
        && (!this.level || d.l === this.level);
    },
  }));

  /* ---- Copy-token button (diamond_edit.html / ach_edit.html) ------------- */
  Alpine.data('copyToken', (token) => ({
    copied: false,
    copy() { navigator.clipboard.writeText(token).then(() => { this.copied = true; setTimeout(() => { this.copied = false; }, 1600); }); },
  }));

  /* ---- Diamond Model editor (diamond_edit.html) -------------------------- */
  Alpine.data('diamondEditor', (dataId) => ({
    ...readJSON(dataId),  // fields, previewSvg
    timer: null,
    schedule() { clearTimeout(this.timer); this.timer = setTimeout(() => this.refresh(), 300); },
    async refresh() {
      const res = await fetch('/api/preview/diamond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.fields),
      });
      if (res.ok) { this.previewSvg = (await res.json()).svg; }
    },
    // x-html is prohibited in the CSP build; render the SVG via a ref instead.
    renderPreview() { if (this.$refs.preview) this.$refs.preview.innerHTML = this.previewSvg; },
  }));

  /* ---- ACH matrix editor (ach_edit.html) --------------------------------- */
  Alpine.data('achEditor', (dataId) => ({
    ...readJSON(dataId),  // title, question, hypotheses, evidence, ratings, previewSvg
    timer: null,
    nextId(prefix, rows) {
      let max = 0;
      rows.forEach(r => {
        if (typeof r.id === 'string' && r.id[0] === prefix) {
          const n = parseInt(r.id.slice(1), 10);
          if (!Number.isNaN(n)) max = Math.max(max, n);
        }
      });
      return `${prefix}${max + 1}`;
    },
    addHypothesis() { this.hypotheses.push({ id: this.nextId('h', this.hypotheses), text: '' }); this.schedule(); },
    addEvidence() { this.evidence.push({ id: this.nextId('e', this.evidence), text: '' }); this.schedule(); },
    removeHypothesis(j) {
      const hid = this.hypotheses[j].id;
      this.hypotheses.splice(j, 1);
      Object.keys(this.ratings).forEach(k => { if (k.split(':')[0] === hid) delete this.ratings[k]; });
      this.schedule();
    },
    removeEvidence(i) {
      const eid = this.evidence[i].id;
      this.evidence.splice(i, 1);
      Object.keys(this.ratings).forEach(k => { if (k.split(':')[1] === eid) delete this.ratings[k]; });
      this.schedule();
    },
    rating(hid, eid) { return this.ratings[`${hid}:${eid}`] || 'NEUTRAL'; },
    setRating(hid, eid, value) {
      if (value === 'NEUTRAL') { delete this.ratings[`${hid}:${eid}`]; }
      else { this.ratings[`${hid}:${eid}`] = value; }
      this.schedule();
    },
    serialize() {
      this.$refs.matrix.value = JSON.stringify({ hypotheses: this.hypotheses, evidence: this.evidence, ratings: this.ratings });
    },
    schedule() { clearTimeout(this.timer); this.timer = setTimeout(() => this.refresh(), 300); },
    async refresh() {
      const res = await fetch('/api/preview/ach', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: this.title, question: this.question, hypotheses: this.hypotheses, evidence: this.evidence, ratings: this.ratings }),
      });
      if (res.ok) { this.previewSvg = (await res.json()).svg; }
    },
    // x-html is prohibited in the CSP build; render the SVG via a ref instead.
    renderPreview() { if (this.$refs.preview) this.$refs.preview.innerHTML = this.previewSvg; },
  }));

  /* ---- Feed reader: send-to-notebook picker (feeds_reader.html) ---------- */
  // Toggles the "new notebook" inputs when the notebook <select> is set to the
  // empty "create new" option.
  Alpine.data('sendToNotebook', () => ({ notebookId: '' }));

  /* ---- Confirm-before-submit guard (delete forms) ------------------------ */
  Alpine.data('confirmSubmit', (message) => ({
    message,
    check(e) { if (!window.confirm(this.message)) e.preventDefault(); },
  }));

  /* ---- Report classification: searchable token combobox (report_edit) ---- */
  Alpine.data('tagPicker', (cfg) => ({
    all: (() => {
      const el = document.getElementById('taxonomy-data');
      try { return el ? JSON.parse(el.textContent) : []; } catch { return []; }
    })(),
    selectedIds: Array.isArray(cfg.selectedIds) ? [...cfg.selectedIds] : [],
    initialIds: Array.isArray(cfg.selectedIds) ? [...cfg.selectedIds] : [],
    canTag: cfg.canTag !== false,
    q: '', open: false, activeId: null, justSaved: false,
    kindClass: KIND_CLASS,

    byId(id) { return this.all.find(t => t.id === id); },
    isSelected(id) { return this.selectedIds.includes(id); },
    get selected() {
      return this.selectedIds.map(id => this.byId(id)).filter(Boolean)
        .sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.label.localeCompare(b.label));
    },
    get dirty() {
      const a = [...this.selectedIds].sort((x, y) => x - y), b = [...this.initialIds].sort((x, y) => x - y);
      return a.length !== b.length || a.some((v, i) => v !== b[i]);
    },
    get summaryText() {
      const n = this.selectedIds.length, f = new Set(this.selected.map(t => t.kind)).size;
      return n ? `${n} tag${n > 1 ? 's' : ''} · ${f} facet${f > 1 ? 's' : ''}` : 'No tags yet';
    },
    get menuItems() {
      const q = this.q.trim().toLowerCase();
      // only active, not-yet-selected terms are offered; retired stay on the report but aren't suggested
      return this.all.filter(t => t.active && !this.isSelected(t.id) && (
        !q || t.label.toLowerCase().includes(q) || (t.ext || '').toLowerCase().includes(q) ||
        (t.desc || '').toLowerCase().includes(q) || t.kind.toLowerCase().includes(q)));
    },
    get groups() {
      const g = {};
      for (const t of this.menuItems) {
        if (!g[t.kind]) g[t.kind] = [];
        g[t.kind].push(t);
      }
      return KIND_ORDER.filter(k => g[k]).map(k => ({ kind: k, items: g[k] }));
    },
    get flat() { return this.groups.flatMap(g => g.items); },

    focusInput() { if (this.canTag) { this.open = true; this.$refs.input?.focus(); } },
    ensureActive() { const f = this.flat; if (!f.some(t => t.id === this.activeId)) this.activeId = f.length ? f[0].id : null; },
    move(d) { this.open = true; const f = this.flat; if (!f.length) { this.activeId = null; return; } let i = f.findIndex(t => t.id === this.activeId); i = (i + d + f.length) % f.length; this.activeId = f[i].id; },
    enter() { if (this.activeId != null) this.toggle(this.activeId); },
    toggle(id) {
      this.selectedIds = this.isSelected(id) ? this.selectedIds.filter(x => x !== id) : [...this.selectedIds, id];
      this.q = ''; this.$nextTick(() => { this.ensureActive(); this.$refs.input?.focus(); });
    },
    remove(id) { this.selectedIds = this.selectedIds.filter(x => x !== id); },
    backspace() { if (this.q === '' && this.selectedIds.length) this.selectedIds = this.selectedIds.slice(0, -1); },
    init() { this.$watch('q', () => { this.open = true; this.ensureActive(); }); this.ensureActive(); },
  }));

  /* ---- Admin: add-a-term live chip preview (admin_tags.html) -------------- */
  Alpine.data('tagAdder', (cfg) => ({
    kind: cfg.kind, label: '', ext: '',
    cls: KIND_CLASS,
    get isNamed() { return NAMED_KINDS.includes(this.kind); },
  }));

  /* ---- Admin taxonomy curation filter (admin_tags.html) ------------------ */
  Alpine.data('taxonomyFilter', (cfg) => ({
    kindOrder: cfg.kinds?.length ? cfg.kinds : KIND_ORDER,
    kindClass: KIND_CLASS,
    kindFilter: '', search: '', showRetired: true,

    matches(el) {
      const kind = el.dataset.kind, active = el.dataset.active === 'true';
      const hay = (el.dataset.search || '').toLowerCase();
      const q = this.search.trim().toLowerCase();
      return (this.showRetired || active) &&
             (!this.kindFilter || kind === this.kindFilter) &&
             (!q || hay.includes(q));
    },
  }));
});
