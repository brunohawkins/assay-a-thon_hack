# nl_to_spec (owners: Yuvraj + Szonja)

Turns a plain-English description of an assay chip into a `chip_spec.json`.

```
python chip/nl_to_spec/nl_to_spec.py chip/nl_to_spec/prompts/example_prompt.txt
# -> writes chip/nl_to_spec/out/chip_spec.json
python chip/nl_to_spec/nl_to_spec.py my_prompt.txt --out chip/nl_to_spec/out/my_chip.json
```

**Status: stub.** `generate_spec()` ignores the prompt and returns a copy of
`chip/placeholder_chip_spec.json`, so the rest of the pipeline always has a valid spec.

## To build it

- Replace `generate_spec(prompt) -> dict` in `nl_to_spec.py`. Rules, an LLM, or both.
- The script validates the output against `contracts/schemas/chip_spec.schema.json`
  before writing, and prints readable errors if it doesn't fit.
- Output goes to `out/` (git-ignored). When a spec is good, copy it to `chip/<name>.json`
  and commit it; that's what the operator and twin use.
- If you call the Claude API, read the key from the `ANTHROPIC_API_KEY` environment
  variable. Never put keys in code or commit them. Requirements are stdlib +
  `jsonschema` only, so use `urllib` like `operator/llm_operator.py` does, or agree
  a new dependency with the team first.
- Put example prompts in `prompts/`.
