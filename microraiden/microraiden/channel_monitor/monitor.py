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
    keccak256_hex,
    get_receipt_message,
    sign_receipt,
    sign_receipt2,
    verify_receipt,
    sign_cond_payment,
    verify_cond_payment,
    wait_for_transaction,
    bcolors,
    monitor_balance_message_from_state
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

class ConditionalPayment(object):
    def __init__(
        self,
        conditional_transfer: bool,
        payout: int,
        image: bytes,
        open_block_number: int,
        pay_sig: bytes
    ):
        self.conditional_transfer = conditional_transfer
        self.payout = payout
        self.image = image
        self.open_block_number = open_block_number
        self.pay_sig = pay_sig

class MonitorJob(object):
    def __init__(
        self,
        customer: str,
        sender: str,
        channel_manager: Contract,
        open_block_number: int,
        last_signature = None,
        last_hash = None,
        last_customer_sig = None,
        last_round_number = None
        #deposit: int,
    ):
        assert channel_manager
        self.customer = customer
        self.sender = sender
        self.channel_manager = channel_manager
        self.open_block_number = open_block_number
        self.last_signature = last_signature
        self.last_hash = last_hash
        self.last_round_number = last_round_number
        self.all_signatures = []
        self.all_customer_signatures = []
        self.all_hashes = []
        self.all_round_numbers = []

        self.interfered = False
        self.sent_receipt = False
        self.sent_pre_image = False
        self.respond = False
        self.last_image = None
        self.last_preimage = None
        self.round_number = 0
        self.conditional_payment = None

class ChannelMonitor(gevent.Greenlet):

    def __init__(
            self,
            web3: Web3,
            channel_manager_contract: Contract,
            channel_monitor_contract: Contract,
            token_contract: Contract,
            private_key: str,
            state_filename: str,
            n_confirmations=1,
            try_to_make_some_money: bool = False,
            reveal_pre_image: bool = True,
            redeem_payment: bool = True,
            cheat_with_receipt: bool = False,
    ) -> None:
        gevent.Greenlet.__init__(self)
        self.blockchain = Blockchain(
                web3,
                channel_manager_contract,
                channel_monitor_contract,
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
        self.try_to_make_some_money = try_to_make_some_money 
        self.reveal_pre_image = reveal_pre_image
        self.redeem_payment = redeem_payment
        self.cheat_with_receipt = cheat_with_receipt

        self.log.info('new receiver address %s', self.channel_manager_owner)
        
        self.state = ChannelManagerState(state_filename)
        self.state.setup_db(
            network_id,
            self.channel_manager_contract.address,
            self.channel_manager_owner
        )
        
        assert self.state is not None

        self.jobs = {}

        #self.log.info('calling setup if either deltas are zero')

        _delta_settle = int(self.channel_monitor_contract.call().delta_settle())
        _delta_withdraw = int(self.channel_monitor_contract.call().delta_withdraw())
        _guardian_deposit = int(self.channel_monitor_contract.call().guardian_deposit())
        self.delta_receipt = 20
        assert _delta_settle != 0 and _delta_withdraw != 0
   
        print(bcolors.OKBLUE,
                'Monitor Contract Params:',
                '\n\tdelta settle: %s' % _delta_settle,
                '\n\tdelta_withdraw: %s' % _delta_withdraw,
                '\n\tdeposit: %s' % _guardian_deposit, 
                bcolors.ENDC
        )

#        if _delta_settle == 0 or _delta_withdraw == 0:
#            raw_tx = create_signed_contract_transaction(
#                self.private_key,
#                self.channel_monitor_contract,
#                'setup',
#                args=[20,3],
#                value=1
#            )

#            self.log.info('sent setup transaction')
#
#            txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)
#
#            self.log.info('waiting for tx %s', encode_hex(txid))
#
#            wait_for_transaction(self.blockchain.web3, txid)
#
#            self.log.info('transaction %s mined', encode_hex(txid))

        self.log.info('channel monitor setup is finished\n')


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
        self.log.debug('new unconfirmed channel opened (sender %s, block_number %s)', sender, open_block_number)

    def unconfirmed_event_channel_topup(self, sender, open_block_number, txhash, added_deposit):
        assert is_checksum_address(sender)
        self.log.debug('unconfirmed top up of unconfirmed channel (sender %s, block_number %s, added %s)',
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

    """
    Event channel that is being monitored requested a close, time to respond
    Only respond to it once though
    """
    def event_channel_close_requested(self, sender: str, open_block_number: int, balance, timeout):
        sender = to_checksum_address(sender)

        self.log.info(
            'channel close requested (\n\tsender %s, \n\tblock number %s, \n\tbalance %s, \n\ttimeout %s)',
            sender, open_block_number, balance, timeout
        )

        job = self.jobs[self.state.receiver,sender,open_block_number]

        if not job.respond:
            self.log.info("Didn't finish fair exchange to so don't respond to close request\n")
            return

        if job.interfered:
            self.log.info('Already responded to a close (\n\tsender %s \n\topen_block_number %s, \n\tbalance %s)\n',
                sender,
                open_block_number,
                balance)
            return

        assert job.last_hash == job.all_hashes[-1]
        assert job.last_signature == job.all_signatures[-1]
        assert job.last_customer_sig == job.all_customer_signatures[-1]
        assert job.last_round_number == job.all_round_numbers[-1]

        if self.try_to_make_some_money and len(job.all_hashes) > 1:
            evidence_hash = job.all_hashes[-2]
            evidence_sig = job.all_signatures[-2]
            evidence_customer_sig = job.all_customer_signatures[-2]
            evidence_round_number = job.all_round_numbers[-2]
        else:
            evidence_hash = job.last_hash
            evidence_sig = job.last_signature
            evidence_customer_sig = job.last_customer_sig
            evidence_round_number = job.last_round_number
        
        print('\nevidence')
        debug_print([evidence_hash, decode_hex(evidence_sig), decode_hex(evidence_customer_sig), evidence_round_number])


        raw_tx = create_signed_contract_transaction(
            self.private_key,
            self.channel_manager_contract,
            'monitorEvidence',
            [
                self.state.receiver,
                open_block_number,
                evidence_hash,
                decode_hex(evidence_sig),
                decode_hex(evidence_customer_sig),
                evidence_round_number
            ]
        )

        txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)
        self.log.info('sent monitor intereference (\n\tsender %s, \n\tblock number %d, \n\ttxid %s)\n',
            sender, open_block_number, encode_hex(txid))


    """
    Ensure the customer has made a DEPOSIT to the contract
    """
    def verify_customer_deposit(self, customer: str):
        customer = to_checksum_address(customer)

        customer_deposit = int(self.channel_monitor_contract.call().balance(to_checksum_address(customer)))
        
        self.log.info('Customer %s deposit is %d',
            customer,
            customer_deposit
        )

        if customer_deposit > 0:
            return True
        else:
            return False
    
    """
    An outsource message is received from the customer. Ensure customer deposit
    """
    def on_outsource_requested(self, command: str, sender: str, customer: str, open_block_number: int):
        sender = to_checksum_address(sender)
        customer = to_checksum_address(customer)

        if (customer,sender,open_block_number) in self.jobs:
            return MONITOR_OUTSOURCE_ACCEPTED

        job = MonitorJob(customer, sender, self.channel_manager_contract, open_block_number)
        self.jobs[customer,sender,open_block_number] = job
        print(bcolors.OKGREEN + '\nfirst sig %s\n' % job.last_signature + bcolors.ENDC)

        self.log.info('new outsource requested (customer %s sender %s open_block_number %d)\n',
            customer,
            sender,
            open_block_number
        )

        return MONITOR_OUTSOURCE_ACCEPTED 

    """
    create a RECEIPT for the customer
    """
    def create_signed_receipt(self, customer: str, sender: str, open_block_number: int, balance_message_hash: bytes, signature: str, round_number: int):
    #def create_signed_receipt(self, customer: str, sender: str, open_block_number: int, balance_message_hash: bytes, round_number: int, signature: str):

        customer = to_checksum_address(customer)
        sender = to_checksum_address(sender)

        pre_image = self.rng.randint(0,2**20)
        image = keccak256((pre_image,32))

        self.log.info('Receipt (pre image %d, image %s)\n', pre_image, encode_hex(image))

        self.jobs[customer,sender,open_block_number].last_image = image
        self.jobs[customer,sender,open_block_number].last_preimage = pre_image

        #delta_settle = int(self.channel_monitor_contract.call().delta_settle())
        curr_block = self.blockchain.web3.eth.blockNumber

        t_start = curr_block
        t_expire = t_start + self.delta_receipt

        receipt_msg = sign_receipt2(
                self.private_key,
                customer,
                sender,
                open_block_number,
                image,
                t_start,
                t_expire,
                round_number,
                balance_message_hash
        )
       
        """
        Output for demonstration video
        """
        print(bcolors.OKBLUE + '\nMonitor: Sending receipt to monitor...' + bcolors.OKBLUE)
        print(bcolors.OKBLUE + 
                'Receipt:' + 
                '\n\tStart: %s' % t_start + 
                '\n\tExpire: %s' % t_expire +
                '\n\tpre image: %s' % pre_image + 
                '\n\timage: %s' % encode_hex(image) +
                bcolors.ENDC
        )

        #return [MONITOR_RECEIPT, (customer,sender,open_block_number,image,t_start,t_expire,balance_message_hash), receipt_msg] 
        return [MONITOR_RECEIPT, (customer,sender,open_block_number,image,t_start,t_expire,round_number,balance_message_hash), receipt_msg] 

    """
    received a new BLANCE SIG from customer
    """
    """old function"""
#    def on_new_balance_sig(self, command: str, customer: str,sender: str, open_block_number: int, balance_message_hash: bytes, signature: str):
    """accepted customer sig"""
    #def on_new_balance_sig(self, command: str, customer: str,sender: str, open_block_number: int, balance_message_hash: bytes, signature: str, customer_sig: str):
    """with round number"""
    def on_new_balance_sig(self, command: str, customer: str,sender: str, open_block_number: int, balance_message_hash: bytes, round_number: int, signature: str, customer_sig: str):
        #print('\nbalance sig')
        #debug_print([customer, sender, open_block_number, balance_message_hash, signature])
        customer = to_checksum_address(customer)
        sender = to_checksum_address(sender)

        if not self.verify_customer_deposit(customer):
            return NO_CONTRACT_DEPOSIT 

        current_round_number = self.jobs[customer,sender,open_block_number].round_number
        try:
            assert self.jobs[customer,sender,open_block_number].round_number == round_number-1
        except AssertionError:
            print(bcolors.OKBLUE + 'gave incorrent monitor number (current %d, expected %d, received %d)' % (current_round_number, current_round_number + 1, round_number) + bcolors.ENDC)
            return
        try:
            assert self.jobs[customer,sender,open_block_number].last_signature != signature
            self.log.info('Signature is different')
            self.log.info('Current round number %d, state hash round number %d', self.jobs[customer,sender,open_block_number].round_number, round_number)
            final_hash = monitor_balance_message_from_state(balance_message_hash, round_number)
            
            #self.log.info('Monitors hash: (balanace message hash %s)', encode_hex(balance_message_hash))
            self.log.info('Monitors hash: (round number %s, balanace message hash %s, final hash %s)', round_number, encode_hex(balance_message_hash), encode_hex(final_hash))

            """regular"""
            #sig_addr = addr_from_sig(decode_hex(signature), balance_message_hash)
            """with receipt"""
            sig_addr = addr_from_sig(decode_hex(signature), final_hash)
            if not is_same_address(
                    sig_addr,
                    sender
            ):
                self.log.info('balance message not signed by correct sender')
                return BALANCE_SIG_NOT_ACCEPTED 

            """regular"""
            #customer_sig_addr = addr_from_sig(decode_hex(customer_sig), balance_message_hash)
            """with receipt"""
            customer_sig_addr = addr_from_sig(decode_hex(customer_sig), final_hash)
            if not is_same_address(
                    customer_sig_addr,
                    customer
            ):
                self.log.info('customers balance sig is wrong. address: %s', customer_sig_addr)
                return BALANCE_SIG_NOT_ACCEPTED

            self.jobs[customer,sender,open_block_number].round_number += 1

            receipt = self.create_signed_receipt(customer, sender, open_block_number, balance_message_hash, signature, round_number)
            #receipt = self.create_signed_receipt(customer, sender, open_block_number, balance_message_hash, round_number, signature)
            #self.customer_fair_exchange(customer, sender, open_block_number, balance_message_hash, signature)
            self.jobs[customer,sender,open_block_number].last_signature = signature
            self.jobs[customer,sender,open_block_number].last_hash = balance_message_hash
            self.jobs[customer,sender,open_block_number].last_customer_sig = customer_sig
            self.jobs[customer,sender,open_block_number].last_round_number = round_number

            self.jobs[customer,sender,open_block_number].all_signatures.append(signature)
            self.jobs[customer,sender,open_block_number].all_customer_signatures.append(customer_sig)
            self.jobs[customer,sender,open_block_number].all_hashes.append(balance_message_hash)
            self.jobs[customer,sender,open_block_number].all_round_numbers.append(round_number)


            self.log.info('Accepting new balance signature (customer %s, sender %s, open block number %d)', customer, sender, open_block_number)

#            self.log.info('Accepting new balance signature (customer %s, sender %s, open block number %d, \n\told hash %s, \n\tnew hash %s)',
#                customer,
#                sender,
#                open_block_number,
#                encode_hex(self.jobs[customer,sender,open_block_number].last_hash),
#                encode_hex(balance_message_hash)
#            )

      
            self.jobs[customer,sender,open_block_number].sent_receipt = True

            self.log.debug('accepted balance signature (customer %s, sender %s, open_block_number %s\n',
                customer, sender, open_block_number)
#            self.log.info('TYPE OF RECEIPT %s', str(type(receipt)))
#            self.log.info('RECEIPT %s', str(receipt))
            return receipt
        except KeyError:
            self.log.info('balance sig for job not being watched\n')
            return IGNORE_BALANCE_SIG
        except AssertionError:
            self.log.info('customer sent the same signature as before: %s\n', encode_hex(signature))
            return IGNORE_SAME_BALANCE_SIG

    """
    COND PAYMENT signed by customer
    """
    def on_conditional_payment(self, command: str, customer: str, sender: str, open_block_number: int,
            payout: int, conditional_transfer: bool, image: bytes, pay_sig: bytes):
        #print('\nconditional paymen\n')
        #debug_print([customer, sender, open_block_number, payout, conditional_transfer, 'image', image, 'pay_sig', pay_sig])
        customer = to_checksum_address(customer)
        sender = to_checksum_address(sender)

        try:
            job = self.jobs[customer,sender,open_block_number]
        except KeyError:
            self.log.info('No such channel being outsourced (customer %s sender %s open_block_number %s',
                customer,
                sender,
                open_block_number
            )
            return
        
        assert job.sent_receipt
        assert payout > 0

        self.log.info('Conditional Payment (customer %s)\n', customer)
        self.log.debug('Cond Payment (\n\tjob image %s, \n\tcustomer image %s)', encode_hex(job.last_image), encode_hex(image))
        
        assert image == job.last_image 
        
        signer = verify_cond_payment(
            sender,
            open_block_number,
            payout,
            conditional_transfer,
            image,
            pay_sig
        )

        self.log.debug('conditional payment signed by %s, customer %s', signer, customer)
        assert is_same_address(
                signer,
                customer
        )

        assert keccak256((job.last_preimage,32)) == image
        p = ConditionalPayment(conditional_transfer, payout, image, open_block_number, pay_sig)
        self.jobs[customer,sender,open_block_number].conditional_payment = p

        if self.reveal_pre_image:
            print(bcolors.OKBLUE + '\nMonitor: Sending pre-image to customer %d...\n' % job.last_preimage + bcolors.ENDC)
            job.respond = True
            return job.last_preimage 
        else:
            print(bcolors.OKBLUE + 
                    '\nMonitor: Not revealing pre-image to customer' +
                    bcolors.ENDC
            )
#            job.respond = False
            return

    """
    Customer raised DISPUTED
    """
    def event_customer_dispute(self, customer: str, sender: str, open_block_number: int):
        customer = to_checksum_address(customer)
        sender = to_checksum_address(sender)
        
        try:
            job = self.jobs[customer,sender,open_block_number]
        except KeyError:
            self.log.info('Customer disputed a channel not being watched.')
            return


        if self.redeem_payment:
            p = job.conditional_payment

            #debug_print([p.payout, job.last_image, job.last_preimage, p.pay_sig, customer])
            
            print(bcolors.OKBLUE + '\nMonitor: Redeeming payment on-chain instead...\n' + bcolors.ENDC)

            self.log.info('Redeeming payment on-chain (customer %s)', customer)
#            self.log.info('calling setstate to redeem payment \n\tpayout %d, \n\tlast_image %s, \n\tlast preimage %d, \n\tpay_sig %s, \n\tcustomer %s)',
#                p.payout,
#                encode_hex(job.last_image),
#                job.last_preimage,
#                encode_hex(p.pay_sig),
#                customer
#            )
            raw_tx = create_signed_contract_transaction(
                self.private_key,
                self.channel_monitor_contract,
                'setstate',
                [
                    sender,
                    open_block_number,
                    p.payout,
                    p.conditional_transfer,
                    job.last_image,
                    job.last_preimage,
                    p.pay_sig,
                    customer
                ]
            )

            txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)

            self.log.debug('setstate transaction (txid %s)\n', encode_hex(txid))
   
        print(bcolors.OKBLUE +
                "\nMonitor: Customer raised dispute in contract. No action required." +
                bcolors.ENDC
        ) 

        if self.redeem_payment and self.cheat_with_receipt:
            job.respond = False
        elif self.redeem_payment:
            job.respond = True
        else:
            job.respond = False



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
       
        self.log.info('Starting listener...')

        self.listener = Listener((address,port))
        self.conn = None

        print(bcolors.BOLD + 'Starting Monitor...' + bcolors.ENDC)


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
                    self.log.debug('RESPONSE BEING SENT type %s, response %s',
                        str(type(response)), str(response))
                    self.conn.send(response)
                gevent.sleep(0.5)
            except EOFError:
                self.log.info('connection to customer terminated')
                break

    def run(self):
        print(bcolors.BOLD + 'MONITOR: running run' + bcolors.ENDC)
        assert (not self.conn)
        print(bcolors.BOLD + "monitor: opening listner (address %s, port %d)" % (self.address, self.port) + bcolors.ENDC)
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

    def stop(self):
        if self.channel_monitor.running:
            self.channel_monitor.stop()
            self.channel_monitor.join()

    def wait_sync(self):
        self.channel_monitor.wait_sync()
 
