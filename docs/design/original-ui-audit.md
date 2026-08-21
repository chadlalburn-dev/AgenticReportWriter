# Audit of the original UI

Recorded so the same patterns are not reintroduced. Ranked by how much credibility each cost.
Each was verified against the original Flask/Jinja templates and rendered screenshots, not
assumed.

**1. The front door asked the wrong question, then over-equipped itself to answer it.** The
landing page was a gallery of report types; the compound ID was the first field of a form two
clicks in. Six cards were governed by a search field, a Group-by select, a Sort-within-groups
select, an Apply button, a tag filter panel and an active-filter summary — six controls for
six objects. Fixed by making the compound the front door and the gallery a secondary browse
surface.

**2. Three near-identical gallery screens shipped as separate states** (grouped by domain,
grouped by discipline, filtered). Shipping a faceting UI as the primary surface means the
product has no opinion about what the user is there to do.

**3. The gallery card carried nine data classes at one visual weight** — title, description,
chips, source counts, readiness fraction, section count, version, owner, actions. Nine things
at equal weight is zero hierarchy.

**4. Trust signals were diluted by being everywhere.** Readiness fraction and source mix are
the only things on that card that justify the product's existence, and they sat next to
version and owner.

**5. The run page's coverage strip was a sticky hero row of five stat numbers, three of them
zero.** `4/4 · 4 · 0 · 1 · 0` at 24px/300 in five bordered boxes, pinned to the top, where it
physically occluded the `<h1>` in every scrolled state. Replaced by one line of text plus the
tick strip in the identity band.

**6. Type was bottom-heavy in application.** Across the stylesheet: 12px in 34 rules, 14px in
23, 16px in 14, and 20/24/28px in 12 rules combined. Roughly two-thirds of all declared text
sat in the bottom two steps, so every page rendered as one even grey texture with no entry
point. The scale itself was fine; nothing used the top of it.

**7. The card was used as a paragraph break, not an object boundary.** A two-line banner, a
six-row preflight table, a forty-row source dump and an entire form all sat in the identical
12px-radius, 1px-stroke, 24px-padded box. When every container is the same container,
containment stops carrying meaning.

**8. The same warning was stated three times in three components.** On run setup: a full amber
block per unready source, then a "what will be missing" table repeating all four with the same
sentences, then an amber summary panel listing the affected sections. Three renderings of one
fact, together taking more vertical space than the form they qualified. Now stated once, in
the binding row, with one footnote.

**9. Deterministic data and model-written prose were not visually distinguished** in the draft
view — the core epistemic contract of the product was carried entirely by citation
superscripts. Addressed by the provenance gutter and by reserving the recessed surface to mean
"retrieved".

**10. Citation review was a modal round trip.** Click marker, panel opens, read source, close,
find your place again. The core task of the core screen. Now resolves in place in the gutter.

**11. Preflight was a block inside run setup rather than a state of the compound.** Source
readiness is a property of "what do we know about this compound", knowable before a report
type is chosen. Now the compound page's main content.

**12. Source ledger rendered twice in two component languages.** Sources tab used sentence-case
titles, a definition list and pill status; the draft tab used uppercase mono headings, an inline
meta line and a differently-shaped chip. Same objects, same run, two vocabularies, one screen.

**13. Status chips contained sentences.** "Pulled but not cited — 1 row went unused by the
draft." Inside bordered pills sized for two words. A chip states the state; the explanation
belongs in the row below it where it can wrap.

**14. Nested horizontal scrollbars inside bordered cards inside a scrolling page** — five
stacked on the run page, each data preview with its own inner scroll and visible grey bar.

**15. No empty states and no first-run.** Absence of designed zero-states is the most reliable
amateur marker there is. Still outstanding — not designed in this redesign either.

**16. The template editor was a flat stack of ~40 inputs with no spatial model.** "Source 1 /
potency", "Source 3 / pivotal_tox" repeating identically down the page; reordering via
`Move up` / `Move down` text links at the bottom of each block; a permanent 12px helper
sentence under every field, so close to half the editor's vertical space was instructional
prose. You could not tell how many sources a template had without scrolling to the end.
Recommendation: move behind an admin route and redesign separately — it is a different job for
a different person.

## What was NOT wrong

`gsk.css` itself is well engineered: complete token layer, two-tone focus rings that survive
tinted surfaces, `strong` carrying an underline rule because only weights 300/400 exist, real
dark mode, print styles. The problems above are decisions the stylesheet does not govern. If
implementing into that same app, expect to rewrite templates and add tokens rather than start
over.
