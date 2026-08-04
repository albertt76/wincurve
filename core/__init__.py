"""Sport-agnostic core shared across the multi-sport projection system.

This package holds machinery that does not depend on any one sport: the throttled,
disk-cached HTTP client (``httpcache``), and -- as the NHL/soccer modules mature and
the real seams become visible -- the walk-forward backtest harness, the Monte Carlo
season simulator (with a pluggable outcome model), and the market-comparison layer.

Extraction is deliberately incremental. Code earns its place in ``core`` only once a
second sport actually needs it; premature "sport-agnostic" abstractions tend to model
the wrong seams. The NBA project (``nbaproj``) is not refactored onto core until the
NHL build shows what is genuinely common.
"""
