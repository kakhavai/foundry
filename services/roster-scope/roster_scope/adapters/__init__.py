"""roster-scope's two upstreams.

Two, not one — and that is the honest shape rather than an accident.
`depth_chart` is the ordered-player feed; `identity` is the `player-identity`
collector reached over HTTP. Every slot is filled by a resolved `player_id`,
never a raw name, so neither adapter can stand in for the other.
"""
