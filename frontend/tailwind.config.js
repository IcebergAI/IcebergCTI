/** @type {import('tailwindcss').Config} */
// Compiled to a static, minified, version-pinned stylesheet by
// scripts/vendor_assets.py (no Node — the standalone Tailwind CLI binary).
// The theme.extend below maps Tailwind's colour/font tokens onto the oklch CSS
// variables defined in src/iceberg/static/css/iceberg.css — kept verbatim from
// the former inline `tailwind.config` in base.html.
module.exports = {
  content: [
    "./src/iceberg/templates/**/*.html",
    "./src/iceberg/static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        paper: "var(--paper)",
        surface: "var(--surface)",
        "surface-2": "var(--surface-2)",
        ink: "var(--ink)",
        "ink-soft": "var(--ink-soft)",
        muted: "var(--muted)",
        faint: "var(--faint)",
        line: "var(--line)",
        accent: "var(--accent)",
        "accent-ink": "var(--accent-ink)",
        "accent-deep": "var(--accent-deep)",
        "accent-soft": "var(--accent-soft)",
      },
      fontFamily: {
        sans: ["Archivo", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
        serif: ["Spectral", "Georgia", "serif"],
      },
    },
  },
  plugins: [],
};
