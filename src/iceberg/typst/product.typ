// =============================================================================
//  Iceberg — intelligence product template  ·  publication-quality PDF
// -----------------------------------------------------------------------------
//  Renders a report (title + metadata + markdown body, optional sources and
//  attachments appendix) to PDF in the house style of the Iceberg app: Archivo
//  display, Spectral prose, JetBrains Mono markings, glacial-cyan accent on cool
//  paper.
//
//  One template, three formats selected via `--input format=FULL|EXEC_BRIEF|
//  ONE_PAGER`; they differ in page geometry, type scale and whether the sources
//  appendix is included.
//
//  Markdown is rendered by the `cmarker` package (fetched from the Typst
//  registry on first use). Body elements are restyled below via show rules so
//  the prose matches the in-app `.md` register exactly.
//
//  Fonts: install Archivo, Spectral and JetBrains Mono (Google Fonts) so Typst
//  can embed them. Sensible fallbacks are listed if they are unavailable.
// =============================================================================

#import "@preview/cmarker:0.1.1"

#let data = json("data.json")
#let fmt  = sys.inputs.at("format", default: "FULL")

// --- Design tokens (mirrors static/css/iceberg.css) -------------------------
#let c-paper       = oklch(98.4%, 0.006, 240deg)
#let c-surface     = white
#let c-surface-2   = oklch(97.2%, 0.008, 240deg)
#let c-surface-ink = oklch(22%,   0.026, 256deg)
#let c-ink         = oklch(26%,   0.026, 262deg)
#let c-ink-soft    = oklch(44%,   0.022, 260deg)
#let c-muted       = oklch(58%,   0.017, 258deg)
#let c-faint       = oklch(70%,   0.012, 258deg)
#let c-line        = oklch(91.5%, 0.008, 248deg)
#let c-line-strong = oklch(85.5%, 0.012, 248deg)
#let c-accent      = oklch(66%,   0.118, 226deg)
#let c-accent-ink  = oklch(50%,   0.115, 234deg)
#let c-accent-deep = oklch(42%,   0.10,  238deg)
#let c-accent-soft = oklch(95.5%, 0.028, 226deg)
#let c-accent-line = oklch(86%,   0.055, 226deg)
#let c-code-fg     = oklch(92%,   0.012, 240deg)
#let tlp-red       = oklch(55%,   0.20,  25deg)
#let tlp-amber     = oklch(70%,   0.15,  70deg)
#let tlp-green     = oklch(62%,   0.14,  155deg)
#let tlp-clear     = oklch(55%,   0.01,  258deg)

#let f-sans  = ("Archivo", "Helvetica Neue", "Arial", "Liberation Sans")
#let f-serif = ("Spectral", "Georgia", "Times New Roman")
#let f-mono  = ("JetBrains Mono", "DejaVu Sans Mono", "Menlo", "Consolas")

// --- Per-format geometry & scale --------------------------------------------
#let cfg = if fmt == "ONE_PAGER" {
  (margin: 1.4cm, size: 8.8pt, title: 16pt, kicker: "One-Pager",       compact: true)
} else if fmt == "EXEC_BRIEF" {
  (margin: 2cm,   size: 10.5pt, title: 20pt, kicker: "Executive Brief", compact: false)
} else {
  (margin: 2.4cm, size: 10.5pt, title: 23pt, kicker: "Full Assessment", compact: false)
}

// --- TLP colour resolved from the marking label -----------------------------
#let tlp-col = if "RED" in data.tlp {
  tlp-red
} else if "AMBER" in data.tlp {
  tlp-amber
} else if "GREEN" in data.tlp {
  tlp-green
} else {
  tlp-clear
}

// --- Small components --------------------------------------------------------
#let brand-mark(s) = box(width: s, height: s, baseline: 22%)[
  #place(top + left, polygon(
    fill: c-accent-ink,
    (s * 0.50, s * 0.10), (s * 0.95, s * 0.52), (s * 0.05, s * 0.52),
  ))
  #place(top + left, polygon(
    fill: c-accent.transparentize(64%),
    (s * 0.06, s * 0.60), (s * 0.94, s * 0.60), (s * 0.50, s * 0.95),
  ))
]

// classification stamp: coloured swatch + mono label
#let stamp(label, swatch: none, fg: c-ink, bg: c-surface, bd: c-line-strong, dot: none) = {
  let lead = if swatch != none {
    (rect(width: 8pt, height: 8pt, radius: 1pt, fill: swatch,
          stroke: 0.5pt + black.transparentize(92%)),)
  } else if dot != none {
    (circle(radius: 3pt, fill: dot, stroke: none),)
  } else {
    ()
  }
  let cells = lead + (text(tracking: 0.6pt, upper(label)),)
  box(inset: (x: 7pt, y: 4.5pt), radius: 4pt, fill: bg, stroke: 0.7pt + bd)[
    #set text(font: f-mono, size: 8pt, weight: "bold", fill: fg)
    #grid(
      columns: if lead.len() > 0 { (auto, auto) } else { (auto,) },
      gutter: 5pt, align: horizon,
      ..cells,
    )
  ]
}

#let title-rule = box(width: 44pt, height: 3pt, radius: 1.5pt, fill: c-accent)

// Taxonomy tag chip — a "catalog stamp" that mirrors the portal's `.tagk` /
// `.k-*` design: a kind-tinted prefix cell (k-soft fill, k-ink text, k-line
// divider) + a neutral body (white) with the external id (k-ink) and label.
// All mono, calmer than the TLP/level/status stamps. Hues match iceberg.css.
#let tag-kind-colors(kind) = if kind == "ACTOR" {
  (ink: oklch(50%, 0.13, 320deg), soft: oklch(96.3%, 0.030, 320deg), line: oklch(88%, 0.050, 320deg))
} else if kind == "CAMPAIGN" {
  (ink: oklch(50%, 0.13, 350deg), soft: oklch(96.3%, 0.030, 350deg), line: oklch(88%, 0.050, 350deg))
} else if kind == "MALWARE" {
  (ink: oklch(50%, 0.13, 286deg), soft: oklch(96.3%, 0.030, 286deg), line: oklch(88%, 0.050, 286deg))
} else if kind == "TECHNIQUE" {
  (ink: oklch(50%, 0.13, 258deg), soft: oklch(96.3%, 0.030, 258deg), line: oklch(88%, 0.050, 258deg))
} else if kind == "SECTOR" {
  (ink: oklch(47%, 0.10, 168deg), soft: oklch(96.3%, 0.030, 168deg), line: oklch(86%, 0.048, 168deg))
} else {
  (ink: oklch(47%, 0.10, 124deg), soft: oklch(96.3%, 0.034, 124deg), line: oklch(86%, 0.050, 124deg))
}

#let tag-chip(t) = {
  let kc = tag-kind-colors(t.kind)
  box(radius: 4pt, clip: true, stroke: 0.6pt + c-line-strong, baseline: 3pt)[
    #grid(
      columns: (auto, auto), align: horizon, inset: (x: 6pt, y: 3.5pt),
      fill: (col, _) => if col == 0 { kc.soft } else { c-surface },
      grid.vline(x: 1, stroke: 0.6pt + kc.line),
      text(font: f-mono, size: 7pt, weight: "bold", tracking: 0.7pt, fill: kc.ink)[#upper(t.kind.slice(0, 3))],
      {
        if t.external_id != "" [#text(font: f-mono, size: 8pt, weight: 600, fill: kc.ink)[#t.external_id]#h(4pt)]
        text(font: f-mono, size: 8.5pt, weight: 500, fill: c-ink)[#t.label]
      },
    )
  ]
}

// styled appendix heading (Sources / Attachments) + accent rule
#let appendix-heading(label) = {
  text(font: f-sans, weight: 800, size: 22pt, fill: c-ink)[#label]
  v(12pt)
  title-rule
  v(18pt)
}

// Key Judgements — the BLUF callout (rendered in every format). Tinted box with
// an accent edge so it reads as the product's leading assessment.
#let kj-block(body-md) = block(
  width: 100%, inset: (x: 16pt, y: 13pt), radius: 5pt, breakable: true,
  fill: c-accent-soft, stroke: (left: 2.5pt + c-accent, rest: 0.6pt + c-accent-line),
)[
  #text(font: f-mono, size: 8.5pt, weight: "bold", tracking: 1.8pt, fill: c-accent-deep)[
    #upper("Key Judgements")
  ]
  #v(7pt)
  #cmarker.render(body-md)
]

// Analytic-scaffolding section (Key Assumptions / Intelligence Gaps): a sans
// heading + thin rule + the markdown field, matching the in-app section style.
#let scaffold-section(label, body-md) = {
  v(1.5em)
  block(width: 100%)[
    #text(font: f-sans, weight: 700, size: 1.16em, fill: c-ink)[#label]
    #v(0.35em, weak: true)
    #line(length: 100%, stroke: 0.6pt + c-line)
  ]
  v(0.8em)
  cmarker.render(body-md)
}

// status → semantic colour
#let status-col = if data.status in ("PUBLISHED", "APPROVED") {
  tlp-green
} else if data.status == "IN_REVIEW" {
  tlp-amber
} else {
  c-muted
}

// =============================================================================
//  Page setup — brandbar, running header/footer
// =============================================================================
#set page(
  paper: "a4",
  margin: cfg.margin,
  background: place(top + left, rect(
    width: 100%, height: 3pt,
    fill: gradient.linear(c-accent-deep, c-accent, c-accent-line, angle: 0deg),
  )),
  header: context {
    set text(font: f-mono, size: 8pt, tracking: 1pt, fill: c-muted)
    grid(columns: (1fr, auto), align: (left + horizon, right + horizon),
      [#box(rect(width: 6pt, height: 6pt, radius: 1.5pt, fill: tlp-col,
                 stroke: 0.5pt + black.transparentize(92%)), baseline: 10%) #h(4pt) #upper(data.tlp)],
      upper(data.intel_level),
    )
  },
  header-ascent: 40%,
  footer: context {
    set text(font: f-mono, size: 8pt, tracking: 1pt, fill: c-muted)
    block(width: 100%, stroke: (top: 0.6pt + c-line), inset: (top: 6pt))[
      #grid(columns: (1fr, auto), align: (left + horizon, right + horizon),
        upper(data.tlp + " — handling per source markings"),
        counter(page).display("1 / 1", both: true),
      )
    ]
  },
  footer-descent: 30%,
)

// --- Base text & paragraph ---------------------------------------------------
#set text(font: f-serif, size: cfg.size, fill: c-ink, lang: "en")
#set par(justify: true, leading: 0.72em)
#show par: set block(spacing: 1.15em)

// =============================================================================
//  Markdown body show rules — match the in-app `.md` prose register
// =============================================================================
#show heading: set text(font: f-sans, fill: c-ink, weight: 700)
#show heading.where(level: 1): it => block(above: 1.5em, below: 0.55em,
  text(size: 1.42em, weight: 800, it.body))
#show heading.where(level: 2): it => block(above: 1.5em, below: 0.7em, width: 100%)[
  #text(size: 1.16em, weight: 700, it.body)
  #v(0.35em, weak: true)
  #line(length: 100%, stroke: 0.6pt + c-line)
]
#show heading.where(level: 3): it => block(above: 1.25em, below: 0.4em,
  text(size: 1.02em, weight: 700, it.body))

#set list(
  marker: box(width: 0.34em, height: 0.34em, radius: 0.5pt, fill: c-accent, baseline: -0.02em),
  body-indent: 0.55em, spacing: 0.78em,
)
#set enum(
  numbering: n => text(font: f-mono, weight: "bold", fill: c-accent-deep, numbering("1.", n)),
  body-indent: 0.5em, spacing: 0.78em,
)

#show link: set text(fill: c-accent-deep)
#show link: underline.with(stroke: 0.5pt + c-accent-line, offset: 2pt)

#show emph: set text(style: "italic")
#show strong: set text(weight: 600, fill: c-ink)

// inline + block code
#show raw.where(block: false): it => box(
  fill: c-surface-2, inset: (x: 3.5pt), outset: (y: 3pt), radius: 3.5pt,
  stroke: 0.5pt + c-line, text(font: f-mono, size: 0.86em, fill: c-accent-deep, it),
)
#show raw.where(block: true): it => block(
  width: 100%, fill: c-surface-ink, inset: (x: 14pt, y: 12pt), radius: 6pt,
  above: 1.3em, below: 1.3em,
)[
  #set text(font: f-mono, size: 0.84em, fill: c-code-fg)
  #it
]

// blockquote
#show quote.where(block: true): it => block(
  width: 100%, inset: (left: 16pt, y: 3pt), above: 1.3em, below: 1.3em,
  stroke: (left: 2.5pt + c-accent-line),
)[
  #set text(style: "italic", fill: c-ink-soft)
  #it.body
]

// tables
#set table(
  stroke: 0.6pt + c-line,
  inset: (x: 9pt, y: 6pt),
  fill: (_, y) => if y == 0 { c-surface-2 },
)
#show table.cell.where(y: 0): set text(font: f-sans, weight: 600, fill: c-ink)
#show table: set text(font: f-sans, size: 0.9em)

// =============================================================================
//  Masthead
// =============================================================================
#block(width: 100%, above: 0pt, below: if cfg.compact { 16pt } else { 24pt })[
  #grid(columns: (1fr, auto), align: (left + horizon, right + horizon),
    grid(columns: (auto, auto), gutter: 9pt, align: horizon,
      brand-mark(if cfg.compact { 16pt } else { 19pt }),
      text(font: f-sans, weight: 800, size: if cfg.compact { 13pt } else { 15pt },
           tracking: 2.4pt, fill: c-ink)[ICEBERG],
    ),
    text(font: f-mono, size: 8pt, tracking: 1.6pt, fill: c-muted)[
      #upper("Intelligence Product · " + cfg.kicker)
    ],
  )
  #v(if cfg.compact { 9pt } else { 14pt })
  #line(length: 100%, stroke: 0.9pt + c-line-strong)
  #v(if cfg.compact { 14pt } else { 22pt })

  // eyebrow + title + accent rule
  #text(font: f-mono, size: 9.5pt, weight: "bold", tracking: 2.4pt, fill: c-accent-deep)[
    #upper(data.intel_level + " Intelligence")
  ]
  #block(above: 10pt, below: 0pt)[
    #set par(leading: 0.32em)
    #text(font: f-sans, weight: 800, size: cfg.title, fill: c-ink)[#data.title]
  ]
  #v(13pt)
  #title-rule

  // byline + markings
  #v(16pt)
  #grid(columns: (1fr, auto), align: (left + bottom, right + bottom), gutter: 16pt,
    text(font: f-sans, size: 10pt, fill: c-muted)[
      Prepared by #text(weight: 600, fill: c-ink-soft)[#data.author] · #data.date
    ],
    box[#stack(dir: ltr, spacing: 7pt,
      stamp(data.tlp, swatch: tlp-col),
      stamp(data.intel_level, fg: c-accent-deep, bg: c-accent-soft, bd: c-accent-line),
      stamp(data.status, fg: status-col, dot: status-col,
            bg: status-col.transparentize(91%), bd: status-col.transparentize(68%)),
    )],
  )
]

#line(length: 100%, stroke: 0.8pt + c-line)

// --- Taxonomy tags (wrapping chip row, if any) ------------------------------
#let report-tags = data.at("tags", default: ())
#if report-tags.len() > 0 {
  v(if cfg.compact { 11pt } else { 15pt })
  block(width: 100%)[
    #set par(leading: 0.95em)
    #for t in report-tags { tag-chip(t); h(5pt) }
  ]
}

#v(if cfg.compact { 12pt } else { 18pt })

// =============================================================================
//  Key Judgements — the BLUF, rendered in every format. Brief formats
//  (EXEC_BRIEF / ONE_PAGER) are Key-Judgements-only products: they carry the
//  masthead, markings and judgements but omit the narrative body and caveats.
// =============================================================================
#let kj = data.at("key_judgements", default: "")
#if kj.trim() != "" {
  kj-block(kj)
} else if fmt != "FULL" {
  // A brief with no judgements would otherwise be near-empty — say so plainly.
  block(width: 100%, inset: (x: 16pt, y: 13pt), radius: 5pt,
        fill: c-surface-2, stroke: 0.6pt + c-line)[
    #text(font: f-serif, style: "italic", fill: c-muted)[No key judgements recorded.]
  ]
}

// =============================================================================
//  Body + analytic caveats (FULL only)
// =============================================================================
#if fmt == "FULL" {
  v(18pt)
  // Override cmarker's `image` so paths resolve relative to THIS template (the
  // temp `--root`) rather than the cmarker package — used for inline Diamond
  // Model diagrams (`diamond-N.svg`, written there per render). Constrained to
  // 92% width so a diagram never overflows the text column in any format.
  cmarker.render(
    data.body_md,
    scope: (
      image: (path, ..args) => align(center, image(path, width: 92%, ..args)),
    ),
  )

  let ka = data.at("key_assumptions", default: "")
  if ka.trim() != "" { scaffold-section("Key Assumptions", ka) }
  let gaps = data.at("intelligence_gaps", default: "")
  if gaps.trim() != "" { scaffold-section("Intelligence Gaps", gaps) }
}

// =============================================================================
//  Appendices (FULL only): cited sources, then cited attachments
// =============================================================================
#let attachments = data.at("attachments", default: ())

#if fmt == "FULL" and (data.sources.len() > 0 or attachments.len() > 0) {
  pagebreak()

  if data.sources.len() > 0 {
    appendix-heading("Sources")
    for (i, s) in data.sources.enumerate() {
      block(breakable: false, above: 0pt, below: 0pt, width: 100%,
        stroke: if i == 0 { none } else { (top: 0.6pt + c-line) },
        inset: (top: if i == 0 { 0pt } else { 13pt }, bottom: 13pt),
      )[
        #grid(columns: (auto, 1fr), gutter: 12pt, align: (right + top, left + top),
          text(font: f-mono, size: 11pt, weight: "bold", fill: c-accent-deep)[[#(i + 1)]],
          [
            #text(font: f-sans, weight: 600, size: 11pt, fill: c-ink)[#s.title]
            #if s.reference != "" {
              h(6pt)
              box(inset: (x: 5pt, y: 1.5pt), radius: 3.5pt, fill: c-surface-2,
                  stroke: 0.5pt + c-line, baseline: 1.5pt,
                  text(font: f-mono, size: 8.5pt, fill: c-accent-deep)[#s.reference])
            }
            #if s.summary != "" {
              v(5pt, weak: true)
              text(font: f-serif, size: 10pt, fill: c-ink-soft)[#s.summary]
            }
          ],
        )
      ]
    }
  }

  if attachments.len() > 0 {
    if data.sources.len() > 0 { v(28pt) }
    appendix-heading("Attachments")
    for (i, a) in attachments.enumerate() {
      block(breakable: false, above: 0pt, below: 0pt, width: 100%,
        stroke: if i == 0 { none } else { (top: 0.6pt + c-line) },
        inset: (top: if i == 0 { 0pt } else { 13pt }, bottom: 13pt),
      )[
        #grid(columns: (auto, 1fr), gutter: 12pt, align: (right + top, left + top),
          text(font: f-mono, size: 11pt, weight: "bold", fill: c-accent-deep)[[#(i + 1)]],
          [
            #box(inset: (x: 5pt, y: 1.5pt), radius: 3.5pt, fill: c-surface-2,
                stroke: 0.5pt + c-line,
                text(font: f-mono, size: 9pt, weight: "bold", fill: c-accent-deep)[#a.filename])
            #if a.summary != "" {
              v(6pt, weak: true)
              text(font: f-serif, size: 10pt, fill: c-ink-soft)[#a.summary]
            }
          ],
        )
      ]
    }
  }
}
