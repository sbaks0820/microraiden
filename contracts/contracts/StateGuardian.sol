pragma solidity ^0.4.17;

import './Token.sol';
import './lib/ECVerify.sol';
//import './RaidenMicroTransferChannels.sol';

contract StateGuardian {

    event CustomerDeposit(
        address indexed _sender_address,
        uint indexed _sender_deposit
    );

    event Dispute(
        address indexed _sender_address,
        address indexed _channel_settle
    );

    event Resolve(
        address indexed _sender_address
    );

    event Evidence(
        address indexed _sender_address,
        uint indexed _payout,
        bytes32 indexed _hash
    );

    enum Flags { OK, DISPUTE, CLOSED, CHEATED }

    struct channel {
        uint deposit;
        uint t_settle;
        Flags flag;
        uint payout;
    }
   
    Flags flag;
    uint public profit;
    uint32 public delta_settle;
    uint32 public delta_withdraw;
    uint public num_customers;
    uint public guardian_deposit;
    address public monitor = msg.sender;

    mapping (address => channel) ID;

    function setup(uint32 _delta_withdraw, uint32 _delta_settle)
        payable 
        external
    {
        require(msg.sender == monitor);

        guardian_deposit = msg.value;
        delta_settle = _delta_settle;
        delta_withdraw = _delta_withdraw;
        flag = Flags.OK;
    }

    function deposit()
        payable
        external
    {
        require(msg.value > 0);
        require(flag == Flags.OK);
        require(ID[msg.sender].flag != Flags.DISPUTE);

        if (ID[msg.sender].flag == Flags.CLOSED) {
            num_customers += 1;
            ID[msg.sender].flag = Flags.OK;
        }
        ID[msg.sender].deposit += msg.value;
        
        CustomerDeposit(msg.sender, msg.value); 
    }
    
    function extract_state_signature(
        uint32 _payout,
        bool _cond_transfer,
        bytes32 _hash,
        bytes _signature)
        internal
        view
        returns (address)
    {
        bytes32 message_hash = keccak256(
            keccak256(
                'uint32 payout',
                'bool cond_transfer',
                'bytes32 hash'
            ),
            keccak256(
                _payout,
                _cond_transfer,
                _hash
            )
        );

        address signer = ECVerify.ecverify(message_hash, _signature);
        return signer; 
    }
 
    function setState(
        uint32 _payout,
        bool _cond_transfer,
        bytes32 _hash,
        bytes _pre_image,
        bytes _customer_sig,
        bytes _monitor_sig,
        address _customer)
        view
        external
    {
        require(msg.sender == monitor);
        require(flag != Flags.CHEATED);
        require(_payout != ID[_customer].payout);
        require(_payout <= ID[_customer].deposit);

        if (_cond_transfer) {
           require(keccak256(_pre_image) == _hash);
        }

        address customer_signer = extract_state_signature(
            _payout,
            _cond_transfer,
            _hash,
            _customer_sig
        );

        require(customer_signer == _customer);
     
    }      


    function withdraw()
        external
    {
        msg.sender.transfer(ID[msg.sender].deposit);
    }

    function balance(address customer)
        external
        returns(uint)
    {
        return ID[customer].deposit;
    }

}
