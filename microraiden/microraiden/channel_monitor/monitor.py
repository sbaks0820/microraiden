import re
import sys

from web3 import Web3
from web3.contract import Contract

from microraiden import Session
from microraiden.utils import get_private_key, privkey_to_addr, create_signed_contract_transaction
import logging
import requests
import json
import gevent

from .state import ChannelManagerState
from .blockchain import Blockchain

from web3.middleware.pythonic import (
    pythonic_middleware,
    to_hexbytes,
)

# config = json.load(open('./monitor.json'))
# size_extraData_for_poa = 200
# pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByNumber'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)
# pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByHash'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)

# import random
# random.seed(135134523652834096528341)
# r_seed = random.getrandbits(256)

#from .make_helpers import make_channel_monitor

class ChannelMonitor(gevent.Greenlet):

    def __init__(
            self,
            web3: Web3,
            channel_manager_contract: Contract,
            token_contract: Contract,
            private_key: str,
            state_filename: str,
            n_confirmations=1
    ) -> None:
        gevent.Greenlet.__init__(self)
        self.blockchain = Blockchain(
                web3,
                channel_manager_contract,
                self,
                n_confirmations=n_confirmations
        )

        self.private_key = private_key
        self.channel_manager_contract = channel_manager_contract
        self.token_contract = token_contract
        self. n_confirmations = n_confirmations
        self.log = logging.getLogger('channel_monitor')
        network_id = int(web3.version.network)

        # self.check_contract_version()
       
        self.state = ChannelManagerState(state_filename)
        self.state.setup_db(
            network_id,
            channel_manager_contract.address,
            privkey_to_addr(self.private_key)
        )
        
        assert self.state is not None

        print('INIT:', self.state.unconfirmed_head_number)

    def __del__(self):
        self.stop()

    def _run(self):
        self.blockchain.start()

    def stop(self):
        if self.blockchain.running:
            self.blockchain.stop()
            self.blockchain.join()

    def set_head(self,
                 unconfirmed_head_number: int,
                 unconfirmed_head_hash: int,
                 confirmed_head_number,
                 confirmed_head_hash):
        self.state.update_sync_state(unconfirmed_head_number=unconfirmed_head_number,
                                     unconfirmed_head_hash=unconfirmed_head_hash,
                                     confirmed_head_number=confirmed_head_number,
                                     confirmed_hash_hash=confirmed_head_hash)

    def event_channel_opened(self, sender: str, open_block_number: int, deposit: int):
        assert is_checksum_address(sender)
        self.log.info('new channel opened (sender %s, block_number %s)', sender, open_block_number)
        print('NEW CHANNEL DETECTED')

    def wait_sync(self):
        self.blockchain.wait_sync()

# def main(
#         close_channel: bool = True,
# ):
#     w3 = Web3(HTTPProvider(config['web3path']))
#     private_key = get_private_key(config['private-key'])
#     print("Web3 Provider:", config['web3path'])
#     print('Private Key  :', config['private-key'])
#     print('Password Path:', config['password-path'])
#     print('Resource Reqd:', config['resource'])
#     print('Manager  Addr:', config['manager'])
#     print('Close Channel:', close_channel)
#     close_channels(private_key, config['password-path'], config['resource'], config['manager'], w3, close_channel)
# 
# def close_channels(
#         private_key: str,
#         password_path: str,
#         resource: str,
#         channel_manager_address: str = None,
#         web3: Web3 = None,
#         retry_interval: float = 5,
#         endpoint_url: str = 'http://0.0.0.0:5000',
#         close_channel: bool = False
# ):
# 
#     channel_manaer = make_channel_monitor
#     
#     
#     # Create the client session.
#     session = Session(
#         endpoint_url=endpoint_url,
#         private_key=private_key,
#         key_password_path=password_path,
#         channel_manager_address=channel_manager_address,
#         web3=web3,
#         retry_interval=retry_interval,
#         close_channel_on_exit=close_channel
#     )
#     print("Private Key:", private_key)
#     addr = privkey_to_addr(private_key)
#     print("Address:", addr)
# #    response = requests.get('http://0.0.0.0:5000/api/1/channels/{}'.format(addr))
#     response = session.get('{}/api/1/channels/{}'.format('http://0.0.0.0:5000', addr))
#     print(response)
# 
#     response = session.get('{}/{}'.format('http://0.0.0.0:5000',resource))
#     print(response, response.text)
# #
#     session.channel.close(balance=session.channel.balance-1)
# 
# #session.close_channel()
#     response = requests.get('http://0.0.0.0:5000/api/1/channels/{}'.format(addr))
#     print(response)
#     channels = json.loads(response.text)
#     print(channels)
# 
# 
# if __name__ == '__main__':
#     logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
#     main()
