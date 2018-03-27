import re
import sys

from web3 import Web3, HTTPProvider

from microraiden import Session
from microraiden.utils import get_private_key, privkey_to_addr, create_signed_contract_transaction
from microraiden.config import NETWORK_CFG
import logging
import requests
import json
import gevent

from microraiden.channel_manager import ChannelManager
from microraiden.make_helpers import make_channel_monitor
from microraiden.channel_monitor import ChannelMonitor, MonitorListener
#from .state import ChannelManagerState
#from .blockchain import Blockchain

from web3.middleware.pythonic import (
    pythonic_middleware,
    to_hexbytes,
)

from multiprocessing.connection import Listener


log = logging.getLogger(__name__)

monitor_address = ('localhost', 6001)

config = json.load(open('./main.json'))
size_extraData_for_poa = 200
pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByNumber'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)
pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByHash'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)

def background():
    while True:
        gevent.sleep(1)


class CustomerConnection(gevent.Greenlet):
    def __init__(
        self,
        address: str,
        port: int
    ):
        gevent.Greenlet.__init__(self)
        self.address = address
        self.port = port

    def _run(self):
        listener = Listener(monitor_address)
        conn = listener.accept()
        print('connection acception from', listener.last_accepted)
        print(conn.recv())
        conn.close()
        gevent.sleep(1)

def main(
        close_channel: bool = True,
):
    w3 = Web3(HTTPProvider(config['web3path']))
    NETWORK_CFG.set_defaults(int(w3.version.network))
    private_key = get_private_key('./dragonstone-rinkeby-02-03')
    print("Web3 Provider:", config['web3path'])
    #print('Private Key  :', config['private-key'])
    log.info('This private key %s', private_key)
    log.info('This address %s', privkey_to_addr(private_key))
#    print('Password Path:', config['password-path'])
#    print('Resource Reqd:', config['resource'])
#    print('Manager  Addr:', config['manager'])
#    print('Close Channel:', close_channel)
    close_channels(private_key, config['manager'], config['monitor'], w3)

def close_channels(
        private_key: str,
#        password_path: str,
#        resource: str,
        channel_manager_address: str = None,
        channel_monitor_address: str = None,
        web3: Web3 = None,
#        retry_interval: float = 5,
#        endpoint_url: str = 'http://0.0.0.0:5000',
):

    channel_monitor = make_channel_monitor(
        private_key,
        channel_manager_address,
        channel_monitor_address,
        './db/echo_monitor.db',
        web3,
        cheat=False,
        _reveal_pre_image = False
    )

    listener = MonitorListener('localhost', 6001, channel_monitor)
    listener.start()
    #channel_monitor.start()
    #channel_monitor.wait_sync()
    #customer = CustomerConnection('localhost', 6001)

    #customer.start()

    #listener = Listener(monitor_address)
    #conn = listener.accept()
    #print('connection accepted from', listener.last_accepted)
    #print(conn.recv())
    #conn.send('share')
    #conn.close()
    #listener.close()
    #
    #background()

    listener.join()
    #channel_monitor.join()
    print ('THREAD JOINED') 
    # Create the client session.
#    session = Session(
#        endpoint_url=endpoint_url,
#        private_key=private_key,
#        key_password_path=password_path,
#        channel_manager_address=channel_manager_address,
#        web3=web3,
#        retry_interval=retry_interval,
#        close_channel_on_exit=close_channel
#    )
#    print("Private Key:", private_key)
#    addr = privkey_to_addr(private_key)
#    print("Address:", addr)
##    response = requests.get('http://0.0.0.0:5000/api/1/channels/{}'.format(addr))
#    response = session.get('{}/api/1/channels/{}'.format('http://0.0.0.0:5000', addr))
#    print(response)
#
#    response = session.get('{}/{}'.format('http://0.0.0.0:5000',resource))
#    print(response, response.text)
##
#    session.channel.close(balance=session.channel.balance-1)
#
##session.close_channel()
#    response = requests.get('http://0.0.0.0:5000/api/1/channels/{}'.format(addr))
#    print(response)
#    channels = json.loads(response.text)
#    print(channels)


if __name__ == '__main__':
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    main()
