"""Makes `tests` a package.

Two reasons, both mechanical: it gives the mypy override in pyproject.toml a
`tests.*` pattern to match (mypy matches module patterns per dotted component,
so a bare `test_*` matches nothing), and it keeps test module names unambiguous
if two directories ever hold a file of the same name.
"""
