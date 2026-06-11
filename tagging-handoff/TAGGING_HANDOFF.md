# Tagging UI — improvements handoff

A focused pass on the **controlled-taxonomy tagging** surfaces, which the
editorial redesign had left unstyled. Same stack and contracts as the main
handoff: Jinja2 + Tailwind (CDN) + Alpine.js. **No backend, route, schema, form
`action`, or field `name` changes.** Every endpoint and POST body is unchanged —
this is a skin + interaction-model pass plus one new shared JS file.

## What was missing

The redesign skinned the status / TLP / priority / level markings but never
defined the **taxonomy tag chips**, and the rewritten templates had dropped:

- the `tag_chip` macro from `_macros.html`,
- all `.tagk*` / `.chip-check` CSS from `iceberg.css`,
- the **Tags** section from `report_edit.html`,
- the **admin taxonomy** screen (`admin_tags.html`) from the redesign set.

So tagging was the one subsystem with no redesigned UI. This pass restores all
four and upgrades two of them.

## Files to drop into `src/iceberg/`

```
templates/_macros.html        (adds the tag_chip macro back — new structure)
templates/base.html           (loads /static/js/tags.js; adds Taxonomy nav for ADMIN)
templates/report_edit.html    (new Tags section: searchable token combobox)
templates/admin_tags.html     (redesigned curation screen)
static/css/iceberg.css        (adds the tagging block — see “CSS” below)
static/js/tags.js             (new — Alpine component factories)
```

`static/js/tags.js` is served by the existing StaticFiles mount; `base.html`
links it deferred, after Alpine, so `window.tagPicker` / `window.taxonomyFilter`
exist before Alpine evaluates `x-data`.

## The design system for tags

A tag chip is a quiet **catalog stamp**, deliberately calmer than the loud TLP /
status markings (many tags ride on one report, so they lean on a small colour
cue, not a fill):

```
┌─────┬───────────────────┐
│ ACT │ G0016  APT29       │   .tagk.k-actor
└─────┴───────────────────┘
  ^kind   ^ext   ^label
```

- **Structure** (the macro emits exactly this — keep it in sync with the CSS):
  ```html
  <span class="tagk k-actor">
    <span class="tagk-kind">ACT</span>
    <span class="tagk-body">
      <span class="tagk-ext">G0016</span>
      <span class="tagk-label">APT29</span>
    </span>
  </span>
  ```
- **Six kinds, six hues** — one harmonious family (shared lightness/chroma, hue
  varies), each clear of the TLP red/amber/green and the glacial-cyan accent:
  `.k-actor .k-campaign .k-malware .k-technique .k-sector .k-topic`.
- **Retired** tags (`active = false`) render struck-through + dimmed via
  `.is-retired`. They stay on historical reports but are never offered for new
  tagging.
- `.tagk--sm` is a compact variant for dense rows (report lists / feed).
- `KIND_ORDER` and `KIND_CLASS` in `tags.js` are the single source of truth for
  facet order + chip class. Keep them in sync with `models.TagKind` and the
  `.k-*` rules.

## 1 · Report classification — searchable token combobox

Replaces the flat checkbox grid in `report_edit.html`. Selected tags are chips
in the control (each with an × to remove); typing filters the controlled
vocabulary, **grouped by kind**, with keyboard nav (↑/↓/Enter/Esc, Backspace
deletes the last chip). Only **active** terms are suggested; an already-applied
**retired** tag still shows as a struck chip so the analyst can see and remove
it.

**Contract preserved.** The component renders one hidden
`<input name="tag_ids" value="…">` per selected id, inside the same
`<form method="post" action="/reports/{id}/tags">`. The server sees the **exact
same payload** as the old checkbox grid — no endpoint change. A "Save tags"
button is disabled until the selection is dirty; the existing `updated == 'tags'`
flash still renders.

Data is handed to Alpine via a `<script type="application/json"
id="taxonomy-data">` block built from the existing `all_tags` context (id, kind,
label, ext, desc, active). `linked_tag_ids` and `can_tag` are passed straight
into `tagPicker({...})`. **No new context variables are required.**

> If JS is disabled the picker control still renders the current chips and posts
> them (hidden inputs are server-rendered from `selectedIds`), but editing needs
> JS. If you require a no-JS fallback, keep the old `chip-check` grid behind a
> `<noscript>` — the markup is in git history.

## 2 · Admin taxonomy curation — `admin_tags.html`

Redesigned, still **form-based (works without JS)**:

- Header **counter** (total terms · active count).
- **Add a term** card with a live chip **preview** (Alpine-only; posts as a
  normal form to `POST /admin/tags`).
- **Filter** by kind (pills), free-text **search**, and a **Show retired**
  toggle — all client-side via `taxonomyFilter()` (`x-show="matches($el)"` on
  each row reads `data-kind` / `data-active` / `data-search`).
- Kind-grouped rows: chip preview · inline label/ext/description edit
  (`POST /admin/tags/{id}`) · an **Active/Retired switch** that auto-submits its
  form (`onchange="this.form.requestSubmit()"`) · a delete button
  (`POST /admin/tags/{id}/delete`) guarded by a confirm that nudges toward
  retiring instead of deleting.

Context used: `kinds` (list of `TagKind`) and `tags_by_kind` (dict
`TagKind → [Tag]`) — both already provided by the existing admin route. **No new
context variables.**

> The **retire-don't-delete** guard is currently a JS `confirm()` copy nudge. If
> you want it enforced, have the delete service refuse when the tag has report
> links and surface a flash — a small server change, noted here as a follow-up,
> not done in this pass.

## CSS

All additions live in one block in `iceberg.css` (search
`Taxonomy tag chips`). It introduces: the six `.k-*` hue holders, `.tagk*`,
`.kindtab`, the `.tagpick*` combobox, `.tax-*` curation rows, `.switch` toggle,
`.icon-action`, the `.add-*` form helpers, and `.counter-*`. It reuses existing
tokens (`--line-strong`, `--c-ok`, `--c-warn`, `--ring`, `--radius-sm`, …) — no
new design tokens.

## Preview

`preview/report-edit.html` (Tags section) and `preview/taxonomy.html` are
clickable static renders with the **real starter taxonomy** baked in
(`preview/taxonomy-data.js`, 94 terms; "Ryuk" is flagged retired to demonstrate
the retired states). Open `preview/index.html` → "Report editor" / "Taxonomy".
The preview chips/picker/admin use the **same class names and markup** as the
templates, so what you review is what ships.

## Follow-ups (not done — flagged for decision)

1. Enforce retire-don't-delete server-side (refuse delete when report links
   exist) rather than the client confirm nudge.
2. Optional no-JS `<noscript>` checkbox-grid fallback for classification.
3. `/tags/{id}` browse-by-tag listing was out of scope here; the chips already
   link to it via the `tag_chip` macro (`link=True`).
