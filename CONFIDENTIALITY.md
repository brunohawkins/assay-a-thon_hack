# Confidentiality

This is a competition project with confidentiality rules, and the repo is private, but
**private is not a licence to commit sensitive material**. Repos get shared, forked,
screenshotted and demoed. If in doubt, leave it out and ask MJ.

## Never commit

- Lab protocol details (actual step-by-step methods from any lab)
- Primers, or any oligo or barcode designs or sequences
- Buffer recipes or compositions
- Lysis conditions
- Enzymes (names, amounts or conditions)
- Cycle numbers
- Lab data or results of any kind
- Cell lines
- Differentiation protocols
- Vendor quotes, prices or supplier correspondence
- Lab file names, paths or internal identifiers
- Third-party work that isn't published (other teams' or labs' designs, code or data)
- API keys or passwords (`.env` and `*.key` are git-ignored; keep it that way)

## Fine to commit

- Anything from published papers, cited in the protocol's `source` field
- Public vendor specifications (datasheets, catalogue dimensions)
- Placeholder values: obviously fake and generic (`lysis_buffer`, `rt_mix`, `wash_buffer`,
  round numbers), in files marked `"placeholder": true`
- Numbers we generate this weekend (our own chip geometry, simulations, timings)

## How we keep it that way

- **MJ reviews anything that touches the protocol before it's pushed.** No exceptions.
- Use generic reagent names, never product names.
- Keep anything local-only in `private/`, which is git-ignored.
- If something sensitive gets committed, **tell Bruno immediately**. Don't just delete
  it in a new commit: it stays in the history and has to be scrubbed properly.
