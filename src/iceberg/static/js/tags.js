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
// TLP marking → badge class, mirroring templates/_macros.html `tlp_badge` so a
// client-side appended indicator row matches the server-rendered ones.
const IOC_TLP_CLASS = {
  RED: 'tlp--red', AMBER_STRICT: 'tlp--amber tlp--strict',
  AMBER: 'tlp--amber', GREEN: 'tlp--green', CLEAR: 'tlp--clear',
};

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
    cmdOpen: false, cmdQ: '', cmdItems: jumpItems || [], cmdActive: 0, cmdOpener: null,
    get cmdResults() {
      const q = this.cmdQ.trim().toLowerCase();
      return q
        ? this.cmdItems.filter(i => i.label.toLowerCase().includes(q) || i.group.toLowerCase().includes(q))
        : this.cmdItems;
    },
    get cmdActiveDescendant() {
      const results = this.cmdResults;
      return this.cmdOpen && results.length
        ? this.cmdOptionId(Math.min(this.cmdActive, results.length - 1))
        : '';
    },
    cmdOptionId(index) { return `cmdk-option-${index}`; },
    openCmd(opener) {
      if (this.cmdOpen) return;
      this.cmdOpener = opener instanceof HTMLElement ? opener : document.activeElement;
      this.cmdOpen = true;
      this.cmdQ = '';
      this.cmdActive = 0;
      this.$nextTick(() => this.$refs.cmdInput?.focus());
    },
    closeCmd() {
      if (!this.cmdOpen) return;
      const opener = this.cmdOpener;
      this.cmdOpen = false;
      this.cmdQ = '';
      this.cmdActive = 0;
      this.$nextTick(() => {
        if (opener?.isConnected && typeof opener.focus === 'function') opener.focus({ preventScroll: true });
      });
    },
    cmdReset() { this.cmdActive = 0; },
    cmdGo(index = this.cmdActive) {
      const result = this.cmdResults[index];
      if (result) window.location.assign(result.href);
    },
    cmdMove(delta) {
      const count = this.cmdResults.length;
      if (!count) return;
      this.cmdActive = (this.cmdActive + delta + count) % count;
      this.$nextTick(() => document.getElementById(this.cmdOptionId(this.cmdActive))?.scrollIntoView({ block: 'nearest' }));
    },
    trapCmdFocus(event) {
      if (!this.cmdOpen) return;
      const dialog = this.$refs.cmdDialog;
      if (!dialog) return;
      const focusable = [...dialog.querySelectorAll('button, [href], input, select, textarea, [tabindex]')]
        .filter(el => !el.disabled && el.tabIndex >= 0 && el.offsetParent !== null);
      if (!focusable.length) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },
  }));

  /* ---- Report editor (report_edit.html) ---------------------------------- */
  Alpine.data('reportEditor', (dataId) => ({
    ...readJSON(dataId),  // body, kj, ka, gaps, previewHtml, warnings, reportId, canEdit
    tab: 'cite', insertOpen: false, justSaved: false,
    tabOrder: [], dirty: false, saving: false, timer: null, saveTimer: null,
    generation: 0, savedGeneration: 0, savePromise: null, saveQueued: false,
    aiLoading: '', aiApplying: false, aiStatus: '', aiStatusKind: '',
    aiJudgements: null, aiTagIds: [], aiChallenge: '',
    intelLevel: '', tlp: '', tlpKey: '', confidence: '', confKey: '',

    init() {
      this.tabOrder = [...this.$el.querySelectorAll('[data-editor-tab]')]
        .map(el => el.dataset.editorTab);
      this.initMarkings();
    },

    /* ---- header marking chips (intel level / TLP / analytic confidence) ----
       Each chip shows the selected option's own text, so the server-rendered
       label and the hydrated one are identical and there is no label map to
       keep in sync with the enums. */
    initMarkings() {
      this.$el.querySelectorAll('.marking-chip-select')
        .forEach((sel) => { this.readMarking(sel); });
    },
    readMarking(sel) {
      const opt = sel.options[sel.selectedIndex];
      const label = opt ? opt.textContent.trim() : '';
      if (sel.name === 'intel_level') {
        this.intelLevel = label;
      } else if (sel.name === 'tlp') {
        this.tlp = label; this.tlpKey = sel.value;
      } else if (sel.name === 'analytic_confidence') {
        this.confidence = label; this.confKey = sel.value;
      }
    },
    pickMarking(event) {
      this.readMarking(event.target);
      this.markDirty();
    },
    selectTab(id, focus = false) {
      if (this.tabOrder.length && !this.tabOrder.includes(id)) return;
      this.tab = id;
      if (focus) this.$nextTick(() => this.$el.querySelector(`[data-editor-tab="${id}"]`)?.focus());
    },
    moveTab(delta) {
      if (!this.tabOrder.length) return;
      const current = Math.max(0, this.tabOrder.indexOf(this.tab));
      const next = (current + delta + this.tabOrder.length) % this.tabOrder.length;
      this.selectTab(this.tabOrder[next], true);
    },
    firstTab() { if (this.tabOrder.length) this.selectTab(this.tabOrder[0], true); },
    lastTab() { if (this.tabOrder.length) this.selectTab(this.tabOrder[this.tabOrder.length - 1], true); },

    markDirty() { this.generation += 1; this.dirty = true; this.scheduleSave(); },
    schedule() { this.generation += 1; this.dirty = true; clearTimeout(this.timer); this.timer = setTimeout(() => this.refresh(), 350); this.scheduleSave(); },
    scheduleSave() { clearTimeout(this.saveTimer); this.saveTimer = setTimeout(() => this.autosave(), 1200); },
    async saveNow() {
      const form = document.getElementById('reportform');
      if (!form) return false;
      if (this.savePromise) {
        this.saveQueued = true;
        return this.savePromise;
      }
      const generation = this.generation;
      this.saving = true;
      this.savePromise = (async () => {
        try {
          const res = await fetch(form.action, {
            method: 'POST', body: new FormData(form), redirect: 'manual',
            headers: { 'X-Requested-With': 'fetch' },
          });
          if (!res.ok) return false;
          const data = await res.json();
          this.version = data.version;
          this.savedGeneration = generation;
          this.dirty = this.generation !== generation;
          return true;
        } catch {
          this.dirty = true;
          return false;
        } finally {
          this.saving = false;
          this.savePromise = null;
          if (this.saveQueued || this.generation !== generation) {
            this.saveQueued = false;
            this.saveTimer = setTimeout(() => this.autosave(), 0);
          }
        }
      })();
      return this.savePromise;
    },
    async autosave() { await this.saveNow(); },
    async refresh() {
      try {
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
        if (res.ok) {
          const data = await res.json();
          this.previewHtml = data.html;
          this.warnings = data.warnings || [];
        }
      } catch { /* Preview is a non-blocking authoring aid. */ }
    },
    // The CSP build prohibits x-html; render the (server-sanitised) preview HTML
    // through a ref in native JS instead, re-run reactively by x-effect.
    renderPreview() { if (this.$refs.preview) this.$refs.preview.innerHTML = this.previewHtml; },

    // ---- Report-level AI review ------------------------------------------
    // All suggestion state remains in this component until an analyst explicitly
    // applies it. Applying uses the ordinary report save endpoint first, and only
    // then stamps the accepted fields through the existing provenance endpoint.
    setAiStatus(message = '', kind = '') { this.aiStatus = message; this.aiStatusKind = kind; },
    suggestionText(value) {
      if (Array.isArray(value)) return value.map(item => this.suggestionText(item)).filter(Boolean).join('\n');
      if (value && typeof value === 'object') return JSON.stringify(value, null, 2);
      return value == null ? '' : String(value);
    },
    suggestionField(suggestion, names) {
      for (const name of names) {
        if (Object.hasOwn(suggestion || {}, name)) {
          return this.suggestionText(suggestion[name]);
        }
      }
      return '';
    },
    taxonomyTags() {
      const tags = readJSON('taxonomy-data');
      return Array.isArray(tags) ? tags : [];
    },
    tagLabel(id) {
      const tag = this.taxonomyTags().find(item => item.id === id);
      return tag ? `${tag.kind} · ${tag.ext ? `${tag.ext} · ` : ''}${tag.label}` : `Tag #${id}`;
    },
    async requestAi(task) {
      if (this.aiLoading || this.aiApplying) return;
      this.aiLoading = task;
      this.setAiStatus();
      clearTimeout(this.saveTimer);
      // AI endpoints operate on the persisted report. Preserve the editor's
      // existing autosave contract before asking for a second look.
      if (this.dirty && !(await this.saveNow())) {
        this.setAiStatus('Save the draft before requesting AI assistance.', 'warn');
        this.aiLoading = '';
        return;
      }
      const endpoints = {
        judgements: '/api/ai/judgements',
        tags: '/api/ai/suggest-tags',
        challenge: '/api/ai/challenge',
      };
      try {
        const response = await fetch(endpoints[task], {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
          body: JSON.stringify({ report_id: this.reportId }),
        });
        let data = {};
        try { data = await response.json(); } catch { /* Report a useful fail-soft message below. */ }
        if (!response.ok || !data.available) {
          this.setAiStatus(data.message || 'AI assistance is unavailable. Editing remains available.', 'warn');
          return;
        }
        const suggestion = data.suggestion || {};
        if (task === 'judgements') {
          const draft = {
            key_judgements: this.suggestionField(suggestion, ['key_judgements', 'judgements']),
            key_assumptions: this.suggestionField(suggestion, ['key_assumptions', 'assumptions']),
            intelligence_gaps: this.suggestionField(suggestion, ['intelligence_gaps', 'gaps']),
          };
          if (!Object.values(draft).some(Boolean) && Object.keys(suggestion).length) {
            draft.key_judgements = this.suggestionText(suggestion);
          }
          this.aiJudgements = draft;
          this.setAiStatus('Draft judgements are ready to review.', 'ok');
        } else if (task === 'tags') {
          const known = this.taxonomyTags();
          const ids = Array.isArray(suggestion.tag_ids) ? suggestion.tag_ids : [];
          this.aiTagIds = [...new Set(ids.map(Number))]
            .filter(id => known.some(tag => tag.id === id && tag.active));
          this.setAiStatus(
            this.aiTagIds.length ? 'Suggested taxonomy terms are ready to review.' : 'No active taxonomy terms were suggested.',
            this.aiTagIds.length ? 'ok' : 'warn',
          );
        } else {
          this.aiChallenge = this.suggestionField(suggestion, ['challenge_notes', 'challenges', 'challenge', 'notes'])
            || this.suggestionText(suggestion);
          this.setAiStatus(this.aiChallenge ? 'Challenge notes are ready to review.' : 'No challenge notes were suggested.', this.aiChallenge ? 'ok' : 'warn');
        }
      } catch {
        this.setAiStatus('AI request failed. Editing remains available.', 'warn');
      } finally { this.aiLoading = ''; }
    },
    discardAi(task) {
      if (task === 'judgements') this.aiJudgements = null;
      else if (task === 'tags') this.aiTagIds = [];
      else this.aiChallenge = '';
      this.setAiStatus('Suggestion discarded.', 'ok');
    },
    async applyAiReportFields(fields) {
      if (this.aiApplying || !fields.length) return false;
      this.aiApplying = true;
      clearTimeout(this.timer);
      clearTimeout(this.saveTimer);
      this.dirty = true;
      await this.refresh();
      if (!(await this.saveNow())) {
        this.setAiStatus('Suggestion is in the editor, but the report could not be saved. Try Save draft before accepting it.', 'warn');
        this.aiApplying = false;
        return false;
      }
      try {
        const response = await fetch('/api/ai/accept-provenance', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
          body: JSON.stringify({ resource_type: 'report', resource_id: this.reportId, fields }),
        });
        if (!response.ok) {
          this.setAiStatus('The report was saved, but AI provenance could not be recorded.', 'warn');
          return false;
        }
        this.setAiStatus('Suggestion applied and AI provenance recorded.', 'ok');
        return true;
      } catch {
        this.setAiStatus('The report was saved, but AI provenance could not be recorded.', 'warn');
        return false;
      } finally { this.aiApplying = false; }
    },
    async applyAiJudgements() {
      const draft = this.aiJudgements || {};
      const fieldMap = [
        ['key_judgements', 'kj'],
        ['key_assumptions', 'ka'],
        ['intelligence_gaps', 'gaps'],
      ];
      const accepted = [];
      for (const [source, target] of fieldMap) {
        const value = this.suggestionText(draft[source]);
        if (value.trim()) {
          this[target] = value;
          accepted.push(source);
        }
      }
      if (!accepted.length) {
        this.setAiStatus('Add at least one proposed field before applying it.', 'warn');
        return;
      }
      if (await this.applyAiReportFields(accepted)) this.aiJudgements = null;
    },
    applyAiTags() {
      if (!this.aiTagIds.length) return;
      window.dispatchEvent(new CustomEvent('iceberg-ai-tags', { detail: { ids: [...this.aiTagIds] } }));
      this.aiTagIds = [];
      this.selectTab('tags');
      this.setAiStatus('Suggested terms were added to the tag picker. Review them and use Save tags to persist.', 'ok');
    },
    async applyAiChallenge() {
      const note = this.aiChallenge.trim();
      if (!note) {
        this.setAiStatus('Add challenge notes before applying them.', 'warn');
        return;
      }
      const existing = (this.gaps || '').trim();
      this.gaps = `${existing}${existing ? '\n\n' : ''}### Analytic challenge\n\n${note}`;
      if (await this.applyAiReportFields(['intelligence_gaps'])) this.aiChallenge = '';
    },

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
  /* ---- Notebook phase tabs (notebook_detail.html) ------------------------
     The notebook is worked in phases (collect → analyze → produce → trace)
     rather than scrolled end to end, so one phase's sections render at a time.
     The tabs are real anchors: without Alpine the <noscript> rule cancels
     x-cloak and every section stays reachable by its #id, exactly as before. */
  Alpine.data('notebookTabs', (dataId) => ({
    phase: 'collect',
    section: 'sources',
    // {section: phase}, rendered by the template from the same list that builds
    // the tab bar — so the two can never disagree about which phase owns a
    // section.
    sectionPhase: readJSON(dataId),

    init() {
      // Every post-action redirect lands on #<section> (e.g. creating a Diamond
      // model returns to /notebooks/{id}#diamonds). Without this the target sits
      // in a cloaked phase and the page looks like the work vanished.
      this.applyHash();
      window.addEventListener('hashchange', () => this.applyHash());
    },
    applyHash() {
      const section = window.location.hash.slice(1);
      const phase = this.sectionPhase[section];
      if (!phase) return;
      this.show(phase, section);
      // The browser already tried to scroll here while the section was hidden,
      // so re-run it once the phase is visible.
      this.$nextTick(() => document.getElementById(section)?.scrollIntoView());
    },
    show(phase, section) { this.phase = phase; this.section = section; },
  }));

  /* ---- Stakeholder feed filter (feed.html) --------------------------------
     All ⇄ Unread over the already-rendered rows. All is the server-rendered
     default, so the page is complete before (and without) hydration. */
  Alpine.data('feedFilter', () => ({ unreadOnly: false }));

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

  /* ---- AI IOC suggestion review (notebook_detail.html) ------------------- */
  // Advisory, human-in-the-loop: scan a source for candidate indicators, then
  // accept (promote to a real IOC via the existing create endpoint), edit or
  // discard each. Nothing is created until the analyst accepts a row.
  Alpine.data('iocReview', (dataId) => ({
    ...readJSON(dataId),  // notebookId, iocTypes, count (existing indicator rows)
    sourceId: '', loading: false, message: '', candidates: [],
    init() { this.count = Number(this.count) || 0; },
    async suggest() {
      if (!this.sourceId || this.loading) return;
      this.loading = true; this.message = ''; this.candidates = [];
      try {
        const res = await fetch('/api/ai/extract-iocs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
          body: JSON.stringify({ source_id: Number(this.sourceId) }),
        });
        const data = await res.json();
        if (!res.ok || !data.available) {
          this.message = data.message || 'AI assist is unavailable.';
        } else {
          this.candidates = (data.suggestion.candidates || []).map(c => ({ ...c }));
          this.message = this.candidates.length ? '' : 'No indicators suggested from this source.';
        }
      } catch { this.message = 'AI request failed.'; }
      this.loading = false;
    },
    async accept(idx) {
      const c = this.candidates[idx];
      if (!c?.value) return;
      try {
        const res = await fetch(`/api/notebooks/${this.notebookId}/iocs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
          body: JSON.stringify({
            ioc_type: c.ioc_type, value: c.value,
            description: c.description || '', source_id: Number(this.sourceId),
          }),
        });
        if (res.ok) {
          // Reflect the just-created IOC in the Indicators table above without a
          // full reload (#118); the create response carries the persisted row.
          let created = null;
          try { created = await res.json(); } catch { /* row append is best-effort */ }
          if (created?.id) this.appendRow(created);
          this.candidates.splice(idx, 1);
        } else { this.message = 'Could not add that indicator.'; }
      } catch { this.message = 'Request failed.'; }
    },
    // Build a row matching the server-rendered ones with safe DOM APIs
    // (textContent, not innerHTML — the CSP build forbids x-html anyway).
    appendRow(ioc) {
      const body = this.$refs.iocBody;
      if (!body || !ioc?.id) return;
      const row = document.createElement('tr');

      const typeTd = document.createElement('td');
      const typeTag = document.createElement('span');
      typeTag.className = 'tag';
      const meta = (this.iocTypes || []).find(t => t.value === ioc.ioc_type);
      typeTag.textContent = meta ? meta.label : ioc.ioc_type;
      typeTd.appendChild(typeTag);

      const valueTd = document.createElement('td');
      valueTd.className = 'mono break-all';
      valueTd.textContent = ioc.value;

      const tlpTd = document.createElement('td');
      const tlpTag = document.createElement('span');
      tlpTag.className = `tag tlp ${IOC_TLP_CLASS[ioc.tlp] || 'tlp--clear'}`;
      const swatch = document.createElement('span');
      swatch.className = 'swatch';
      tlpTag.appendChild(swatch);
      tlpTag.append(`TLP:${String(ioc.tlp || '').replace('_', '+')}`);
      tlpTd.appendChild(tlpTag);

      const ctxTd = document.createElement('td');
      ctxTd.style.color = 'var(--muted)';
      ctxTd.textContent = ioc.description || '';

      const actionTd = document.createElement('td');
      const form = document.createElement('form');
      form.method = 'post';
      form.action = `/notebooks/${this.notebookId}/iocs/${ioc.id}/delete`;
      form.addEventListener('submit', (e) => {
        if (!window.confirm('Delete this indicator?')) e.preventDefault();
      });
      const del = document.createElement('button');
      del.className = 'link-danger';
      del.style.cssText = 'background:none;border:0;cursor:pointer;';
      del.textContent = 'Delete';
      form.appendChild(del);
      actionTd.appendChild(form);

      row.append(typeTd, valueTd, tlpTd, ctxTd, actionTd);
      body.appendChild(row);
      this.count++;
    },
    discard(idx) { this.candidates.splice(idx, 1); },
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
    idPrefix: cfg.idPrefix || 'tag-picker',
    q: '', open: false, activeId: null, justSaved: false, announcement: '',
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
    get inputId() { return `${this.idPrefix}-input`; },
    get listboxId() { return `${this.idPrefix}-listbox`; },
    get activeDescendant() {
      return this.open && this.activeId != null ? this.optionId(this.activeId) : '';
    },

    optionId(id) { return `${this.idPrefix}-option-${id}`; },
    groupId(kind) { return `${this.idPrefix}-group-${kind.toLowerCase()}`; },
    focusInput() {
      if (!this.canTag) return;
      this.openMenu();
      this.$refs.input?.focus();
    },
    openMenu() {
      if (!this.canTag) return;
      this.open = true;
      this.ensureActive();
    },
    close() { this.open = false; this.activeId = null; },
    dismiss() {
      this.close();
      this.announcement = 'Tag suggestions dismissed.';
      this.$refs.input?.focus();
    },
    ensureActive() { const f = this.flat; if (!f.some(t => t.id === this.activeId)) this.activeId = f.length ? f[0].id : null; },
    move(d) {
      this.openMenu();
      const f = this.flat;
      if (!f.length) { this.activeId = null; return; }
      let i = f.findIndex(t => t.id === this.activeId);
      i = (i + d + f.length) % f.length;
      this.activeId = f[i].id;
      this.$nextTick(() => document.getElementById(this.optionId(this.activeId))?.scrollIntoView({ block: 'nearest' }));
    },
    enter() { if (this.activeId != null) this.toggle(this.activeId); },
    toggle(id) {
      const tag = this.byId(id);
      const selected = this.isSelected(id);
      this.selectedIds = selected ? this.selectedIds.filter(x => x !== id) : [...this.selectedIds, id];
      this.announcement = `${tag?.label || 'Tag'} ${selected ? 'removed' : 'added'}.`;
      this.q = ''; this.$nextTick(() => { this.ensureActive(); this.$refs.input?.focus(); });
    },
    remove(id) {
      const tag = this.byId(id);
      this.selectedIds = this.selectedIds.filter(x => x !== id);
      this.announcement = `${tag?.label || 'Tag'} removed.`;
    },
    backspace() {
      if (this.q !== '' || !this.selectedIds.length) return;
      this.remove(this.selectedIds[this.selectedIds.length - 1]);
    },
    applySuggestedTags(ids) {
      if (!this.canTag || !Array.isArray(ids)) return;
      const offerable = new Set(this.all.filter(tag => tag.active).map(tag => tag.id));
      const additions = [...new Set(ids.map(Number))].filter(id => offerable.has(id) && !this.isSelected(id));
      if (!additions.length) {
        this.announcement = 'No new suggested tags to add.';
        return;
      }
      this.selectedIds = [...this.selectedIds, ...additions];
      this.announcement = `${additions.length} suggested tag${additions.length === 1 ? '' : 's'} added. Review and save tags to persist.`;
      this.close();
      this.$nextTick(() => this.$refs.input?.focus());
    },
    init() {
      this.$watch('q', () => { if (this.canTag) this.openMenu(); });
      this.ensureActive();
    },
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

  // Read-only effective-config page (/admin/config): a text filter over the
  // resolved settings rows. Each row carries its searchable text in data-search.
  Alpine.data('configFilter', () => ({
    search: '',
    matches(el) {
      const q = this.search.trim().toLowerCase();
      return !q || (el.dataset.search || '').toLowerCase().includes(q);
    },
  }));
});
