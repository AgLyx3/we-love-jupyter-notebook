/** One Material Symbols Outlined glyph (stitch-diff §A1).
 *
 *  The library is a ligature webfont, so an icon's *name* is its text content.
 *  That is why every glyph is `aria-hidden`: without it the accessible name of
 *  a labelled button would read "check Keep", and of an icon-only button it
 *  would read the ligature instead of its `aria-label`.
 *
 *  styles.css sizes the span as a 1em box with `overflow: hidden`. A Material
 *  Symbols glyph is exactly 1em wide, so nothing is clipped when the font is
 *  there — and when it is not, the literal "keyboard_tab_rtl" is clipped to
 *  that square rather than blowing out the flex row it sits in.
 *
 *  `name` has to be a literal. The font is vendored (assets/fonts) and
 *  subsetted to the names scripts/vendor-fonts.mjs can find in this directory
 *  tree, so a name assembled at runtime is an icon nobody vendored: a blank
 *  box, and only for people who do not already have the full font cached from
 *  another site. The script refuses to run rather than let one through, and a
 *  new literal means re-running it — `npm run vendor:fonts`.
 */
export default function Icon({ name, filled = false, className }: { name: string; filled?: boolean; className?: string }) {
  return <span className={`material-symbols-outlined${filled ? " filled" : ""}${className ? ` ${className}` : ""}`} aria-hidden="true" translate="no">{name}</span>;
}
