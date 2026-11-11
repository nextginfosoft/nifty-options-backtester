"""
Broker switch for live market data.

``BROKER=fyers`` (default) uses ``fyers_apiv3``; ``BROKER=kite`` uses Zerodha Kite
Connect through ``data.kite_adapter`` (same ``FyersModel`` call surface).

Usage in place of ``from fyers_apiv3 import fyersModel``::

    from data.broker import fyersModel
    client = fyersModel.FyersModel(client_id=..., token=..., log_path="")
"""

import os
import types

from utils.env_loader import load_env


def broker_name() -> str:
    load_env()
    return (os.getenv("BROKER") or "fyers").strip().lower()


def _build() -> types.ModuleType:
    if broker_name() == "kite":
        from data.kite_adapter import KiteFyersAdapter

        shim = types.ModuleType("kite_fyers_shim")
        shim.FyersModel = KiteFyersAdapter
        return shim
    from fyers_apiv3 import fyersModel as _real

    return _real


fyersModel = _build()
