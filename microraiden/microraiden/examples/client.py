import re
import sys

from web3 import Web3, HTTPProvider

from microraiden import Session
from microraiden import constants
from microraiden.utils import get_private_key, privkey_to_addr, create_signed_contract_transaction
import logging
import requests
import json
import gevent
import time

from web3.middleware.pythonic import (
    pythonic_middleware,
    to_hexbytes,
)

from multiprocessing.connection import Client

config = json.load(open('./main.json'))
size_extraData_for_poa = 200
pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByNumber'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)
pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByHash'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)

import random
random.seed(135134523652834096528341)
r_seed = random.getrandbits(256)

log = logging.getLogger(__name__)

monitor_address = ('localhost', 6001)

def main(
        close_channel: bool = True,
):
    w3 = Web3(HTTPProvider(config['web3path']))
    private_key = get_private_key('./dragonstone-rinkeby-02-03')
    print("Web3 Provider:", config['web3path'])
    #print('Private Key  :', config['private-key'])
    print('Password Path:', config['password-path'])
    print('Resource Reqd:', config['resource'])
    print('Manager  Addr:', config['manager'])
    print('Close Channel:', close_channel)
    close_channels(private_key, config['password-path'], config['resource'], config['manager'], w3, close_channel)

def get_tokens():
    web3 = Web3(HTTPProvider(config['web3path']))
    private_key = get_private_key('./dragonstone-rinkeby-02-03')
    log.info('This private key %s', private_key)
    log.info('This address %s', privkey_to_addr(private_key))
    token_address = config['token']
    token_abi = constants.CONTRACT_METADATA[constants.TOKEN_ABI_NAME]['abi']
    token_contract = web3.eth.contract(abi=token_abi, address=token_address)

    raw_tx = create_signed_contract_transaction(
        private_key,
        token_contract,
        'mint',
        [],
        100000000000000000
    )
    
    web3.eth.sendRawTransaction(raw_tx)
    log.info('bought tokens from custom token')
    

def close_channels(
        private_key: str,
        password_path: str,
        resource: str,
        channel_manager_address: str = None,
        web3: Web3 = None,
        retry_interval: float = 5,
        endpoint_url: str = 'http://0.0.0.0:5010',
        close_channel: bool = False
):
    
    # Create the client session.
    session = Session(
        endpoint_url=endpoint_url,
        private_key=private_key,
        key_password_path=password_path,
        channel_manager_address=channel_manager_address,
        web3=web3,
        retry_interval=retry_interval,
        close_channel_on_exit=close_channel
    )

    #conn = Client(monitor_address)
    #conn.send('share')
    ##print(conn.recv())
    #conn.close()

    print("Private Key:", private_key)
    addr = privkey_to_addr(private_key)
    print("Address:", addr)
#    response = requests.get('http://0.0.0.0:5000/api/1/channels/{}'.format(addr))
    #response = session.get('{}/api/1/channels/{}'.format('http://0.0.0.0:5000', addr))
    #print(response)

    response = session.get('{}/{}'.format('http://0.0.0.0:5010',resource))
    print(response)
    print(response.text)
    print(response.content)
    print(response.headers)
#
    time.sleep(4)

    response = session.get('{}/{}'.format('http://0.0.0.0:5010',resource))
    print(response)
    print(response.text)
    print(response.content)
    print(response.headers)

    time.sleep(4)


    response = session.get('{}/{}'.format('http://0.0.0.0:5010',resource))
    print(response)
    print(response.text)
    print(response.content)
    print(response.headers)

#    time.sleep(10)

    session.channel.close(balance=session.channel.balance-1)
#session.close_channel()
#    response = requests.get('http://0.0.0.0:5000/api/1/channels/{}'.format(addr))
#    print(response)
#    channels = json.loads(response.text)
#    print(channels)


if __name__ == '__main__':
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    main()
    #get_tokens()
    
