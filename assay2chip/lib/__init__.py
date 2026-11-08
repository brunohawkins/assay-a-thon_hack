"""Shared machinery for the assay2chip stages.

Three modules, and every stage should use all three rather than rolling its own:

  io        read and write a stage file in assay2chip/work/<id>/, schema-validated
            on BOTH read and write, written atomically.
  contracts the ONE place that reads contracts/blocks.json and contracts/parameters.json.
            Never hard-code a number that lives in a contract.
  llm       the ONE interface for language-model calls, with a deterministic offline
            stub so every stage runs in tests with no network.

Import them with the repo root on sys.path (assay2chip/run.py puts it there):

    from assay2chip.lib import io, contracts, llm
"""
