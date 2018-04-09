'''
A simple Python script to deploy contracts and then do a smoke test for them.
'''
import click
from populus import Project
from eth_utils import (
    is_address,
    to_checksum_address,
    encode_hex
)
from utils.utils import (
    check_succesful_tx,
    wait,
)

from web3 import Web3,HTTPProvider
from web3.middleware.pythonic import (
    pythonic_middleware,
    to_hexbytes,
)

import json

@click.command()
@click.option(
    '--chain',
    default='kovan',
    help='Chain to deploy on: kovan | ropsten | rinkeby | tester | privtest'
)
@click.option(
    '--owner',
    help='Contracts owner, default: web3.eth.accounts[0]'
)
@click.option(
    '--challenge-period',
    default=500,
    help='Challenge period in number of blocks.'
)
@click.option(
    '--supply',
    default=10000000,
    help='Token contract supply (number of total issued tokens).'
)
@click.option(
    '--token-name',
    default='CustomToken',
    help='Token contract name.'
)
@click.option(
    '--token-decimals',
    default=18,
    help='Token contract number of decimals.'
)
@click.option(
    '--token-symbol',
    default='TKN',
    help='Token contract symbol.'
)
@click.option(
    '--token-address',
    default=None,
    help='Already deployed token address.'
)
@click.option(
    '--output-json',
    help='Testing flag to output config file.'
)
def main(**kwargs):
    project = Project()

    chain_name = kwargs['chain']
    owner = kwargs['owner']
    challenge_period = kwargs['challenge_period']
    supply = kwargs['supply']
    token_name = kwargs['token_name']
    token_decimals = kwargs['token_decimals']
    token_symbol = kwargs['token_symbol']
    token_address = kwargs['token_address']
    output_json = kwargs['output_json']
    supply *= 10**(token_decimals)
    txn_wait = 250

    #assert challenge_period >= 500, 'Challenge period should be >= 500 blocks'

# PART OF TEMP FIX TO ISSUE 414 IN PULL REQUEST: https://github.com/raiden-network/microraiden/pull/416
#    if chain_name == 'rinkeby':
#        txn_wait = 500
# end

    print('''Make sure {} chain is running, you can connect to it and it is synced,
          or you'll get timeout'''.format(chain_name))

    with project.get_chain(chain_name) as chain:
        web3 = chain.web3
        print('Web3 provider is', web3.providers[0])

        # Temporary fix for Rinkeby; PoA adds bytes to extraData, which is not yellow-paper-compliant
        # https://github.com/ethereum/web3.py/issues/549
        if int(web3.version.network) == 4:
            txn_wait = 500
            size_extraData_for_poa = 200
            pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByNumber'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)
            pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByHash'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)
        # end

        owner = owner or web3.eth.accounts[0]
        assert owner and is_address(owner), 'Invalid owner provided.'
        owner = to_checksum_address(owner)
        print('Owner is', owner)
        assert web3.eth.getBalance(owner) > 0, 'Account with insuficient funds.'

        token = chain.provider.get_contract_factory('CustomToken')

        if not token_address:
            print("Trying to deploy token.")
            txhash = token.deploy(
                args=[supply, token_name, token_symbol, token_decimals],
                transaction={'from': owner}
            )
            receipt = check_succesful_tx(chain.web3, txhash, txn_wait)
            token_address = receipt['contractAddress']

        assert token_address and is_address(token_address)
        token_address = to_checksum_address(token_address)
        print(token_name, 'address is', token_address)
        print(token_name, 'tx is', encode_hex(txhash))

        microraiden_contract = chain.provider.get_contract_factory('RaidenMicroTransferChannels')
        txhash = microraiden_contract.deploy(
            args=[token_address, challenge_period, []],
            transaction={'from': owner}
        )
        receipt = check_succesful_tx(chain.web3, txhash, txn_wait)
        microraiden_address = receipt['contractAddress']

        print('RaidenMicroTransferChannels txid is', encode_hex(txhash))
        print('RaidenMicroTransferChannels address is', microraiden_address)

        owner = web3.eth.accounts[1]
        assert owner and is_address(owner), 'Invalid owner provided.'
        owner = to_checksum_address(owner)
        print('Owner is', owner)
        assert web3.eth.getBalance(owner) > 0, 'Account with insuficient funds.'
        
        guardian_contract = chain.provider.get_contract_factory('StateGuardian')
        txhash = guardian_contract.deploy(
            args=[],
            transaction={'from': owner}
        )
        receipt = check_succesful_tx(chain.web3, txhash, txn_wait)
        guardian_address = receipt['contractAddress']

        print('StateGuardian tx is', encode_hex(txhash))
        print('StateGuardian address is', guardian_address)

        if output_json:
            d = json.load(open(output_json))
            d['token'] = token_address
            d['manager'] = microraiden_address
            d['monitor'] = guardian_address

            json.dump(d, open(output_json, 'w'))

if __name__ == '__main__':
    main()
