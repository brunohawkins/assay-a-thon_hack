# Confidentiality

This is a competition project with confidentiality rules, and the repo is private, but
**private is not a licence to commit sensitive material**. Repos get shared, forked,
screenshotted and demoed. If in doubt, leave it out and ask MJ.

## The hard line

**Nothing from the lab's Labstep or OneDrive goes in this repo.** Not copied, not
summarised, not paraphrased, not transcribed into a JSON field. That is the one rule with
no judgement call attached: if you found it in either of those two places, it does not
belong here. Everything in the next section is the same rule applied to the things that
most often leak.

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
- **Check binaries by opening them, not by reading the diff.** A spreadsheet, document or
  image reveals nothing to `git grep` or to a pull-request review, so anything sensitive
  inside one passes every check we have. Say what a binary contains when you add it.
- Keep anything local-only in `private/`, which is git-ignored.
- If something sensitive gets committed, **tell Bruno immediately**. Don't just delete
  it in a new commit: it stays in the history and has to be scrubbed properly.
