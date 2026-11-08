"""The /api/* implementation for web/server.py. Standard library only.

    paths     where everything is, what an id may be, content types
    stages    the stage spine, with which stage modules exist on disk
    manifest  reading assay2chip/work/<id>/run_manifest.json, and the stage timeline
    llm       which language-model mode is really in force, reported honestly
    sandbox   the /file security boundary: an allowlist, resolved with realpath
    jobs      the one-at-a-time subprocess queue and the event stream behind /events
    chips     every chip in the repo, with its footprint and scale
    http      requests, responses, the error shape, multipart, routing
    routes    the route table from web/README.md, which is authoritative

The API shapes come from web/README.md and nothing here renames any of them.
"""
