"""YouTube -> transcript -> claim-extraction pipeline.

A DESCRIPTIVE ingestion layer (not a decision layer): it records WHAT was claimed in
monitored YouTube playlists, with provenance + a verbatim source quote, into Postgres for a
downstream reasoning model. It never judges whether a claim is correct or tradeable -- all
judgment lives downstream, in one place. See the build spec in docs / the handoff.
"""
