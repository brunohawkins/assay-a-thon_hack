# How we work

Six people, one repo, ~28 hours. These rules keep us out of each other's way.

## The rules

1. **Work in your own folder.** `protocols/` MJ + Kilian, `chip/` Yuvraj + Szonja,
   `cad/` Ayrton, `operator/` and `twin/` Bruno.
2. **Never edit another pair's folder without asking them first.** If you need
   something changed, message the owner. GitHub will also ask them to review
   (see `.github/CODEOWNERS`).
3. **Pull before you push.** `git pull --rebase` before every push.
4. **Small commits, often.** One change per commit, with a message that says what and why
   ("Add wash step before lysis", not "update").
5. **Run the validator before pushing.** `python tools/validate.py` must say `OK`.
   It also runs on GitHub for every push; a red ✗ means something's broken.
6. **Contracts change only by agreement.** Files in `contracts/` are shared by everyone.
   Talk to Bruno before changing them.
7. **Read [CONFIDENTIALITY.md](CONFIDENTIALITY.md).** MJ reviews anything that touches
   the protocol before it's pushed.

## Hand-off times (Saturday)

| Time | What | From → to |
|---|---|---|
| 14:30 | `protocol.json` (steps) | MJ + Kilian → everyone |
| 16:00 | `chip_spec.json` | Yuvraj + Szonja → Ayrton, Bruno |
| 16:30 | physics numbers (volumes, flows, times, temperatures in `protocol.json`) | MJ + Kilian → Bruno |
| 17:00 | first STL | Ayrton → Bruno |

Until each hand-off, build against the placeholder files. That's what they're for.

## Git, step by step

```bash
git pull --rebase                 # get everyone's latest work
# ...edit files in your folder...
python tools/validate.py          # must say OK
git add protocols/my_protocol.json
git commit -m "Add first real scRNA-seq protocol draft"
git pull --rebase                 # again, in case someone pushed meanwhile
git push
```

If `git pull` reports a conflict in a file you don't own, stop and ask its owner.

## After changing a protocol or chip spec

Regenerate the runs so the twin shows your change:

```bash
python operator/run.py --all                                  # every run again, on its own chip
python operator/run.py --all --chip chip/<your_spec>.json     # or every protocol on your chip
```

Commit the updated files in `runs/` too.
