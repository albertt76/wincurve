"""NHL season-points projection module of the multi-sport system.

Mirrors the NBA project's method -- bottom-up player impact, point-in-time
walk-forward backtesting, market prices strictly downstream -- adapted to hockey:
the standings currency is POINTS with the overtime "loser point", goaltending is a
separate volatile module, and special teams (power play / penalty kill) are rated
apart from even strength. See ``nhl/DESIGN.md``.
"""
