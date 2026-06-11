/* Iceberg — tagging interactions (Alpine component factories).
 * Loaded globally by base.html so x-data="tagPicker(...)" / x-data="taxonomyAdmin(...)"
 * resolve. Defined on window before Alpine initialises (both scripts are deferred,
 * this one after the Alpine tag, so it runs before Alpine's DOMContentLoaded start).
 *
 * KIND_ORDER and KIND_CLASS are the single source of truth for facet order and
 * the chip colour class — keep in sync with models.TagKind and iceberg.css (.k-*).
 */
(function () {
  const KIND_ORDER = ['ACTOR', 'CAMPAIGN', 'MALWARE', 'TECHNIQUE', 'SECTOR', 'TOPIC'];
  const KIND_CLASS = {
    ACTOR: 'k-actor', CAMPAIGN: 'k-campaign', MALWARE: 'k-malware',
    TECHNIQUE: 'k-technique', SECTOR: 'k-sector', TOPIC: 'k-topic',
  };

  /* ---- Report classification: searchable token combobox -------------------- */
  window.tagPicker = function ({ tags, selectedIds, canTag }) {
    return {
      all: tags || [],
      selectedIds: Array.isArray(selectedIds) ? [...selectedIds] : [],
      initialIds: Array.isArray(selectedIds) ? [...selectedIds] : [],
      canTag: canTag !== false,
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
        for (const t of this.menuItems) (g[t.kind] ||= []).push(t);
        return KIND_ORDER.filter(k => g[k]).map(k => ({ kind: k, items: g[k] }));
      },
      get flat() { return this.groups.flatMap(g => g.items); },

      focusInput() { if (this.canTag) { this.open = true; this.$refs.input && this.$refs.input.focus(); } },
      ensureActive() { const f = this.flat; if (!f.some(t => t.id === this.activeId)) this.activeId = f.length ? f[0].id : null; },
      move(d) { this.open = true; const f = this.flat; if (!f.length) { this.activeId = null; return; } let i = f.findIndex(t => t.id === this.activeId); i = (i + d + f.length) % f.length; this.activeId = f[i].id; },
      enter() { if (this.activeId != null) this.toggle(this.activeId); },
      toggle(id) {
        this.selectedIds = this.isSelected(id) ? this.selectedIds.filter(x => x !== id) : [...this.selectedIds, id];
        this.q = ''; this.$nextTick(() => { this.ensureActive(); this.$refs.input && this.$refs.input.focus(); });
      },
      remove(id) { this.selectedIds = this.selectedIds.filter(x => x !== id); },
      backspace() { if (this.q === '' && this.selectedIds.length) this.selectedIds = this.selectedIds.slice(0, -1); },
      init() { this.$watch('q', () => { this.open = true; this.ensureActive(); }); this.ensureActive(); },
    };
  };

  /* ---- Admin taxonomy curation -------------------------------------------- */
  /* Reads server-rendered rows; the live page submits via the per-row <form>s
   * (POST /admin/tags, /admin/tags/{id}, /admin/tags/{id}/delete). This factory
   * only powers client-side filter/search/live-preview — see admin_tags.html. */
  window.taxonomyFilter = function ({ kinds }) {
    return {
      kindOrder: kinds && kinds.length ? kinds : KIND_ORDER,
      kindClass: KIND_CLASS,
      kindFilter: '', search: '', showRetired: true,
      draft: { kind: (kinds && kinds[0]) || 'ACTOR', label: '', ext: '' },

      matches(el) {
        const kind = el.dataset.kind, active = el.dataset.active === 'true';
        const hay = (el.dataset.search || '').toLowerCase();
        const q = this.search.trim().toLowerCase();
        return (this.showRetired || active) &&
               (!this.kindFilter || kind === this.kindFilter) &&
               (!q || hay.includes(q));
      },
    };
  };
})();
