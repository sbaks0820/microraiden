import requests
import sys
import gevent
import logging

from ethereum.exceptions import InsufficientBalance
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import BadFunctionCallOutput
from eth_utils import is_same_address, to_checksum_address

from microraiden.config import NETWORK_CFG
from microraiden.constants import PROXY_BALANCE_LIMIT
from microraiden.utils import get_logs


class Blockchain(gevent.Greenlet):
    """Class that watches the blockchain and relays events to the channel manager."""
    poll_interval = 2

    def __init__(
            self,
            web3: Web3,
            channel_manager_contract: Contract,
            channel_monitor_contract: Contract,
            channel_manager,
            n_confirmations,
            sync_chunk_size=100 * 1000
    ):
        gevent.Greenlet.__init__(self)
        self.web3 = web3
        self.channel_manager_contract = channel_manager_contract
        self.channel_monitor_contract = channel_monitor_contract
        self.cm = channel_manager
        self.n_confirmations = n_confirmations
        self.log = logging.getLogger('blockchain')
        self.wait_sync_event = gevent.event.Event()
        self.is_connected = gevent.event.Event()
        self.sync_chunk_size = sync_chunk_size
        self.running = False
        #  insufficient_balance
        #  - set to true if for some reason tx can't be send
        #  - set to false when all pending closes are settled
        #  - used to dispute/close channels that are in CHALLENGED state after manager
        #     has ether to spend again
        self.insufficient_balance = False
        self.sync_start_block = NETWORK_CFG.start_sync_block


    ''' 
        This is the main function that tries to call _update every time it is called. 
        It first asserts is there is sufficient balance for manager and fails is there
        isn't.
    '''
    def _run(self):
        self.running = True
        self.log.info('starting blockchain polling (interval %ss)', self.poll_interval)
        while self.running:
            if self.insufficient_balance:
                self.insufficient_balance_recover()
            try:
                self._update()
                self.is_connected.set()
                if self.wait_sync_event.is_set():
                    gevent.sleep(self.poll_interval)
            except requests.exceptions.ConnectionError as e:
                endpoint = self.web3.currentProvider.endpoint_uri
                self.log.warning(
                    'Ethereum node (%s) refused connection. Retrying in %d seconds.' %
                    (endpoint, self.poll_interval)
                )
                gevent.sleep(self.poll_interval)
                self.is_connected.clear()
        self.log.info('stopped blockchain polling')

    def stop(self):
        self.running = False

    def wait_sync(self):
        """Block until event polling is up-to-date with a most recent block of the blockchain"""
        self.wait_sync_event.wait()


    def _update(self):
        current_block = self.web3.eth.blockNumber
        # reset unconfirmed channels in case of reorg
        if self.wait_sync_event.is_set():  # but not on first sync
            if current_block < self.cm.state.unconfirmed_head_number:
                self.log.info('chain reorganization detected. '
                              'Resyncing unconfirmed events (unconfirmed_head=%d) [@%d]' %
                              (self.cm.state.unconfirmed_head_number, self.web3.eth.blockNumber))
                self.cm.reset_unconfirmed()
            try:
                # raises if hash doesn't exist (i.e. block has been replaced)
                self.web3.eth.getBlock(self.cm.state.unconfirmed_head_hash)
            except ValueError:
                self.log.info('chain reorganization detected. '
                              'Resyncing unconfirmed events (unconfirmed_head=%d) [@%d]. '
                              '(getBlock() raised ValueError)' %
                              (self.cm.state.unconfirmed_head_number, current_block))
                self.cm.reset_unconfirmed()

            # in case of reorg longer than confirmation number fail
            try:
                self.web3.eth.getBlock(self.cm.state.confirmed_head_hash)
            except ValueError:
                self.log.critical('events considered confirmed have been reorganized')
                assert False  # unreachable as long as confirmation level is set high enough

        if self.cm.state.confirmed_head_number is None:
            self.cm.state.update_sync_state(confirmed_head_number=self.sync_start_block)
        if self.cm.state.unconfirmed_head_number is None:
            self.cm.state.update_sync_state(unconfirmed_head_number=self.sync_start_block)
        
        new_unconfirmed_head_number = self.cm.state.unconfirmed_head_number + self.sync_chunk_size
        new_unconfirmed_head_number = min(new_unconfirmed_head_number, current_block)
        new_confirmed_head_number = max(new_unconfirmed_head_number - self.n_confirmations, 0)
        
        # return if blocks have already been processed
        if (self.cm.state.confirmed_head_number >= new_confirmed_head_number and
                self.cm.state.unconfirmed_head_number >= new_unconfirmed_head_number):
            return

        # Look for events in the state guardian contract with the customer
        # before the channel manager. If customer does something in between
        # monitor probably needs to handle close rather than responding to 
        # customer channel.

        #for job in self.cm.jobs:
        for customer,sender,open_block_number in self.cm.jobs:
           
            # filter for events after block_number
            filters_confirmed = {
                'from_block': self.cm.state.confirmed_head_number + 1,
                'to_block': new_confirmed_head_number,
                'argument_filters': {
                    '_receiver_address': customer
                }
            }
            filters_unconfirmed = {
                'from_block': self.cm.state.unconfirmed_head_number + 1,
                'to_block': new_unconfirmed_head_number,
                'argument_filters': {
                    '_receiver_address': customer
                }
            }
            self.log.debug(
                'filtering for channel events u:%s-%s c:%s-%s @%d',
                filters_unconfirmed['from_block'],
                filters_unconfirmed['to_block'],
                filters_confirmed['from_block'],
                filters_confirmed['to_block'],
                current_block
            )

            # channel close requested
            logs = get_logs(
                self.channel_manager_contract,
                'ChannelCloseRequested',
                **filters_confirmed
            )
            for log in logs:
                self.log.info('detected a channel close request in channel manager')
                #assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
                sender = log['args']['_sender_address']
                receiver = log['args']['_receiver_address']
                sender = to_checksum_address(sender)
                receiver = to_checksum_address(receiver)
                open_block_number = log['args']['_open_block_number']
                balance = log['args']['_balance']
                self.log.info('sucessfully parsed the event information')
                self.log.info('address of channel manager %s', self.channel_manager_contract.address)
                try:
                    self.log.info('params to get info: %s, %s, %d', sender, self.cm.state.receiver, open_block_number)
                    mtimeout,timeout = self.channel_manager_contract.call().getChannelInfo(
                        sender,
                        self.cm.state.receiver,
                        open_block_number
                    )[2:4]
                except BadFunctionCallOutput:
                    self.log.info('BadFunctionCallOutput error caught when trying to get channel info')
                    continue
                try:
                    self.cm.event_channel_close_requested(sender, open_block_number, balance, timeout)
                except InsufficientBalance:
                    self.log.fatal('Insufficient ETH balance of the receiver. '
                                   "Can't close the channel. "
                                   'Will retry once the balance is sufficient')
                    self.insufficient_balance = True
                    # TODO: recover


            logs = get_logs(
                self.channel_manager_contract,
                'DebugInputs',
                **filters_unconfirmed
            )

            for log in logs:
                msg = log['args']['msg']
                sig = log['args']['sig']
                
                print('inputs')
                print('msg', type(msg), msg)
                print('sig', type(sig), sig)

            logs = get_logs(
                self.channel_manager_contract,
                'DebugVerify',
                **filters_unconfirmed
            )

            for log in logs:
                sender = log['args']['_sender']
                msg = log['args']['msg']
                dig = log['args']['sig']

                print('verify')
                print('msg', type(msg), msg)
                print('sig', type(sig), sig)

            # channel settled event
            logs = get_logs(
                self.channel_manager_contract,
                'ChannelSettled',
                **filters_confirmed
            )
            for log in logs:
                #assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
                sender = log['args']['_sender_address']
                sender = to_checksum_address(sender)
                open_block_number = log['args']['_open_block_number']
                self.log.debug('received ChannelSettled event (sender %s, block number %s)',
                               sender, open_block_number)
                self.cm.event_channel_settled(sender, open_block_number)

            logs = get_logs(
                self.channel_monitor_contract,
                'Dispute',
                **filters_confirmed
            )

            for log in logs:
                customer = to_checksum_address(log['args']['_customer_address'])
                sender = to_checksum_address(log['args']['_sender_address'])
                open_block_number = log['args']['_open_block_number']
                
                self.log.info('Customer triggered a dispute (customer %s, sender %s, open_block_number %d)',
                    customer,
                    sender,
                    open_block_number
                )
           
                self.cm.event_customer_dispute(customer, sender, open_block_number)

            logs = get_logs(
                self.channel_monitor_contract,
                'Resolve',
                **filters_unconfirmed
            )

            for log in logs:
                self.log.info('\n\n saw resolve event happen \n\n')


        # update head hash and number
        try:
            new_unconfirmed_head_hash = self.web3.eth.getBlock(new_unconfirmed_head_number).hash
            new_confirmed_head_hash = self.web3.eth.getBlock(new_confirmed_head_number).hash
        except AttributeError:
            self.log.critical("RPC endpoint didn't return proper info for an existing block "
                              "(%d,%d)" % (new_unconfirmed_head_number, new_confirmed_head_number))
            self.log.critical("It is possible that the blockchain isn't fully synced. "
                              "This often happens when Parity is run with --fast or --warp sync.")
            self.log.critical("Can't continue - check status of the ethereum node.")
            sys.exit(1)
        self.cm.set_head(
            new_unconfirmed_head_number,
            new_unconfirmed_head_hash,
            new_confirmed_head_number,
            new_confirmed_head_hash
        )
        if not self.wait_sync_event.is_set() and new_unconfirmed_head_number == current_block:
            self.wait_sync_event.set()

    def insufficient_balance_recover(self):
        """Recover from an insufficient balance state by closing
        all pending channels if possible."""
        balance = self.web3.eth.getBalance(self.cm.receiver)
        if balance < PROXY_BALANCE_LIMIT:
            return
        try:
            self.cm.close_pending_channels()
            self.insufficient_balance = False
        except InsufficientBalance:
            self.log.fatal('Insufficient balance when trying to'
                           'close pending channels. (balance=%d)'
                           % balance)
