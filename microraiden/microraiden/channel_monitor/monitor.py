import re
import sys

from web3 import Web3
from web3.contract import Contract
from eth_utils import (
    decode_hex,
    encode_hex,
    is_same_address,
    is_checksum_address,
    to_checksum_address,
)

from microraiden import Session
#from microraiden.constants import (
#    OUTSOURCE_MESSAGE,
#    MONITOR_SIGNATURE_ACCEPTED,
#    MONITOR_SIGNATURE_REJECTED,
#    MONITOR_CHANNEL_NOT_FOUND,
#    MONITOR_OUTSOURCE_ACCEPTED,
#    NEW_BALANCE_SIG,
#    IGNORE_BALANCE_SIG,
#    IGNORE_SAME_BALANCE_SIG,
#    BALANCE_SIG_ACCEPTED,
#    BALANCE_SIG_NOT_ACCEPTED
#)

from microraiden.constants import *

from microraiden.utils import (
    get_private_key, 
    privkey_to_addr, 
    create_signed_contract_transaction, 
    addr_from_sig,
    debug_print,
    keccak256,
    get_receipt_message,
    sign_receipt,
    verify_receipt,
    sign_cond_payment,
    verify_cond_payment,
    wait_for_transaction
)

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

from random import SystemRandom
import sys

from multiprocessing.connection import Listener

# config = json.load(open('./monitor.json'))
# size_extraData_for_poa = 200
# pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByNumber'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)
# pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByHash'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)

# import random
# random.seed(135134523652834096528341)
# r_seed = random.getrandbits(256)

#from .make_helpers import make_channel_monitor

class MonitorJob(object):
    def __init__(
        self,
        customer: str,
        sender: str,
        channel_manager: Contract,
        open_block_number: int,
        last_signature = None,
        last_hash = None
        #deposit: int,
    ):
        assert channel_manager
        self.customer = customer
        self.sender = sender
        self.channel_manager = channel_manager
        self.open_block_number = open_block_number
        self.last_signature = last_signature
        self.last_hash = last_hash

        self.interfered = False
        self.sent_receipt = False
        self.sent_pre_image = False

class ChannelMonitor(gevent.Greenlet):

    def __init__(
            self,
            web3: Web3,
            channel_manager_contract: Contract,
            channel_monitor_contract: Contract,
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

        self.log = logging.getLogger('channel_monitor')
        self.private_key = private_key
        self.channel_manager_contract = channel_manager_contract
        self.channel_monitor_contract = channel_monitor_contract
        self.channel_manager_owner = self.channel_manager_contract.call().owner_address()
        self.token_contract = token_contract
        self. n_confirmations = n_confirmations
        network_id = int(web3.version.network)
        self.rng = SystemRandom()

        
        self.log.info('new receiver address %s', self.channel_manager_owner)


        # self.check_contract_version()
       
        self.state = ChannelManagerState(state_filename)
        self.state.setup_db(
            network_id,
            self.channel_manager_contract.address,
            self.channel_manager_owner
        )
        
        assert self.state is not None

        self.jobs = {}

        self.log.info('calling setup if either deltas are zero')

        _delta_settle = int(self.channel_monitor_contract.call().delta_settle())
        _delta_withdraw = int(self.channel_monitor_contract.call().delta_withdraw())

        if _delta_settle == 0 or _delta_withdraw == 0:
            raw_tx = create_signed_contract_transaction(
                self.private_key,
                self.channel_monitor_contract,
                'setup',
                args=[3,3],
                value=1
            )

            self.log.info('sent setup transaction')

            txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)

            self.log.info('waiting for tx %s', txid)

            wait_for_transaction(self.blockchain.web3, txid)

            self.log.info('transaction %s mined', txid)

        self.log.info('channel monitor setup is finished')


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

    def event_channel_close_requested(self, sender: str, open_block_number: int, balance, timeout):
        self.log.info(
            'channel close requested (sender %s, block number %s, balance %s, timeout %s)',
            sender, open_block_number, balance, timeout
        )

        job = self.jobs[self.state.receiver,sender,open_block_number]

        if job.interfered:
            self.log.info('already responded to a close (sender %s open_block_number %s, balance %s)',
                sender,
                open_block_number,
                balance)
            return

        print('\nevidence')
        debug_print([job.last_hash, decode_hex(job.last_signature), job.last_signature])

        raw_tx = create_signed_contract_transaction(
            self.private_key,
            self.channel_manager_contract,
            'monitorEvidence',
            [
                self.state.receiver,
                open_block_number,
                job.last_hash,
                decode_hex(job.last_signature)
            ]
        )

        txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)
        self.log.info('sent monitor intereference (sender %s, block number %d, txid %s)',
            sender, open_block_number, txid)

    def verify_customer_deposit(self, customer: str):
        customer_deposit = int(self.channel_monitor_contract.call().balance(to_checksum_address(customer)))
        
        self.log.info('Customer %s deposit is %d',
            customer,
            customer_deposit
        )

        if customer_deposit > 0:
            return True
        else:
            return False
    
    def on_outsource_requested(self, command: str, sender: str, customer: str, open_block_number: int):
        if (customer,sender,open_block_number) in self.jobs:
            return MONITOR_OUTSOURCE_ACCEPTED

        if not self.verify_customer_deposit(customer):
            return NO_CONTRACT_DEPOSIT 

        job = MonitorJob(customer, sender, self.channel_manager_contract, open_block_number)
        self.jobs[customer,sender,open_block_number] = job

        self.log.info('new outsource requested (customer %s sender %s open_block_number %d)',
            customer,
            sender,
            open_block_number
        )

        return MONITOR_OUTSOURCE_ACCEPTED 

    def create_signed_receipt(self, customer: str, sender: str, open_block_number: int, balance_message_hash: bytes, signature: str):

        pre_image = self.rng.randint(0,2**20)
        image = keccak256(pre_image) 

        delta_settle = int(self.channel_monitor_contract.call().delta_settle())
        curr_block = self.blockchain.web3.eth.blockNumber

        t_start = curr_block
        t_expire = t_start + delta_settle

        receipt_msg = sign_receipt(
                self.private_key,
                customer,
                sender,
                open_block_number,
                image,
                t_start,
                t_expire
        )

        return [MONITOR_RECEIPT, (customer,sender,open_block_number,image,t_start,t_expire), receipt_msg] 

    def on_conditional_payment(self, command: str, customer: str, sender: str, open_block_number: int,
            payout: int, conditional_transfer: bool, image: bytes, pay_sig: bytes):
        print('\nconditional paymen\n')
        debug_print([customer, sender, open_block_number, payout, conditional_transfer, image, pay_sig])
        try:
            job = self.jobs[customer,sender,open_block_number]
        except KeyError:
            self.log.info('no such channel being outsourced (customer %s sender %s open_block_number %s',
                customer,
                sender,
                open_block_number
            )
            return None
        
        assert job.sent_receipt
        assert payout > 0
        
        signer = verify_cond_payment(
            payout,
            conditional_transfer,
            image,
            pay_sig
        )

        self.log.info('conditional payment signed by %s, customer %s', signer, customer)
        assert is_same_address(
                signer,
                customer
               )

    def on_new_balance_sig(self, command: str, customer: str,sender: str, open_block_number: int, balance_message_hash: bytes, signature: str):
        print('\nbalance sig')
        debug_print([customer, sender, open_block_number, balance_message_hash, signature])
        try:
            assert self.jobs[customer,sender,open_block_number].last_signature != signature
            sig_addr = addr_from_sig(decode_hex(signature), balance_message_hash)
            if not is_same_address(
                    sig_addr,
                    sender
            ):
                self.log.info('balance message not signed by correct sender')
                return BALANCE_SIG_NOT_ACCEPTED 

            receipt = self.create_signed_receipt(customer, sender, open_block_number, balance_message_hash, signature)
            #self.customer_fair_exchange(customer, sender, open_block_number, balance_message_hash, signature)
            self.jobs[customer,sender,open_block_number].sent_receipt = True

            self.jobs[customer,sender,open_block_number].last_signature = signature
            self.jobs[customer,sender,open_block_number].last_hash = balance_message_hash
            self.log.info('accepted balance signature (customer %s, sender %s, open_block_number %s',
                customer, sender, open_block_number)
            return receipt
        except KeyError:
            self.log.info('balance sig for job not being watched')
            return IGNORE_BALANCE_SIG
        except AssertionError:
            self.log.info('customer sent the same signature as before')
            return IGNORE_SAME_BALANCE_SIG

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
            try:
                if self.conn.poll():
                    recv = self.conn.recv()
                    if recv[0] == OUTSOURCE_MESSAGE:
                        response = self.channel_monitor.on_outsource_requested(*recv)
                    elif recv[0] == NEW_BALANCE_SIG:
                        response = self.channel_monitor.on_new_balance_sig(*recv)
                    elif recv[0] == CONDITIONAL_PAYMENT:
                        response = self.channel_monitor.on_conditional_payment(*recv)
                    self.conn.send(response)
                gevent.sleep(0.5)
            except EOFError:
                self.log.info('connection to customer terminated')
                break

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
        self.conn.send(self.channel_monitor.channel_monitor_contract.address)
        self.log.info('channel monitor contract address %s',
            self.channel_monitor.channel_monitor_contract.address)
        self.listen_forever()
 
