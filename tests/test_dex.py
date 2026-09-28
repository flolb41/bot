import pytest

from bot.dex import DexError, PancakeQuoter, decode_uint_array, encode_get_amounts_out


def test_encode_get_amounts_out_layout():
    usdt = "0x55d398326f99059fF775485246999027B3197955"
    btcb = "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"
    data = encode_get_amounts_out(10**18, [usdt, btcb])

    assert data.startswith("0xd06ca61f")
    body = data[10:]
    words = [body[i : i + 64] for i in range(0, len(body), 64)]
    assert len(words) == 5  # amountIn, offset, length, addr1, addr2
    assert int(words[0], 16) == 10**18
    assert int(words[1], 16) == 0x40
    assert int(words[2], 16) == 2
    assert words[3].endswith(usdt[2:].lower())
    assert words[4].endswith(btcb[2:].lower())


def test_decode_uint_array():
    def w(v: int) -> str:
        return hex(v)[2:].zfill(64)

    payload = "0x" + w(0x20) + w(2) + w(100) + w(83000)
    assert decode_uint_array(payload) == [100, 83000]


def test_decode_uint_array_too_short():
    with pytest.raises(DexError):
        decode_uint_array("0x" + "00" * 10)


def test_spread_pct():
    assert PancakeQuoter.spread_pct(100.0, 101.0) == pytest.approx(1.0)
    assert PancakeQuoter.spread_pct(100.0, 99.0) == pytest.approx(-1.0)
    assert PancakeQuoter.spread_pct(0.0, 99.0) == 0.0


def test_quote_price_unknown_token():
    quoter = PancakeQuoter(rpc_url="http://localhost:0")
    with pytest.raises(DexError):
        quoter.quote_price("XYZ/USDT")


def test_quote_price_uses_best_route(monkeypatch):
    quoter = PancakeQuoter(rpc_url="http://localhost:0")
    calls = []

    def fake_amounts_out(amount_in, path):
        calls.append(path)
        # route directe donne moins de BTC que la route via WBNB
        return [amount_in, 10**15] if len(path) == 2 else [amount_in, 0, 12 * 10**14]

    monkeypatch.setattr(quoter, "amounts_out", fake_amounts_out)
    price = quoter.quote_price("BTC/USDT", notional=100.0)
    assert len(calls) == 2
    # 100 USDT -> 0.0012 BTC => 83333.33 USDT/BTC
    assert price == pytest.approx(100.0 / 0.0012)
