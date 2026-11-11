"""KiteFyersAdapter returns Fyers-shaped responses (no network, fake Kite client)."""

from datetime import date, timedelta

import pytest

from data import kite_adapter
from data.kite_adapter import KiteFyersAdapter


class TokenException(Exception):
    pass


class FakeKite:
    def __init__(self):
        today = date.today()
        self.w1 = today + timedelta(days=5)
        self.w2 = today + timedelta(days=12)
        self.rows = []
        for exp in (self.w1, self.w2):
            for strike in (24900, 24950, 25000, 25050, 25100):
                for typ in ("CE", "PE"):
                    self.rows.append({
                        "name": "NIFTY", "instrument_type": typ, "expiry": exp,
                        "strike": float(strike),
                        "tradingsymbol": f"NIFTY{exp:%y%m%d}{strike}{typ}",
                    })
        self.rows.append({"name": "BANKNIFTY", "instrument_type": "CE", "expiry": self.w1,
                          "strike": 55000.0, "tradingsymbol": "BANKNIFTYX"})
        self.fail = False

    def instruments(self, exchange):
        assert exchange == "NFO"
        return self.rows

    def quote(self, keys):
        if self.fail:
            raise TokenException("bad token")
        out = {}
        for k in keys:
            if k == "NSE:NIFTY 50":
                out[k] = {"last_price": 25012.0}
            elif k == "NSE:INDIA VIX":
                out[k] = {"last_price": 17.4}
            else:
                out[k] = {"last_price": 100.0, "oi": 5000, "volume": 900,
                          "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100.5}]}}
        return out


@pytest.fixture(autouse=True)
def _reset_cache():
    kite_adapter._instrument_cache.update(day=None, rows=[])


def test_index_quotes_use_fyers_names():
    a = KiteFyersAdapter(kite=FakeKite())
    r = a.quotes({"symbols": "NSE:NIFTY50-INDEX,NSE:INDIAVIX-INDEX"})
    assert r["s"] == "ok"
    by_name = {i["n"]: i["v"]["lp"] for i in r["d"]}
    assert by_name == {"NSE:NIFTY50-INDEX": 25012.0, "NSE:INDIAVIX-INDEX": 17.4}


def test_expired_token_maps_to_fyers_code_minus_15():
    k = FakeKite()
    k.fail = True
    r = KiteFyersAdapter(kite=k).quotes({"symbols": "NSE:NIFTY50-INDEX"})
    assert r["s"] == "error" and r["code"] == -15


def test_optionchain_expiries_and_chain_shape():
    k = FakeKite()
    a = KiteFyersAdapter(kite=k)
    meta = a.optionchain({"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""})
    exp = meta["data"]["expiryData"]
    assert [e["expiry"] for e in exp] == [k.w1.isoformat(), k.w2.isoformat()]
    assert exp[0]["date"] == k.w1.strftime("%d-%m-%Y")

    chain = a.optionchain({"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1,
                           "timestamp": exp[0]["expiry"]})["data"]["optionsChain"]
    assert {c["strike_price"] for c in chain} == {24950.0, 25000.0, 25050.0}  # ATM +/- 1
    assert {c["option_type"] for c in chain} == {"CE", "PE"}
    first = chain[0]
    assert first["symbol"].startswith("NFO:NIFTY")
    assert (first["ltp"], first["bid"], first["ask"], first["oi"], first["volume"]) == \
        (100.0, 99.5, 100.5, 5000, 900)
    assert "BANKNIFTY" not in " ".join(c["symbol"] for c in chain)


def test_option_symbols_from_chain_round_trip_through_quotes():
    a = KiteFyersAdapter(kite=FakeKite())
    ts = a.optionchain({"strikecount": 1, "timestamp": ""})["data"]["expiryData"][0]["expiry"]
    sym = a.optionchain({"strikecount": 1, "timestamp": ts})["data"]["optionsChain"][0]["symbol"]
    r = a.quotes({"symbols": sym})
    assert r["s"] == "ok" and r["d"][0]["n"] == sym and r["d"][0]["v"]["lp"] == 100.0
