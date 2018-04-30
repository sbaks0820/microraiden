# µRaiden + PISA

µRaiden is an off-chain, cheap, scalable and low-latency micropayment solution.

[µRaiden documentation](http://microraiden.readthedocs.io/en/latest/)

Pisa is a privacy-preserving third party monitoring protocol to protect channel participants from the counterparty.

## Brief Overview

µRaiden is not part of the [Raiden Network](https://github.com/raiden-network/raiden). However, it was built using the same state channel idea and implements it in a less general fashion focusing on the concrete application of micropayments for paywalled content.

The main differences between the Raiden Network and µRaiden are:
 * µRaiden is a many-to-one unidirectional state channel protocol, while the Raiden Network is a many-to-many bidirectional solution and implies a more complex design of channel networks. This allows the Raiden Network to efficiently send transfers without being forced to pay for opening new channels with people who are already in the network.
 * µRaiden off-chain transactions do not cost anything, as they are only exchanged between sender and receiver. The Raiden Network has a more complicated incentive-based off-chain transport of transaction information, from one user to another (following the channel network path used to connect the sender and the receiver).


### Tokens and Channel Manager Contract

µRaiden uses its own token for payments which is both [ERC20](https://github.com/ethereum/EIPs/issues/20) and [ERC223](https://github.com/ethereum/EIPs/issues/223) compliant.

In a nutshell, clients (subsequently called "senders") wanting to access a provider's payable resources, will [open a micropayment channel](http://microraiden.readthedocs.io/en/latest/contract/index.html#opening-a-transfer-channel) with the provider ("receiver") and fund the channel with a number of tokens. These escrowed tokens will be kept by a third party contract that manages opening and closing of channels.
a
### Monitor Contract
Monitors using PISA deploy a StateGuardian contract that implements a uni-directional payment channel and functionality to allow customers to outsource the most recent state of their channel to the monitor. The contract also enables customers to ensure a fair exchange of payment to the monitor in return for a signed receipt of appointment. The receipt can be submitted and used to forfeit the monitor's deposit in case of incorrect behavior.

### Demos:
1. The monitor does not correctly complete the fair exchange for a receipt:
[![Alt text](https://img.youtube.com/vi/xklyocwoh7Y/0.jpg)](https://youtu.be/xklyocwoh7Y)
2. Monitor doesn't reveal pre-image, but still redeems payment. Receipt is VALID.
[![Alt text](https://img.youtube.com/vi/Tijbh8vA-yQ/0.jpg)](https://www.youtube.com/watch?v=Tijbh8vA-yQ)
