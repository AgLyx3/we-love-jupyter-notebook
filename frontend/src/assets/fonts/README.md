# Vendored webfonts

Three families, fetched from Google Fonts once and committed here rather than
linked at runtime. `scripts/vendor-fonts.mjs` produced them and
`../../fonts.css` declares them; neither file is edited by hand.

The tab used to link `fonts.googleapis.com`. That put an external request on
every load of an editor whose whole claim is that it runs on your machine, and
on a host that could not reach Google it left every icon rendering as its own
ligature name — `menu_book` clipped to a square reading `u_` (#51).

| Family | Licence | Notes |
| --- | --- | --- |
| Inter | [SIL Open Font License 1.1](https://openfontlicense.org/) | Weights 400/500/600 share one variable file per script subset. |
| JetBrains Mono | [SIL Open Font License 1.1](https://openfontlicense.org/) | Weights 400/500, same arrangement. |
| Material Symbols Outlined | [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) | Subsetted to the icon names the app renders. |

Both licences permit redistribution of the font files, including bundled inside
another work, and neither is copyleft over the software they ship with. The OFL
asks that the fonts not be sold on their own and that a modified font be
renamed — the files here are unmodified, apart from the subsetting Google's own
API performs.

## Regenerating

    node scripts/vendor-fonts.mjs

Run it when a font changes and, the case that actually comes up, when an
`<Icon>` uses a name no other icon does. Material Symbols is subsetted to the
names the script finds in `frontend/src`, so an icon that is not in the subset
renders as a blank box — and looks perfectly fine to anyone who happens to have
the full font cached from another site.
