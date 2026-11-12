import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pytoniq_core import Address, begin_cell

from parsers.message.tonco import TONCOSwap
from model.dexswap import DEX_TONCO, DEX_TONCO_V2


FIXTURES = json.loads((Path(__file__).parent / 'fixtures/tonco_v2_swaps.json').read_text())
# Independently decoded from the five public traces in Dune query 9119482.
EXPECTED_AMOUNTS = [
    (80583714, 73508107297),
    (55000000000, 60366878),
    (526305000000, 580161917),
    (14868750000, 16046878),
    (100000000, 77680780126),
]


@pytest.mark.parametrize('fixture,amounts', list(zip(FIXTURES, EXPECTED_AMOUNTS)))
def test_v2_real_swap_and_pool_discovery(fixture, amounts):
    parser = TONCOSwap()
    obj = fixture['message']
    db = Mock()
    db.is_tx_successful.return_value = True
    db.get_parent_message_body.return_value = bytes.fromhex(fixture['parent_boc'])
    db.get_wallet_master.side_effect = lambda address: address
    assert parser.predicate(obj)

    with patch('parsers.message.tonco.estimate_volume'):
        parser.handle_internal(obj, db)

    db.serialize.assert_called_once()
    swap = db.serialize.call_args.args[0]
    assert swap.platform == DEX_TONCO_V2
    assert (swap.swap_src_amount, swap.swap_dst_amount) == amounts
    assert swap.router == parser.ROUTER_V2
    assert swap.swap_pool == obj['source']
    db.discover_dex_pool.assert_called_once_with(swap)


def test_legacy_layout_is_preserved():
    parser = TONCOSwap()
    owner = Address('0:' + '11' * 32)
    wallet0 = Address('0:' + '22' * 32)
    wallet1 = Address('0:' + '33' * 32)
    coins = (begin_cell().store_coins(0).store_address(wallet0)
             .store_coins(900).store_address(wallet1).end_cell())
    payout = (begin_cell().store_uint(0xa1daa96d, 32).store_uint(7, 64)
              .store_address(owner).store_address(owner).store_uint(200, 32)
              .store_uint(0, 64).store_maybe_ref(coins).end_cell())
    params = begin_cell().store_coins(1000).store_uint(0, 160).store_coins(800).end_cell()
    parent = (begin_cell().store_uint(0xa7fb58f8, 32).store_uint(7, 64)
              .store_address(owner).store_address(wallet0).store_ref(params).end_cell())
    obj = dict(FIXTURES[0]['message'], destination=parser.ROUTER, body_boc=payout.to_boc())
    db = Mock()
    db.is_tx_successful.return_value = True
    db.get_parent_message_body.return_value = parent.to_boc()
    db.get_wallet_master.side_effect = lambda address: address

    with patch('parsers.message.tonco.estimate_volume'):
        parser.handle_internal(obj, db)

    swap = db.serialize.call_args.args[0]
    assert swap.platform == DEX_TONCO
    assert (swap.swap_src_amount, swap.swap_dst_amount) == (1000, 900)
    assert swap.min_out == 800


def test_failed_transaction_does_not_create_a_trade_or_pool():
    db = Mock()
    db.is_tx_successful.return_value = False
    TONCOSwap().handle_internal(FIXTURES[0]['message'], db)
    db.serialize.assert_not_called()
    db.discover_dex_pool.assert_not_called()


@pytest.mark.parametrize('change', [
    {'destination': '0:' + 'FF' * 32},
    {'direction': 'out'},
    {'opcode': 0},
])
def test_unrelated_messages_are_rejected(change):
    assert not TONCOSwap().predicate(dict(FIXTURES[0]['message'], **change))


def test_v2_export_version(monkeypatch):
    # The datalake runs with its directory on sys.path in production.
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3] / 'datalake'))
    from converters.dex_trades import DEX_MAPPING, DEX_VERSION_MAPPING

    assert DEX_MAPPING[DEX_TONCO_V2] == 'tonco'
    assert DEX_VERSION_MAPPING[DEX_TONCO_V2] == 2
    assert DEX_VERSION_MAPPING.get(DEX_TONCO, 1) == 1


@pytest.mark.parametrize('platform,extra_fields', [(DEX_TONCO, 0), (DEX_TONCO_V2, 6)])
def test_pool_state_supports_both_getter_layouts(platform, extra_fields):
    from parsers.accounts.tvl import TVLPoolStateParser
    from model.dexpool import DexPool

    parser = TVLPoolStateParser.__new__(TVLPoolStateParser)
    pool = DexPool('test-pool', platform, None, None)
    parser.pools = {'test-pool': pool}
    token0 = Address('0:' + '44' * 32)
    token1 = Address('0:' + '55' * 32)
    state = [0] * (25 + extra_fields)
    state[5] = begin_cell().store_address(token0).end_cell().begin_parse()
    state[6] = begin_cell().store_address(token1).end_cell().begin_parse()
    state[10], state[11] = 1000, 30
    state[20:23] = [12345, 67890, 42]
    parser._execute_method = Mock(return_value=state)
    db = Mock()

    with patch('parsers.accounts.tvl.estimate_tvl'):
        parser._do_parse({'account': 'test-pool', 'timestamp': 1}, db, Mock())

    assert (pool.reserves_left, pool.reserves_right, pool.total_supply) == (12345, 67890, 42)
    assert (pool.jetton_left, pool.jetton_right) == (token0, token1)
    assert pool.lp_fee == pytest.approx(0.0027)
    assert pool.protocol_fee == pytest.approx(0.0003)
    assert parser._execute_method.call_args.args[1] == 'getPoolStateAndConfiguration'
    db.update_dex_pool_state.assert_called_once_with(pool)
