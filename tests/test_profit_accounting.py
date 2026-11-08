from web3 import Web3

from app.trading.executor import _net_token_transfer_from_receipts
from app.trading.fees import receipt_total_gas_cost_wei


WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
QUOTE = "0x" + "c" * 40


def _address_topic(address: str) -> bytes:
    return bytes.fromhex(address[2:].lower()).rjust(32, b"\0")


def _transfer(token: str, sender: str, recipient: str, amount: int) -> dict:
    return {
        "address": token,
        "topics": [
            Web3.keccak(text="Transfer(address,address,uint256)"),
            _address_topic(sender),
            _address_topic(recipient),
        ],
        "data": amount.to_bytes(32, "big"),
    }


def test_receipt_gas_cost_includes_base_l1_fee():
    assert receipt_total_gas_cost_wei({
        "gasUsed": 100,
        "effectiveGasPrice": 3,
        "l1Fee": 50,
    }) == 350


def test_receipt_gas_cost_supports_hex_l1_fee_and_price_fallback():
    assert receipt_total_gas_cost_wei({
        "gasUsed": "0x64",
        "l1Fee": "0x32",
    }, fallback_gas_price_wei=3) == 350


def test_receipt_gas_cost_remains_compatible_when_l1_fee_is_not_provided():
    assert receipt_total_gas_cost_wei({
        "gasUsed": 100,
        "effectiveGasPrice": 3,
    }) == 300


def test_quote_delta_uses_only_transfer_logs_from_included_transactions():
    receipts = [
        {"logs": [_transfer(QUOTE, WALLET, OTHER, 1_000)]},
        {"logs": [
            _transfer(QUOTE, OTHER, WALLET, 1_040),
            _transfer(QUOTE, "0x" + "d" * 40, "0x" + "e" * 40, 900_000),
        ]},
    ]

    assert _net_token_transfer_from_receipts(QUOTE, WALLET, receipts) == 40


def test_quote_delta_returns_none_when_receipts_have_no_matching_transfers():
    receipts = [{"logs": [_transfer(OTHER, WALLET, OTHER, 1_000)]}]

    assert _net_token_transfer_from_receipts(QUOTE, WALLET, receipts) is None
