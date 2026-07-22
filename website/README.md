# Iceberg documentation site

The public docs site — built with [Zensical](https://zensical.org/) (the
Material-based static site generator) from `docs/` + `zensical.toml`, styled to
the shared Iceberg design system (`docs/stylesheets/iceberg.css`, self-hosted
fonts).

Deployed to GitHub Pages by `.github/workflows/docs.yml` on every push to
`main` touching `website/**` (Settings → Pages → Source = "GitHub Actions").
The build pins `zensical==0.0.50` — bump deliberately, in step with the
sibling IcebergTTX/EBS sites.

Preview locally:

```bash
pip install zensical==0.0.50
cd website
zensical serve          # http://localhost:8000, live reload
zensical build --clean  # writes site/ (gitignored)
```

Screenshots under `docs/assets/` are copies of the README set in
`../docs/images/` — regenerate those first (`docs/screenshots/README.md`),
then re-copy the subset used here.
