import re
import sys

from web3 import Web3
from web3.contract import Contract
from eth_utils import (
    decode_hex,
    is_same_address,
    is_checksum_address
)
from microraiden import Session
from microraiden.constants import (
    OUTSOURCE_MESSAGE,
    MONITOR_SIGNATURE_ACCEPTED,
    MONITOR_SIGNATURE_REJECTED,
    MONITOR_CHANNEL_NOT_FOUND
)

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

from multiprocessing.connection import Listener

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
            state_guardian_contract: Contract,
            token_contract: Contract,
            private_key: str,
            state_filename: str,
            n_confirmations=1
    ) -> None:
        gevent.Greenlet.__init__(self)
        self.blockchain = Blockchain(
                web3,
                channel_manager_contract,
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
                                     confirmed_head_hash=confirmed_head_hash)

    def event_channel_opened(self, sender: str, open_block_number: int, deposit: int):
        assert is_checksum_address(sender)
        self.log.info('new channel opened (sender %s, block_number %s)', sender, open_block_number)

   
    def unconfirmed_event_channel_opened(self, sender: str, open_block_number: int, deposit: int):
        assert is_checksum_address(sender)
        self.log.info('new unconfirmed channel opened (sender %s, block_number %s)', sender, open_block_number)

    def unconfirmed_event_channel_topup(self, sender, open_block_number, txhash, added_deposit):
        assert is_checksum_address(sender)
        self.log.info('unconfirmed top up of unconfirmed channel (sender %s, block_number %s, added %s)',
                    sender, open_block_number, added_deposit)

    def event_channel_topup(self, sender, open_block_number, txhash, added_deposit):
        assert is_checksum-address(sender)
        self.log.info(
            'Deposit top up (sender %s, block number %s, added deposit %s)',
            sender, open_block_number, added_deposit
        )

    def event_channel_settled(self, sender, open_block_number):
        self.log.info(
            'channel settled (sender %s, block number %s)',
            sender, open_block_number
        )

    def event_channel_close_requested(self, sender, open_block_number, balance, timeout):
        self.log.info(
            'channel close requested (sender %s, block number %s, balance %s, timeout %s)',
            sender, open_block_number, balance, timeout
        )


    def wait_sync(self):
        self.blockchain.wait_sync()

    @property
    def channels(self):
        return self.state.channels

    @property
    def unconfirmed_channels(self):
        return self.state.unconfirmed_channels

    @property
    def pending_channels(self):
        return self.state.pending_channels

    def get_token_address(self):
        return self.token_contract.address


class MonitorListener(gevent.Greenlet):
    def __init__(
        self,
        address: str,
        port: int,
        channel_monitor: ChannelMonitor
    ):
        gevent.Greenlet.__init__(self)
        self.channel_monitor = channel_monitor
        self.address = address
        self.port = port
        self.channel_monitor.start()
        self.channel_monitor.wait_sync()
        self.log = logging.getLogger('channel_monitor')
        
        self.listener = Listener((address,port))
        self.conn = None


    def listen_forever(self):
        while True:
            if self.conn.poll():
                recv = self.conn.recv()
                self.log.info(recv)
                assert len(recv) == 3
                sender,open_block_number,signature = recv
                self.conn.send('signature accepted')

            gevent.sleep(1)

    def run(self):
        assert (not self.conn)
        conn = self.listener.accept()
        recv = conn.recv()
        try:
            assert recv == 'Start Connection'
            self.log.info('Started connection with cursomer.')
        except AssertionError:
            self.log.info('Error: connection to customer received message: %s', recv)
            return

        self.conn = conn
        self.listen_forever()
 
