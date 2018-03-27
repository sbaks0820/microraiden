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
            channel_manager,
            n_confirmations,
            sync_chunk_size=100 * 1000
    ):
        gevent.Greenlet.__init__(self)
        self.web3 = web3
        self.channel_manager_contract = channel_manager_contract
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

        self.channel_monitor_contract = None
        self.wait_to_dispute = dict()
        self.wait_to_resolve = dict()
        self.resolve_timeouts = dict()

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

        # filter for events after block_number
        filters_confirmed = {
            'from_block': self.cm.state.confirmed_head_number + 1,
            'to_block': new_confirmed_head_number,
            'argument_filters': {
                '_receiver_address': self.cm.state.receiver
            }
        }
        filters_unconfirmed = {
            'from_block': self.cm.state.unconfirmed_head_number + 1,
            'to_block': new_unconfirmed_head_number,
            'argument_filters': {
                '_receiver_address': self.cm.state.receiver
            }
        }
        self.log.debug(
            'filtering for events u:%s-%s c:%s-%s @%d',
            filters_unconfirmed['from_block'],
            filters_unconfirmed['to_block'],
            filters_confirmed['from_block'],
            filters_confirmed['to_block'],
            current_block
        )

        # unconfirmed channel created
        logs = get_logs(
            self.channel_manager_contract,
            'ChannelCreated',
            **filters_unconfirmed
        )
        for log in logs:
            assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
            sender = log['args']['_sender_address']
            sender = to_checksum_address(sender)
            deposit = log['args']['_deposit']
            open_block_number = log['blockNumber']
            self.log.debug(
                'received unconfirmed ChannelCreated event (sender %s, block number %s)',
                sender,
                open_block_number
            )
            self.cm.unconfirmed_event_channel_opened(sender, open_block_number, deposit)

        # channel created
        logs = get_logs(
            self.channel_manager_contract,
            'ChannelCreated',
            **filters_confirmed
        )
        for log in logs:
            assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
            sender = log['args']['_sender_address']
            sender = to_checksum_address(sender)
            deposit = log['args']['_deposit']
            open_block_number = log['blockNumber']
            self.log.debug('received ChannelOpened event (sender %s, block number %s)',
                           sender, open_block_number)
            self.cm.event_channel_opened(sender, open_block_number, deposit)

        # unconfirmed channel top ups
        logs = get_logs(
            self.channel_manager_contract,
            'ChannelToppedUp',
            **filters_unconfirmed
        )
        for log in logs:
            assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
            txhash = log['transactionHash']
            sender = log['args']['_sender_address']
            sender = to_checksum_address(sender)
            open_block_number = log['args']['_open_block_number']
            added_deposit = log['args']['_added_deposit']
            self.log.debug(
                'received top up event (sender %s, block number %s, deposit %s)',
                sender,
                open_block_number,
                added_deposit
            )
            self.cm.unconfirmed_event_channel_topup(
                sender,
                open_block_number,
                txhash,
                added_deposit
            )

        # confirmed channel top ups
        logs = get_logs(
            self.channel_manager_contract,
            'ChannelToppedUp',
            **filters_confirmed
        )
        for log in logs:
            assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
            txhash = log['transactionHash']
            sender = log['args']['_sender_address']
            sender = to_checksum_address(sender)
            open_block_number = log['args']['_open_block_number']
            added_deposit = log['args']['_added_deposit']
            self.log.debug(
                'received top up event (sender %s, block number %s, added deposit %s)',
                sender,
                open_block_number,
                added_deposit
            )
            self.cm.event_channel_topup(sender, open_block_number, txhash, added_deposit)

        # channel settled event
        logs = get_logs(
            self.channel_manager_contract,
            'ChannelSettled',
            **filters_confirmed
        )
        for log in logs:
            assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
            sender = log['args']['_sender_address']
            sender = to_checksum_address(sender)
            open_block_number = log['args']['_open_block_number']
            self.log.debug('received ChannelSettled event (sender %s, block number %s)',
                           sender, open_block_number)
            self.cm.event_channel_settled(sender, open_block_number)

        # channel close requested
        logs = get_logs(
            self.channel_manager_contract,
            'ChannelCloseRequested',
            **filters_confirmed
        )
        for log in logs:
            assert is_same_address(log['args']['_receiver_address'], self.cm.state.receiver)
            sender = log['args']['_sender_address']
            sender = to_checksum_address(sender)
            open_block_number = log['args']['_open_block_number']
            if (sender, open_block_number) not in self.cm.channels:
                continue
            balance = log['args']['_balance']
            try:
                self.log.info('params to get info: %s, %s, %d', sender, self.cm.state.receiver, open_block_number)
                mtimeout,timeout = self.channel_manager_contract.call().getChannelInfo(
                    sender,
                    self.cm.state.receiver,
                    open_block_number
                    )[2:4]
            except BadFunctionCallOutput:
                self.log.warning(
                    'received ChannelCloseRequested event for a channel that doesn\'t '
                    'exist or has been closed already (sender=%s open_block_number=%d)'
                    % (sender, open_block_number))
                self.cm.force_close_channel(sender, open_block_number)
                continue
            self.log.debug('received ChannelCloseRequested event (sender %s, block number %s)',
                           sender, open_block_number)
            self.log.info('received ChannelCloseRequested event (blocknumber %s, monitor period %s, timeout %s)', 
                self.cm.state.confirmed_head_number, mtimeout, timeout)

            self.wait_to_dispute[int(mtimeout)] = (sender, open_block_number, balance, mtimeout, timeout)

            #try:
            #    self.cm.event_channel_close_requested(sender, open_block_number, balance, mtimeout, timeout)
            #except InsufficientBalance:
            #    self.log.fatal('Insufficient ETH balance of the receiver. '
            #                   "Can't close the channel. "
            #                   'Will retry once the balance is sufficient')
            #    self.insufficient_balance = True
                # TODO: recover

        logs = get_logs(
            self.channel_manager_contract,
            'MonitorInterference',
            **filters_unconfirmed
        )

        for log in logs:
            sender = log['args']['_sender_address']
            sender = to_checksum_address(sender)
            open_block_number = log['args']['open_block_number']
            if (sender, open_block_number) not in self.cm.channels:
                self.log.info("monitor interfered in channel that wasn't outsourced (sender %s open_block_number %s, balance_msg_sig %s)", 
                    sender,
                    open_block_number,
                    balance_msg_sig)
            
            self.cm.event_monitor_interference(sender, open_block_number)
       
#        logs = get_logs(
#            self.channel_monitor_contract,
#            'Dispute',
#            **filters_confirmed
#        )
#
#        for log in logs:
#            self.log.info('DECETED the dispute successfully go throughi (customer %s, settle timeout %d)',
#                log['args']['_sender_address'],
#                log['args']['_channel_settle']
#            )

        logs = get_logs(
            self.channel_manager_contract,
            'RevealSigner',
            **filters_unconfirmed
        )

        for log in logs:
            self.log.info('DETECTED REVEAL SIGNER EVENT')

        logs = get_logs(
            self.channel_manager_contract,
            'RevealCorrectBalance',
            **filters_unconfirmed
        )

        for log in logs:
            self.log.info('DETECTED RevealCorrectBalance')

        logs = get_logs(
            self.channel_manager_contract,
            'RevealIncorrectBalance',
            **filters_unconfirmed
        )

        for log in logs:
            self.log.info('DETECTED RevealIncorrectBalance')


        logs = get_logs(
            self.channel_monitor_contract,
            'StealMonitorDeposit',
            **filters_confirmed
        )

        for log in logs:
            #self.log.info('STEAL THE MONITORS DEPOSIT FOR NOT RESPONDING CORRECTLY(%d, %d)', log['args']['_monitor_balance'], log['args']['_closing_balance'])
            self.log.info('STEAL THE MONITORS DEPOSIT')

        logs = get_logs(
            self.channel_monitor_contract,
            'LeaveMonitorDeposit',
            **filters_confirmed
        )

        for log in logs:
            #self.log.info('LEAVE THE MONITORS DEPOSIT (%d, %d)', log['args']['_monitor_balance'], log['args']['_closing_balance'])
            self.log.info('LEAVE THE MONITORS DEPOSIT')

        logs = get_logs(
            self.channel_monitor_contract,
            'ClosingInfo',
            **filters_unconfirmed
        )

        for log in logs:
            closing_balance = log['args']['_closing_balance']
            monitor_balance = log['args']['_monitor_balance']
            settle_block = log['args']['_settle_block']
            self.log.info('\n Channel closing info (closing balance %d, monitor balance %d, settle block %d)',
                closing_balance,
                monitor_balance,
                settle_block
            )


        logs = get_logs(
            self.channel_monitor_contract,
            'Dispute',
            **filters_unconfirmed
        )

        for log in logs:
            customer = log['args']['_customer_address']
            open_block_number = log['args']['_open_block_number']
            sender = log['args']['_sender_address']
            self.log.info('DETECTED UNCONFIMED DISPUTE from monitor contract (customer %s, sender %s, open_block_number %d)',
                customer,
                sender,
                open_block_number
            )

        logs = get_logs(
            self.channel_monitor_contract,
            'Dispute',
            **filters_confirmed
        )

        for log in logs:
            customer = to_checksum_address(log['args']['_customer_address'])
            open_block_number = log['args']['_open_block_number']
            sender = to_checksum_address(log['args']['_sender_address'])
            settle_timeout = int(log['data'], 16)
            self.log.info('DETECTED CONFIMED DISPUTE from monitor contract (customer %s, sender %s, open_block_number %d, settle timeout %d, current block %d)',
                customer,
                sender,
                open_block_number,
                settle_timeout,
                self.cm.blockchain.web3.eth.blockNumber
            )

            self.wait_to_resolve[settle_timeout+1] = (customer, sender, open_block_number)
            self.resolve_timeouts[sender,open_block_number] = settle_timeout+1

        logs = get_logs(
            self.channel_monitor_contract,
            'Resolve',
            **filters_confirmed
        )

        for log in logs:
            customer = to_checksum_address(log['args']['_sender_address'])
            self.log.info('successfully resolved channel (customer %s)', customer)

        logs = get_logs(
            self.channel_monitor_contract,
            'DebugHash',
            **filters_unconfirmed
        )

        for log in logs:
            pre_image = log['args']['_pre_image']
            image = log['args']['_image']

            self.log.info('\nDEBUG HASH (pre image %d, hash %s)', pre_image, image)

        logs = get_logs(
            self.channel_monitor_contract,
            'DebugSigner',
            **filters_unconfirmed
        )

        for log in logs:
            signer = to_checksum_address(log['args']['_signer'])
            self.log.info('\nDEBUG SIGNER (signer %s)', signer)

        logs = get_logs(
            self.channel_monitor_contract,
            'PayoutInfo',
            **filters_unconfirmed
        )

        for log in logs:
            payout = log['args']['_payout']
            deposit = log['args']['_deposit']
            self.log.info('\nPAYOUT INFO (payout %d, deposit %d)\n', payout, deposit)

        logs = get_logs(
            self.channel_monitor_contract,
            'Evidence',
            **filters_confirmed
        )

        for log in logs:
            customer = to_checksum_address(log['args']['_customer_address'])
            sender = to_checksum_address(log['args']['_sender_address'])
            pre_image = log['args']['_pre_image']
            open_block_number = int(log['data'], 16)

            if (sender, open_block_number) not in self.cm.channels or customer != self.cm.receiver:
                self.log.info('received SET STATE for unknown channel')
            else:
                self.log.info('Monitor called SET STATE (sender %s, open_block_number %d, pre_image %d)',
                    sender,
                    open_block_number,
                    pre_image
                )
                self.cm.event_set_state(customer, sender, open_block_number, pre_image)


        # See which close requests are ready to be responded to
        # and process them normally.
        disputed = []
        for mtimeout in self.wait_to_dispute:
            if self.cm.state.confirmed_head_number > mtimeout:
                try:
                    if not self.cm.monitor_channels[self.wait_to_dispute[mtimeout][0], self.wait_to_dispute[mtimeout][1]]:
                        self.log.info("No EXPECTATION of monitor event responding due to no fair exchange")
                    else:
                        self.log.info('Processing close request at block %s, mtimeout %s',
                            self.cm.state.confirmed_head_number, mtimeout)
                        self.cm.reveal_monitor_submission(*self.wait_to_dispute[mtimeout])
                        self.cm.event_channel_close_requested(*self.wait_to_dispute[mtimeout])
                except InsufficientBalance:
                    self.log.fatal('Insufficient ETH balance of the receiver. '
                                    "Can't close the channel. "
                                    'Will retry once the balance is sufficient')
                    self.insufficient_balance = True
                    # TODO: recover
                disputed.append(mtimeout)

        for dispute in disputed:
            del self.wait_to_dispute[dispute]

        resolved = []
        for stimeout in self.wait_to_resolve:
            if self.cm.blockchain.web3.eth.blockNumber > stimeout:
                self.log.info('Processing DISPUTE request at block %s, stimeout %s',
                    self.cm.blockchain.web3.eth.blockNumber, stimeout
                )
                customer,sender,open_block_number = self.wait_to_resolve[stimeout]
                self.cm.event_dispute(customer, sender, open_block_number)
                resolved.append(stimeout)

        for resolve in resolved:
            del self.wait_to_resolve[resolve]

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
