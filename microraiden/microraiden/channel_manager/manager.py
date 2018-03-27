"""Channel manager handles channel state changes on a low (blockchain) level."""
import time

import gevent
import gevent.event
import filelock
import logging
import os
from eth_utils import (
    decode_hex,
    encode_hex,
    is_same_address,
    is_checksum_address,
    to_checksum_address
)
from ethereum.exceptions import InsufficientBalance
from web3 import Web3
from web3.contract import Contract

from microraiden.utils import (
    verify_balance_proof,
    privkey_to_addr,
    sign_close,
    create_signed_contract_transaction,
    create_local_contract_transaction,
    get_balance_message,
    debug_print,
    wait_for_transaction,
    get_receipt_message,
    sign_receipt,
    verify_receipt,
    sign_cond_payment,
    verify_cond_payment,
    get_balance_message,
    keccak256,
    keccak256_hex
)

#from microraiden.exceptions import (
#    NetworkIdMismatch,
#    StateReceiverAddrMismatch,
#    StateContractAddrMismatch,
#    StateFileLocked,
#    NoOpenChannel,
#    InsufficientConfirmations,
#    InvalidBalanceProof,
#    InvalidBalanceAmount,
#    InvalidContractVersion,
#    NoBalanceProofReceived,
#)
from microraiden.exceptions import *

#from microraiden.constants import (
#    CHANNEL_MANAGER_CONTRACT_VERSION,
#    OUTSOURCE_MESSAGE,
#    MONITOR_SIGNATURE_ACCEPTED,
#    MONITOR_SIGNATURE_REJECTED,
#    MONITOR_CHANNEL_NOT_FOUND,
#    MONITOR_OUTSOURCE_ACCEPTED,
#    NEW_BALANCE_SIG,
#    IGNORE_BALANCE_SIG,
#    IGNORE_SAME_BALANCE_SIG,
#    BALANCE_SIG_ACCEPTED,
#    BALANCE_SIG_NOT_ACCEPTED,
#    CONTRACT_METADATA,
#    CHANNEL_MONITOR_ABI_NAME
#)

from microraiden.constants import *

from web3.contract import Contract
from microraiden import constants
from collections import defaultdict

from multiprocessing.connection import Client
from .state import ChannelManagerState
from .blockchain import Blockchain
from .channel import Channel, ChannelState

log = logging.getLogger(__name__)


class MonitorReceipt:
    def __init__(
        self,
        _t_start: int,
        _t_expire: int,
        _image: bytes,
        _signature: bytes
    ):
        self.t_start = _t_start
        self.t_expire = _t_expire
        self.image = _image
        self.signature = _signature

class MonitorChannel:
    def __init__(
        self,
        _sender: str,
        _receiver: str,
        _deposit: int
    ):
        self.sender = _sender
        self.receiver = _receiver
        self.deposit = _deposit
        self.payout = 0

        self.last_cond_payment = None
        self.last_receipt = None
        self.fair_exchange = True

        self.monitor_channels = {}
        self.signed_receipts = {}

    def add_payout(
        self,
        _payout: int
    ):
        self.payout += _payout

    @property
    def profit(self):
        return self.deposit - self.payout

class ChannelManager(gevent.Greenlet):
    """Manages channels from the receiver's point of view."""

    def __init__(
            self,
            web3: Web3,
            channel_manager_contract: Contract,
            token_contract: Contract,
            private_key: str,
            state_filename: str = None,
            monitor_address: str = None,
            monitor_port: int = None,
            n_confirmations=1,
    ) -> None:
        gevent.Greenlet.__init__(self)
        self.blockchain = Blockchain(
            web3,
            channel_manager_contract,
            self,
            n_confirmations=n_confirmations
        )
        self.receiver = privkey_to_addr(private_key)
        assert is_checksum_address(self.receiver)

        self.private_key = private_key
        self.channel_manager_contract = channel_manager_contract
        self.token_contract = token_contract
        self.n_confirmations = n_confirmations
        self.log = logging.getLogger('channel_manager')
        network_id = int(web3.version.network)
        assert is_same_address(privkey_to_addr(self.private_key), self.receiver)

        self.wait_to_dispute = {}

        # check contract version
        self.check_contract_version()

        if state_filename not in (None, ':memory:') and os.path.isfile(state_filename):
            self.state = ChannelManagerState.load(state_filename)
        else:
            self.state = ChannelManagerState(state_filename)
            self.state.setup_db(
                network_id,
                channel_manager_contract.address,
                self.receiver
            )

        assert self.state is not None
        if state_filename not in (None, ':memory:'):
            self.lock_state = filelock.FileLock(state_filename + '.lock')
            try:
                self.lock_state.acquire(timeout=0)
            except filelock.Timeout:
                raise StateFileLocked("state file %s is locked by another process" %
                                      state_filename)

        if network_id != self.state.network_id:
            raise NetworkIdMismatch("Network id mismatch: state=%d, backend=%d" % (
                                    self.state.network_id, network_id))

        if not is_same_address(self.receiver, self.state.receiver):
            raise StateReceiverAddrMismatch('%s != %s' %
                                            (self.receiver, self.state.receiver))
        if not is_same_address(self.state.contract_address, channel_manager_contract.address):
            raise StateContractAddrMismatch('%s != %s' % (
                channel_manager_contract.address, self.state.contract_address))
    

        if monitor_address:
            self.monitor = Client((monitor_address, monitor_port))
            self.monitor.send('Start Connection')
            self.monitor_address = monitor_address
            self.monitor_port = monitor_port

        monitor_contract_address = self.monitor.recv() 
        
        if type(monitor_contract_address) == list:
            monitor_contract_address = monitor_contract_address[0]
        monitor_contract_address = to_checksum_address(monitor_contract_address)

        self.channel_monitor_contract = web3.eth.contract(
            abi=CONTRACT_METADATA[CHANNEL_MONITOR_ABI_NAME]['abi'],
            address=monitor_contract_address
        )

        self.blockchain.channel_monitor_contract = self.channel_monitor_contract

        self.log.info('setting up channel manager, receiver=%s channel_contract=%s' %
                       (self.receiver, channel_manager_contract.address))

    
        balance = self.token_contract.call().balanceOf(self.receiver)

        self.monitor_eth_address = self.channel_monitor_contract.call().monitor().lower()
        self.log.info('monitor address in contract is %s', self.monitor_eth_address)

        customer_deposit = int(self.channel_monitor_contract.call().balance(self.receiver))
        self.log.info('customer deposit is %d', customer_deposit)

        if customer_deposit == 0:
            raw_tx = create_signed_contract_transaction(
                self.private_key,
                self.channel_monitor_contract,
                'deposit',
                args=[self.channel_manager_contract.address],
                value=10,
            )
    
            self.log.info('sent deposit transaction to address %s',
                self.channel_monitor_contract.address
            )
    
            txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)
            self.log.info('waiting for tx %s',
                encode_hex(txid)
            )
    
            wait_for_transaction(self.blockchain.web3, txid)
            self.log.info('transaction %s has been waited for',
                encode_hex(txid)
            )
       
        customer_deposit = int(self.channel_monitor_contract.call().balance(self.receiver))
        assert customer_deposit != 0
        
        self.log.info('channel manager setup is finished')

        self.previous_channel_sigs = {}
#        self.signed_receipts = {}
#        self.monitor_channels = defaultdict(bool)

        self.payment_channel = MonitorChannel(self.receiver, self.monitor_eth_address,customer_deposit)

    @property
    def monitor_channels(self):
        return self.payment_channel.monitor_channels

    @property
    def signed_receipts(self):
        return self.payment_channel.signed_receipts

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
        """Set the block number up to which all events have been registered."""
        assert unconfirmed_head_number > 0
        assert confirmed_head_number > 0
        assert confirmed_head_number < unconfirmed_head_number
        self.state.update_sync_state(unconfirmed_head_number=unconfirmed_head_number,
                                     unconfirmed_head_hash=unconfirmed_head_hash,
                                     confirmed_head_number=confirmed_head_number,
                                     confirmed_head_hash=confirmed_head_hash)


    
    def outsource_channel(self, channel: Channel):
        if self.monitor:
            self.monitor.send([OUTSOURCE_MESSAGE, channel.sender, channel.receiver, channel.open_block_number])

            if self.monitor.poll(10):
                recv = self.monitor.recv()

                if recv == NO_CONTRACT_DEPOSIT:
                    raise NoDepositDetected("monitor didn't detect a deposit") 

#                if recv != MONITOR_OUTSOURCE_ACCEPTED:
#                    raise MonitorRefusedOutsoure("monitor didn't respond with acknowledgement")

                self.log.info('outsourced channel to monitor (sender %s receiver %s open_block_number %d)',
                    channel.sender,
                    channel.receiver,
                    channel.open_block_number
                )
            else:
                self.log.info('monitor not responding to outsource request, likely failed')
                self.monitor = None
        else:
            self.log.info('monitor connection failed previously, monitor is None object now')

    # relevant events from the blockchain for receiver from contract

    def event_channel_opened(self, sender: str, open_block_number: int, deposit: int):
        """Notify the channel manager of a new confirmed channel opening."""
        assert is_checksum_address(sender)
        if (sender, open_block_number) in self.channels:
            return  # ignore event if already provessed
        c = Channel(self.state.receiver, sender, deposit, open_block_number)
        c.confirmed = True
        c.state = ChannelState.OPEN
        self.log.info('new channel opened (sender %s, block number %s)', sender, open_block_number)
        self.state.set_channel(c)

        self.outsource_channel(c)

    def unconfirmed_event_channel_opened(self, sender: str, open_block_number: int, deposit: int):
        """Notify the channel manager of a new channel opening that has not been confirmed yet."""
        assert is_checksum_address(sender)
        assert deposit >= 0
        assert open_block_number > 0
        event_already_processed = (sender, open_block_number) in self.unconfirmed_channels
        channel_already_confirmed = (sender, open_block_number) in self.channels
        if event_already_processed or channel_already_confirmed:
            return
        c = Channel(self.state.receiver, sender, deposit, open_block_number)
        c.confirmed = False
        c.state = ChannelState.OPEN
        self.state.set_channel(c)
        self.log.info('unconfirmed channel event received (sender %s, block_number %s)',
                      sender, open_block_number)

    def unconfirmed_event_monitor_intereference(self, sender: str, receiver: str, open_block_number: int):
        assert is_checksum_address(sender)
        assert is_checksum_address(receiver)
        assert open_block_number > 0
       

    def event_channel_close_requested(
        self,
        sender: str,
        open_block_number: int,
        balance: int,
        monitor_timeout: int,
        settle_timeout: int
    ):
        """Notify the channel manager that a the closing of a channel has been requested.
        Params:
            settle_timeout (int):   settle timeout in blocks"""
        assert is_checksum_address(sender)
        assert settle_timeout >= 0
        if (sender, open_block_number) not in self.channels:
            self.log.warning(
                'attempt to close a non existing channel (sender %ss, block_number %ss)',
                sender,
                open_block_number
            )
            return
        c = self.channels[sender, open_block_number]
        if c.balance > balance:
            self.log.warning('sender tried to cheat, sending challenge '
                             '(sender %s, block number %s)',
                             sender, open_block_number)
            #self.wait_to_dispute[monitor_timeout] = (sender, open_block_number, balance, monitor_timeout, settle_timeout)
            self.close_channel(sender, open_block_number)  # dispute by closing the channel
        else:
            self.log.info('valid channel close request received '
                          '(sender %s, block number %s, timeout %d)',
                          sender, open_block_number, settle_timeout)
            c.settle_timeout = settle_timeout
            c.monitor_timeout = monitor_timeout
            c.is_closed = True
            c.confirmed = True
            c.mtime = time.time()
        self.state.set_channel(c)

    def event_channel_settled(self, sender, open_block_number):
        """Notify the channel manager that a channel has been settled."""
        assert is_checksum_address(sender)
    
        c = self.channels[sender,open_block_number]

        closing_balance,monitor_balance,settle_block_number = self.channel_manager_contract.call().getClosingInfo(sender, self.state.receiver, open_block_number)

        self.log.info('closing infor for channel (sender %s, open_block_number %d) is (closing_balance %d, monitor_balance %d, setlle_block_number %d)',
            sender,
            open_block_number,
            closing_balance,
            monitor_balance,
            settle_block_number
        )

        if closing_balance > monitor_balance:
            try:
                m = self.signed_receipts[sender, open_block_number]
                
                self.log.info('sending recourse (sender %s, open_block_number %d, image %s, signature %s)',
                    sender,
                    open_block_number,
                    m.image,
                    m.signature
                )

                sig_addr = verify_receipt(
                    self.state.receiver,
                    to_checksum_address(sender),
                    open_block_number,
                    m.image,
                    m.t_start,
                    m.t_expire,
                    m.signature
                )
          
                self.log.info('signer of the receourse receipt %s', sig_addr)
                if sig_addr.lower() == self.monitor_eth_address.lower():
                    print('\n\nRECEIPT CHECKS OUT\n\n')
                else:
                    print('\n\nRECEIPT DOESNT CHECK OUT\n\n')
            

                raw_tx = create_signed_contract_transaction(
                    self.private_key,
                    self.channel_monitor_contract,
                    'recourse',
                    [
                        sender,
                        open_block_number,
                        m.image,
                        m.t_start,
                        m.t_expire,
                        m.signature,
                        7
                    ]
                )

                txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)

                self.log.info('sent recourse transaction (closing %d, monitor %d, txid %s)',
                    closing_balance,
                    monitor_balance,
                    encode_hex(txid)
                )
            except KeyError:
                self.log.info('DIDNT FIND THE RECEIPT. GET REKT')

        
        self.log.info('Forgetting settled channel (sender %s, block number %s)',
                      sender, open_block_number)
        self.state.del_channel(sender, open_block_number)

    def unconfirmed_event_channel_topup(
            self, sender, open_block_number, txhash, added_deposit
    ):
        """Notify the channel manager of a topup with not enough confirmations yet."""
        assert is_checksum_address(sender)
        if (sender, open_block_number) not in self.channels:
            assert (sender, open_block_number) in self.unconfirmed_channels
            self.log.info('Ignoring unconfirmed topup of unconfirmed channel '
                          '(sender %s, block number %s, added %s)',
                          sender, open_block_number, added_deposit)
            return
        self.log.info('Registering unconfirmed deposit top up '
                      '(sender %s, block number %s, added %s)',
                      sender, open_block_number, added_deposit)
        c = self.channels[sender, open_block_number]
        c.unconfirmed_topups[txhash] = added_deposit
        self.state.set_channel(c)

    def event_channel_topup(self, sender, open_block_number, txhash, added_deposit):
        """Notify the channel manager that the deposit of a channel has been topped up."""
        assert is_checksum_address(sender)
        self.log.info(
            'Registering deposit top up (sender %s, block number %s, added deposit %s)',
            sender, open_block_number, added_deposit
        )
        assert (sender, open_block_number) in self.channels
        c = self.channels[sender, open_block_number]
        if c.is_closed is True:
            self.log.warning(
                "Topup of an already closed channel (sender=%s open_block=%d)" %
                (sender, open_block_number)
            )
            return None
        c.deposit += added_deposit
        c.unconfirmed_topups.pop(txhash, None)
        c.mtime = time.time()
        self.state.set_channel(c)

    # end events ####

    def close_channel(self, sender: str, open_block_number: int):
        """Close and settle a channel.
        Params:
            sender (str):               sender address
            open_block_number (int):    block the channel was open in
        """
        assert is_checksum_address(sender)
        if not (sender, open_block_number) in self.channels:
            self.log.warning(
                "attempt to close a non-registered channel (sender=%s open_block=%s" %
                (sender, open_block_number)
            )
            return
        c = self.channels[sender, open_block_number]
        if c.last_signature is None:
            raise NoBalanceProofReceived('Cannot close a channel without a balance proof.')
        # send closing tx
        closing_sig = sign_close(
            self.private_key,
            sender,
            open_block_number,
            c.balance,
            self.channel_manager_contract.address
        )

        print('\nclose channel')
        debug_print([c.last_signature, decode_hex(c.last_signature), closing_sig])
        raw_tx = create_signed_contract_transaction(
            self.private_key,
            self.channel_manager_contract,
            'cooperativeClose',
            [
                self.state.receiver,
                open_block_number,
                c.balance,
                decode_hex(c.last_signature),
                closing_sig
            ]
        )

        # update local state
        c.is_closed = True
        c.mtime = time.time()
        self.state.set_channel(c)

        try:
            txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)
            self.log.info('sent channel close(sender %s, block number %s, tx %s)',
                          sender, open_block_number, encode_hex(txid))
        except InsufficientBalance:
            c.state = ChannelState.CLOSE_PENDING
            self.state.set_channel(c)
            raise

    def force_close_channel(self, sender: str, open_block_number: int):
        """Forcibly remove a channel from our channel state"""
        assert is_checksum_address(sender)
        try:
            self.close_channel(sender, open_block_number)
            return
        except NoBalanceProofReceived:
            c = self.channels[sender, open_block_number]
            c.is_closed = True
            self.state.set_channel(c)

    def sign_close(self, sender: str, open_block_number: int, balance: int):
        """Sign an agreement for a channel closing.
        Returns:
            channel close signature (str): a signature that can be used client-side to close
            the channel by directly calling contract's close method on-chain.
        """
        assert is_checksum_address(sender)
        if (sender, open_block_number) not in self.channels:
            raise NoOpenChannel('Channel does not exist or has been closed'
                                '(sender=%s, open_block_number=%d)' % (sender, open_block_number))
        c = self.channels[sender, open_block_number]
        if c.is_closed:
            raise NoOpenChannel('Channel closing has been requested already.')
        assert balance is not None
        if c.last_signature is None:
            raise NoBalanceProofReceived('Payment has not been registered.')
        if balance != c.balance:
            raise InvalidBalanceProof('Requested closing balance does not match latest one.')
        c.is_closed = True
        c.mtime = time.time()
        receiver_sig = sign_close(
            self.private_key,
            sender,
            open_block_number,
            c.balance,
            self.channel_manager_contract.address
        )
        self.state.set_channel(c)
        self.log.info('signed cooperative closing message (sender %s, block number %s)',
                      sender, open_block_number)
        return receiver_sig

    def get_locked_balance(self):
        """Get the balance in all channels combined."""
        return sum([c.balance for c in self.channels.values()])

    def get_liquid_balance(self):
        """Get the balance of the receiver in the token contract (not locked in channels)."""
        balance = self.token_contract.call().balanceOf(self.receiver)
        return balance

    def get_eth_balance(self):
        """Get eth balance of the receiver"""
        return self.channel_manager_contract.web3.eth.getBalance(self.receiver)

    def verify_balance_proof(self, sender, open_block_number, balance, signature):
        """Verify that a balance proof is valid and return the sender.

        This method just verifies if the balance proof is valid - no state update is performed.

        :returns: Channel, if it exists
        """
        assert is_checksum_address(sender)
        if (sender, open_block_number) in self.unconfirmed_channels:
            raise InsufficientConfirmations(
                'Insufficient confirmations for the channel '
                '(sender=%s, open_block_number=%d)' % (sender, open_block_number))
        try:
            c = self.channels[sender, open_block_number]
        except KeyError:
            raise NoOpenChannel('Channel does not exist or has been closed'
                                '(sender=%s, open_block_number=%s)' % (sender, open_block_number))
        if c.is_closed:
            raise NoOpenChannel('Channel closing has been requested already.')

        sig_addr = verify_balance_proof(
            self.receiver,
            open_block_number,
            balance,
            decode_hex(signature),
            self.channel_manager_contract.address
        )

        if not is_same_address(
                sig_addr,
                sender
        ):
            raise InvalidBalanceProof('Recovered signer does not match the sender')
        return c

    def send_proof_to_monitor(self, sender: str, receiver: str, open_block_number: int, balance: int, signature: bytes):
        sender = to_checksum_address(sender)
        receiver = to_checksum_address(receiver)

        balance_message_hash = get_balance_message(self.state.receiver, open_block_number, balance, self.channel_manager_contract.address)
        print('\nbalance sig')
        debug_print([sender, receiver, open_block_number, balance_message_hash, signature])
        self.monitor.send([NEW_BALANCE_SIG, receiver, sender, open_block_number, balance_message_hash, signature])

#        self.monitor.send([NEW_BALANCE_SIG, receiver, sender, open_block_number, signature])
        self.log.info('WAIT FOR MONITOR TO ACCEPT BALANCE SIG') 
        #while not self.monitor.poll():
        #    pass

        recv = self.monitor.recv()
        self.log.info('MONITOR RESPONDED')

        if recv == BALANCE_SIG_NOT_ACCEPTED:
            self.log.info("monitor didn't accept the signature")
            return

        self.log.info('GOT TO THIS POINT CHECKING TYPE AND RETURN VALUE (type %s)',
            str(type(recv))
        )

        if type(recv) == list and recv[0] == MONITOR_RECEIPT:
            _customer,_sender,_open_block_number,_image,_t_start,_t_expire = recv[1]
            
            assert _sender == sender
            assert _customer == receiver
            assert _open_block_number == open_block_number
            assert _t_expire > _t_start

            signed_receipt_msg = recv[2]
      
            m = MonitorReceipt(_t_start, _t_expire, _image, signed_receipt_msg)

            self.signed_receipts[sender, open_block_number] = m 

            sig_addr = verify_receipt(
                _customer,
                _sender,
                _open_block_number,
                _image,
                _t_start,
                _t_expire,
                signed_receipt_msg
            )
            
            self.log.info('received a receipt signed by %s', sig_addr)

            assert sig_addr.lower() == self.monitor_eth_address.lower()
   
            self.payment_channel.add_payout(1)

            cond_payment_sig = sign_cond_payment(
                self.private_key,
                _sender,
                _open_block_number,
                self.payment_channel.payout,
                True,
                _image
            )

            print('\nconditional payment\n')
            debug_print([_customer, _sender, _open_block_number, self.payment_channel.payout, True, _image, cond_payment_sig])

            self.monitor.send([CONDITIONAL_PAYMENT, _customer, _sender, _open_block_number, self.payment_channel.payout, True, _image, cond_payment_sig])

            start = time.time()
            
            pre_image = self.monitor.recv()

            if pre_image is not None:
                assert keccak256((pre_image, 32)) == _image
                self.monitor_channels[sender, open_block_number] = True
                self.payment_channel.fair_exchange = True
            else:
                self.log.info('MONITOR didnt reveal the pre-image, trigger dispute')

                raw_tx = create_signed_contract_transaction(
                    self.private_key,
                    self.channel_monitor_contract,
                    'triggerdispute',
                    [
                        _sender,
                        _open_block_number
                    ]
                )

                txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)

                self.log.info("triggered dispute because the monitor didn't reveal the pre-image in the receipt (txid %s)", encode_hex(txid))
                self.monitor_channels[sender, open_block_number] = False
                self.payment_channel.fair_exchange = False

        self.log.info('FINISHED THE COND PAYMENT FUNCTION')


    def register_payment(self, sender: str, open_block_number: int, balance: int, signature: str):
        """Register a payment.
        Method will try to reconstruct (verify) balance update data
        with a signature sent by the client.
        If verification is succesfull, an internal payment state is updated.
        Counter within channel is also updated.
        Parameters:
            sender (str):               sender of the balance proof
            open_block_number (int):    block the channel was opened in
            balance (int):              updated balance
            signature(str):             balance proof to verify
        """
        assert is_checksum_address(sender)
        c = self.verify_balance_proof(sender, open_block_number, balance, signature)
        if balance <= c.balance:
            raise InvalidBalanceAmount('The balance must not decrease.')
        if balance > c.deposit:
            raise InvalidBalanceProof('Balance must not be greater than deposit')
        received = balance - c.balance
        c.balance = balance
        c.last_signature = signature
        c.mtime = time.time()
        self.previous_channel_sigs[signature] = (sender, open_block_number, balance)
        self.state.set_channel(c)
        self.log.debug('registered payment (sender %s, block number %s, new balance %s)',
                       c.sender, open_block_number, balance)


        if self.payment_channel.fair_exchange:
            self.send_proof_to_monitor(sender, c.receiver, open_block_number, balance, signature)
        else:
            self.log.info("DON'T outsource to monitor because he didn't do the fair exchange correctly")

        return c.sender, received

    def reset_unconfirmed(self):
        """Forget all unconfirmed channels and topups to allow for a clean resync."""
        self.state.del_unconfirmed_channels()
        for channel in self.channels.values():
            channel.unconfirmed_topups.clear()
            self.state.set_channel(channel)
        self.state.unconfirmed_head_number = self.state.confirmed_head_number
        self.state.unconfirmed_head_hash = self.state.confirmed_head_hash

    @property
    def channels(self):
        return self.state.channels

    @property
    def unconfirmed_channels(self):
        return self.state.unconfirmed_channels

    @property
    def pending_channels(self):
        return self.state.pending_channels

    def channels_to_dict(self):
        """Export all channels as a dictionary."""
        d = {}
        for sender, block_number in self.channels:
            channel = self.channels[sender, block_number]
            channel_dict = {
                'deposit': channel.deposit,
                'balance': channel.balance,
                'mtime': channel.mtime,
                'ctime': channel.ctime,
                'settle_timeout': channel.settle_timeout,
                'last_signature': channel.last_signature,
                'is_closed': channel.is_closed
            }
            if sender not in d:
                d[sender] = {}
            d[sender][block_number] = channel_dict
        return d

    def unconfirmed_channels_to_dict(self):
        """Export all unconfirmed channels as a dictionary."""
        d = {}
        for sender, block_number in self.unconfirmed_channels:
            channel = self.unconfirmed_channels[sender, block_number]
            channel_dict = {
                'deposit': channel.deposit,
                'ctime': channel.ctime
            }
            if sender not in d:
                d[sender] = {}
            d[sender][block_number] = channel_dict
        return d

    def wait_sync(self):
        self.blockchain.wait_sync()

    def node_online(self):
        return self.blockchain.is_connected.is_set()

    def get_token_address(self):
        return self.token_contract.address

    def check_contract_version(self):
        """Compare version of the contract to the version of the library.
        Only major and minor version is used in the comparison.
        """
        deployed_contract_version = self.channel_manager_contract.call().version()
        deployed_contract_version = deployed_contract_version.rsplit('.', 1)[0]
        library_version = CHANNEL_MANAGER_CONTRACT_VERSION.rsplit('.', 1)[0]
        if deployed_contract_version != library_version:
            raise InvalidContractVersion("Incompatible contract version: expected=%s deployed=%s" %
                                         (CHANNEL_MANAGER_CONTRACT_VERSION,
                                          deployed_contract_version))

    def close_pending_channels(self):
        """Close all channels that are in CLOSE_PENDING state.
        This state happens if the receiver's eth balance is not enough to
            close channel on-chain."""
        for sender, open_block_number in self.pending_channels.keys():
            self.close_channel(sender, open_block_number)  # dispute by closing the channel

    def get_monitor_connection(self, sender: str, open_block_number: int):
        return self.monitor_connections[sender, open_block_number]

    def event_monitor_interference(self, sender: str, open_block_number: int):
        assert is_checksum_address(sender)
        balance_msg, balance_sig = self.channel_manager_contract.call().getMonitorEvidence(
            sender,
            self.state.receiver,
            open_block_number
        )

        self.log.info('got monitor evidence (msg %s, sig %s)',
            balance_msg,
            balance_sig
        )

        channel = self.channels[sender, open_block_number]

        sig_addr = verify_balance_proof(
            self.receiver,
            open_block_number,
            channel.balance,
            balance_sig,
            self.channel_manager_contract.address
        )

        if not is_same_address(
            sig_addr,
            sender
        ):
            self.log.info('WARNING: monitor gave signature for different address (sig addr %s, sener %s)',
                sig_addr,
                sender
            )

        else:
            if encode_hex(balance_sig) in self.previous_channel_sigs:
                self.log.info('\n\nthe monitor signature submitted was ACTUALLY an old one (sig %s)\n\n',
                    encode_hex(balance_sig)
                )
            else:
                self.log.info('\n\nthe monitor evidence was NOT found in previous payments (sig %s)\n\n',
                    encode_hex(balance_sig)
                )

                print('\n\n%s\n\n', self.previous_channel_sigs)

            self.log.info('monitor responded with the most recent state, HOORAY!')

    def reveal_monitor_submission(
        self,
        sender: str,
        open_block_number: int,
        balance: int,
        monitor_timeout: int,
        settle_timeout: int
    ):
        assert is_checksum_address(sender)
        assert settle_timeout >= 0

        if (sender, open_block_number) not in self.channels:
            self.log.info('SHIIIET: trying to reveal evidence for channel not tracked!')
            return
        c = self.channels[sender, open_block_number]
        
        balance_msg, balance_sig = self.channel_manager_contract.call().getMonitorEvidence(
            sender,
            self.state.receiver,
            open_block_number
        )

        assert encode_hex(balance_sig) in self.previous_channel_sigs

        _sender,_open_block_number,_balance = self.previous_channel_sigs[encode_hex(balance_sig)]

        assert _sender == sender
        assert _open_block_number == open_block_number
    
        self.log.info('revealing evidence (channel balance %d, client balance %d, monitor balance %d)',
            c.balance,
            balance,
            _balance
        )

        self.log.info('provided balance message %s',
            balance_msg
        )

        self.log.info('calculatred balance messagae %s',
            get_balance_message(
                self.state.receiver,
                open_block_number,
                _balance,
                self.channel_manager_contract.address
            )
        )

        raw_tx = create_signed_contract_transaction(
            self.private_key,
            self.channel_manager_contract,
            'revealMonitorEvidence',
            args=[sender, self.receiver, open_block_number, _balance],
        )

        txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)

        self.log.info('sent reveal transaction %s', encode_hex(txid))

    def event_dispute(self, customer: str, sender: str, open_block_number: int):
        assert is_checksum_address(customer)
        assert is_checksum_address(sender)

        raw_tx = create_signed_contract_transaction(
            self.private_key,
            self.channel_monitor_contract,
            'resolve',
            []
        )

        txid = self.blockchain.web3.eth.sendRawTransaction(raw_tx)

        self.log.info('send RESOLVE transaction %s', encode_hex(txid))

    def event_set_state(self, customer: str, sender: str, open_block_number: int, pre_image: int):
        assert is_checksum_address(customer)
        assert is_checksum_address(sender)

        m = self.signed_receipts[sender, open_block_number]

        assert keccak256((pre_image, 32)) == m.image
        
        self.log.info('Deleting resolve timer for channel with monitor')

        del self.blockchain.wait_to_resolve[self.blockchain.resolve_timeouts[sender,open_block_number]]
        del self.blockchain.resolve_timeouts[sender,open_block_number]

         
class ManagerListener(gevent.Greenlet):
    def __init__(
        self,
        monitor: Client,
        channel_manager: ChannelManager
    ):
        gevent.Greenlet.__init__(self)
        self.channel_manager = channel_manager
    
        self.log = logging.getLogger('channel_manager')

        self.monitor = monitor

    def listen_forever(self):
        while True:
            try:
                print('listened for something')
            except EOFError:
                print('EOF')
                break

