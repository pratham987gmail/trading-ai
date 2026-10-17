"""Historical market-data storage layer for the Upstox research integration.

This package is additive research infrastructure. It does not touch Angel One
(the live-trading broker) and does not modify ``core/option_store.py``. See
``store.py`` for why a separate database exists rather than folding Upstox
history into the existing option-chain store.
"""
