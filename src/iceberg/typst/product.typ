// Iceberg intelligence product template.
//
// Renders a report (title + metadata + markdown body, optional sources
// appendix) to PDF. One template serves three formats selected via
// `--input format=FULL|EXEC_BRIEF|ONE_PAGER`; they differ in page geometry,
// type size and whether the sources appendix is included.
//
// Markdown is rendered by the `cmarker` package. If your Typst install cannot
// fetch this exact version, change the version below to one it has.
#import "@preview/cmarker:0.1.1"

#let fmt = sys.inputs.at("format", default: "FULL")
#let data = json("data.json")

#let geometry = if fmt == "ONE_PAGER" {
  (margin: 1.4cm, size: 9pt)
} else if fmt == "EXEC_BRIEF" {
  (margin: 2cm, size: 11pt)
} else {
  (margin: 2.5cm, size: 11pt)
}

#set page(
  paper: "a4",
  margin: geometry.margin,
  header: context [
    #set text(8pt, fill: gray)
    #data.tlp #h(1fr) #data.intel_level
  ],
  footer: context [
    #set text(8pt, fill: gray)
    #data.tlp #h(1fr) #counter(page).display("1 / 1", both: true)
  ],
)
#set text(size: geometry.size)
#set par(justify: true)

#align(center)[
  #text(17pt, weight: "bold")[#data.title]
  #linebreak()
  #text(9pt, fill: gray)[
    #data.intel_level · #data.tlp · #data.date · #data.author
  ]
]
#v(0.4em)
#line(length: 100%, stroke: 0.5pt + gray)
#v(0.8em)

#cmarker.render(data.body_md)

#if fmt == "FULL" and data.sources.len() > 0 [
  #pagebreak()
  #text(14pt, weight: "bold")[Sources]
  #v(0.5em)
  #for s in data.sources [
    #strong(s.title)
    #if s.reference != "" [ — #raw(s.reference) ]
    #if s.summary != "" [
      #linebreak()
      #s.summary
    ]
    #v(0.4em)
  ]
]
