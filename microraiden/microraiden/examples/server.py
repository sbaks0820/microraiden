"""
This is dummy code showing how the minimal app could look like.
In his case we don't use a proxy, but directly a server
"""
import logging
import os
import click
import json
import gevent
from flask import request
from web3 import Web3, HTTPProvider

from microraiden.channel_manager import ChannelManager
from microraiden.make_helpers import make_channel_manager
from microraiden.constants import WEB3_PROVIDER_DEFAULT
from microraiden.config import NETWORK_CFG
from microraiden.proxy import PaywalledProxy
from microraiden.proxy.resources import Expensive
from microraiden.utils import get_private_key, privkey_to_addr


from web3.middleware.pythonic import (
    pythonic_middleware,
    to_hexbytes,
)

log = logging.getLogger(__name__)

config = json.load( open('./main.json'))
size_extraData_for_poa = 200
pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByNumber'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)
pythonic_middleware.__closure__[2].cell_contents['eth_getBlockByHash'].args[1].args[0]['extraData'] = to_hexbytes(size_extraData_for_poa, variable_length=True)

class StaticPriceResource(Expensive):
    def get(self, url: str, param: str):
        log.info('Resource requested: {} with param "{}"'.format(request.url, param))
        return param


class DynamicPriceResource(Expensive):
    def get(self, url: str, param: int):
        log.info('Resource requested: {} with param "{}"'.format(request.url, param))
        return param

    def price(self):
        return int(request.view_args['param'])

def main():
    web3 = Web3(HTTPProvider(config['web3path']))
    NETWORK_CFG.set_defaults(int(web3.version.network))
    private_key = get_private_key('./dragonstone-rinkeby')
    log.info('This private key %s', private_key)
    log.info('This address %s', privkey_to_addr(private_key))
    channel_manager = make_channel_manager(
        private_key,
        config['manager'],
        './db/echo_server.db',
        web3,
        'localhost',
        6001
    )

    run(private_key=private_key, channel_manager=channel_manager)


def run(
        private_key: str,
        state_file_path: str = './db/echo_server.db',
        channel_manager: ChannelManager = None,
        join_thread: bool = True
):
    dirname = os.path.dirname(state_file_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    log.info('(manager %s, token %s)', channel_manager.channel_manager_contract.address,
                                       channel_manager.token_contract.address)

    #config_client = json.load(open('client.json'))
    #config_client['manager'] = channel_manager.channel_manager_contract.address
    #config_client['token'] = channel_manager.token_contract.address
    #json.dump(config_client, open('client.json', 'w'))

    #config_monitor = json.load(open('monitor.json'))
    #config_monitor['manager'] = channel_manager.channel_manager_contract.address
    #config_monitor['token'] = channel_manager.token_contract.address
    #json.dump(config_monitor, open('monitor.json', 'w'))

    #log.info(
    #    'Updated manager address in monitor and client configs.'
    #)

    # set up a paywalled proxy
    # arguments are:
    #  - private key to use for receiving funds
    #  - file for storing state information (balance proofs)

    if channel_manager is None:
        raise Exception('Channel Manager not created!')


    app = PaywalledProxy(channel_manager)

    # Add resource defined by regex and with a fixed price of 1 token.
    app.add_paywalled_resource(
        StaticPriceResource,
        "/echofix/<string:param>",
        price=5
    )
    # Resource with a price determined by the second parameter.
    app.add_paywalled_resource(
        DynamicPriceResource,
        "/echodyn/<int:param>"
    )

    # Start the app. proxy is a WSGI greenlet, so you must join it properly.
    app.run(debug=True, host='0.0.0.0')


    if join_thread:
        app.join()
    else:
        return app
    # Now use echo_client to get the resources.


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
