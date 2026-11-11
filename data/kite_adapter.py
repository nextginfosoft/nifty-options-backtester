"""
Zerodha Kite Connect adapter exposing the subset of ``fyers_apiv3.fyersModel.FyersModel``
that this project uses (``quotes`` and ``optionchain``), returning Fyers-shaped
responses so the signal / monitor code in ``main.py`` works unchanged.

Select it with ``BROKER=kite`` (see ``data/broker.py``). Credentials come from the
environment (``.env.local``): ``KITE_API_KEY`` and ``KITE_ACCESS_TOKEN``. This module
never prints or logs them. Read-only: it only reads quotes and the instrument list,
and never places orders.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# Fyers symbol -> Kite quote key for the two index symbols the project asks for.
_INDEX_MAP = {
    "NSE:NIFTY50-INDEX": "NSE:NIFTY 50",
    "NSE:INDIAVIX-INDEX": "NSE:INDIA VIX",
}

_QUOTE_BATCH = 400  # Kite allows up to 500 instruments per quote call

# Module-level cache so the several short-lived adapters main.py creates share one download.
_instrument_cache: Dict[str, Any] = {"day": None, "rows": []}


def _err(code: int, message: str) -> Dict[str, Any]:
    return {"s": "error", "code": code, "message": message}


class KiteFyersAdapter:
    """Drop-in stand-in for ``FyersModel`` backed by Kite Connect."""

    def __init__(self, client_id: Optional[str] = None, token: Optional[str] = None,
                 log_path: str = "", kite: Any = None, **_ignored):
        # client_id / token are the Fyers-named values main.py passes; we use KITE_* env
        # instead (the env loader aliases them, but the explicit KITE_* values win).
        if kite is None:
            from kiteconnect import KiteConnect

            api_key = os.getenv("KITE_API_KEY") or client_id
            access_token = os.getenv("KITE_ACCESS_TOKEN") or token
            kite = KiteConnect(api_key=api_key)
            if access_token:
                kite.set_access_token(access_token)
        self._kite = kite

    # ------------------------------------------------------------------ helpers
    def _fail(self, exc: Exception) -> Dict[str, Any]:
        # Fyers reports an expired token as code -15; main.py keys its warning off that.
        if exc.__class__.__name__ in ("TokenException", "PermissionException"):
            return _err(-15, f"Kite token invalid/expired: {exc}")
        return _err(-1, f"{exc.__class__.__name__}: {exc}")

    def _nfo_nifty(self) -> List[Dict[str, Any]]:
        today = date.today()
        if _instrument_cache["day"] != today:
            rows = self._kite.instruments("NFO")
            _instrument_cache["rows"] = [
                r for r in rows
                if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")
            ]
            _instrument_cache["day"] = today
        return _instrument_cache["rows"]

    def _raw_quotes(self, keys: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for i in range(0, len(keys), _QUOTE_BATCH):
            out.update(self._kite.quote(keys[i:i + _QUOTE_BATCH]) or {})
        return out

    @staticmethod
    def _values(q: Dict[str, Any]) -> Dict[str, Any]:
        depth = q.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0].get("price", 0) or 0
        sell = (depth.get("sell") or [{}])[0].get("price", 0) or 0
        return {
            "lp": q.get("last_price", 0) or 0,
            "bid": buy,
            "ask": sell,
            "oi": q.get("oi", 0) or 0,
            "volume": q.get("volume", 0) or 0,
        }

    # ------------------------------------------------------------------ Fyers API
    def quotes(self, data: Dict[str, str]) -> Dict[str, Any]:
        """Fyers ``quotes``: ``{"symbols": "A,B"}`` -> ``{"s": "ok", "d": [{"n", "v"}]}``."""
        requested = [s for s in data.get("symbols", "").split(",") if s]
        kite_keys = [_INDEX_MAP.get(s, s) for s in requested]
        try:
            raw = self._raw_quotes(kite_keys)
        except Exception as exc:  # noqa: BLE001 - mapped to a Fyers-style error dict
            return self._fail(exc)
        d = []
        for orig, key in zip(requested, kite_keys):
            if key in raw:
                d.append({"n": orig, "v": self._values(raw[key])})
        if not d:
            return _err(-1, "Kite returned no quotes for the requested symbols")
        return {"s": "ok", "d": d}

    def leg_quotes(self, legs: List[Tuple[date, float, str]]) -> Dict[str, Any]:
        """Live quotes for exact option legs plus the Nifty spot.

        ``legs`` is a list of (expiry, strike, "CE"/"PE"). Returns ``{"spot": float|None,
        "legs": {(expiry, strike, type): {"ltp", "bid", "ask", "oi", "volume"}}}``; legs with no matching
        instrument (e.g. already expired) are simply absent. Raises on API errors (the caller maps them).
        """
        symbol_for = {(r["expiry"], float(r["strike"]), r["instrument_type"]): r["tradingsymbol"]
                      for r in self._nfo_nifty()}
        wanted: Dict[Tuple[date, float, str], str] = {}
        for expiry, strike, opt_type in legs:
            sym = symbol_for.get((expiry, float(strike), opt_type))
            if sym:
                wanted["NFO:" + sym] = (expiry, float(strike), opt_type)
        raw = self._raw_quotes(["NSE:NIFTY 50"] + list(wanted))
        out = {wanted[k]: self._values(raw[k]) for k in wanted if k in raw}
        spot = (raw.get("NSE:NIFTY 50") or {}).get("last_price")
        return {"spot": spot, "legs": out}

    def optionchain(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Fyers ``optionchain`` for NIFTY.

        Empty ``timestamp``: returns ``expiryData`` (upcoming expiries). With a
        ``timestamp`` (the ISO date this adapter put in ``expiryData``): also returns
        ``optionsChain`` for the ``strikecount`` strikes either side of ATM.
        """
        try:
            rows = self._nfo_nifty()
            today = date.today()
            expiries = sorted({r["expiry"] for r in rows if r["expiry"] >= today})
            last_of_month: Dict[tuple, date] = {}
            for e in expiries:
                last_of_month[(e.year, e.month)] = e  # sorted -> last wins
            expiry_data = [
                {
                    "date": e.strftime("%d-%m-%Y"),
                    "expiry": e.isoformat(),
                    "expiry_flag": "M" if last_of_month[(e.year, e.month)] == e else "W",
                }
                for e in expiries
            ]
            result: Dict[str, Any] = {"expiryData": expiry_data, "optionsChain": []}

            ts = data.get("timestamp") or ""
            if ts:
                target = datetime.strptime(ts, "%Y-%m-%d").date()
                spot = self._raw_quotes(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
                n = max(int(data.get("strikecount", 10)), 1)
                strikes = sorted({r["strike"] for r in rows if r["expiry"] == target})
                if strikes:
                    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
                    keep = set(strikes[max(atm_i - n, 0): atm_i + n + 1])
                    chosen = [r for r in rows if r["expiry"] == target and r["strike"] in keep]
                    keys = ["NFO:" + r["tradingsymbol"] for r in chosen]
                    raw = self._raw_quotes(keys)
                    for r, key in zip(chosen, keys):
                        if key not in raw:
                            continue
                        v = self._values(raw[key])
                        result["optionsChain"].append({
                            "symbol": key,
                            "strike_price": r["strike"],
                            "option_type": r["instrument_type"],
                            "ltp": v["lp"], "bid": v["bid"], "ask": v["ask"],
                            "oi": v["oi"], "volume": v["volume"],
                        })
            return {"s": "ok", "code": 200, "data": result}
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
