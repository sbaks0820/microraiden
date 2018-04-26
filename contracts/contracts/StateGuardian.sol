pragma solidity ^0.4.17;

import './Token.sol';
import './lib/ECVerify.sol';
import './RaidenMicroTransferChannels.sol';

contract StateGuardian {

    event CustomerDeposit(
        address indexed _sender_address,
        uint indexed _sender_deposit
    );

    event Dispute(
        address indexed _customer_address,
        uint32 indexed _open_block_number,
        address indexed _sender_address,
        uint32 _channel_settle
    );

    event Resolve(
        address indexed _sender_address
    );

    event Evidence(
        address indexed _customer_address,
        address indexed _sender_address,
        uint32 indexed _pre_image,
        uint32 _open_block_number
    );

    event Close(
        uint32 _t_withdraw
    );

    event Withdraw();

    enum Flags { OK, DISPUTE, CLOSED, CHEATED }

    struct channel {
        uint deposit;
        uint32 t_settle;
        Flags flag;
        uint payout;
        RaidenMicroTransferChannels caddr;
    }
   
    Flags public flag;
    uint public profit;
    uint32 public delta_settle;
    uint32 public delta_withdraw;
    uint32 t_withdraw;
    uint public num_customers;
    uint public guardian_deposit;
    address public monitor = msg.sender;

    mapping (address => channel) public ID;
//    mapping (address => Channel[]) public ID;

    function StateGuardian(uint32 _delta_withdraw, uint32 _delta_settle)
        public
        payable
    {
        guardian_deposit = msg.value;
        delta_settle = _delta_settle;
        delta_withdraw = _delta_withdraw;
        t_withdraw = 0;
        flag = Flags.OK;
    }

    function setup(uint32 _delta_withdraw, uint32 _delta_settle)
        payable 
        external
    {
        require(msg.sender == monitor);

        guardian_deposit = msg.value;
        delta_settle = _delta_settle;
        delta_withdraw = _delta_withdraw;
        t_withdraw = 0;
        flag = Flags.OK;
    }

    function deposit(address caddr)
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
        ID[msg.sender].caddr = RaidenMicroTransferChannels(caddr);

        num_customers += 1;
        
        CustomerDeposit(msg.sender, msg.value); 
    }
    
    function extract_state_signature(
        address _sender,
        uint32 _open_block_number,
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
                'address sender',
                'uint32 open block number',
                'uint32 payout',
                'bool cond_transfer',
                'bytes32 hash'
            ),
            keccak256(
                _sender,
                _open_block_number,
                _payout,
                _cond_transfer,
                _hash
            )
        );

        address signer = ECVerify.ecverify(message_hash, _signature);
        return signer; 
    }

    //event DebugHash(uint32 indexed _pre_image, bytes32 indexed _image, bytes32 indexed _hash);
    //event DebugSigner(address _signer);
    //event PayoutInfo(uint _payout, uint _deposit);
    function setstate(
        address _sender,
        uint32 _open_block_number,
        uint32 _payout,
        bool _cond_transfer,
        bytes32 _hash,
        uint32 _pre_image,
        bytes _customer_sig,
        address _customer)
        view
        external
    {
        require(msg.sender == monitor);
        require(flag != Flags.CHEATED);
        require(_payout != ID[_customer].payout);
        require(_payout <= ID[_customer].deposit);
        require(keccak256(_pre_image) == _hash);

        address customer_signer = extract_state_signature(
            _sender,
            _open_block_number,
            _payout,
            _cond_transfer,
            _hash,
            _customer_sig
        );

        //DebugSigner(customer_signer);
        require(customer_signer == _customer);

        profit += _payout;

        _customer.transfer(ID[_customer].deposit - _payout);

        ID[_customer].deposit = 0;
        ID[_customer].t_settle = 0;
        ID[_customer].flag = Flags.CLOSED;
        ID[_customer].payout = 0;

        num_customers -= 1;

        Evidence(_customer, _sender, _pre_image, _open_block_number);
    }      


    function triggerdispute(address _sender, uint32 _open_block_number)
        external
    {
        if (ID[msg.sender].flag == Flags.OK) {
            ID[msg.sender].flag = Flags.DISPUTE;
            ID[msg.sender].t_settle = uint32(block.number + delta_settle);
            Dispute(msg.sender, _open_block_number, _sender, ID[msg.sender].t_settle);
        }
    }

    function resolve()
        external
    {
        if (flag == Flags.CHEATED ||
           (ID[msg.sender].t_settle < block.number && 
            ID[msg.sender].flag == Flags.DISPUTE))
        {
            msg.sender.transfer(ID[msg.sender].deposit);
            ID[msg.sender].t_settle = 0;
            ID[msg.sender].deposit = 0;
            ID[msg.sender].flag = Flags.CLOSED;
            ID[msg.sender].payout = 0;
            num_customers -= 1;
            Resolve(msg.sender);
        }
    }

    function stopmonitoring()
        external
    {
        require(msg.sender == monitor);
        flag = Flags.CLOSED;
        t_withdraw = uint32(block.number + delta_withdraw);
        Close(t_withdraw);
    }

    function withdraw()
        external
    {
        require(flag != Flags.CHEATED);
        require(msg.sender == monitor);
        
        if (num_customers == 0 && 
            block.number > t_withdraw &&
            flag == Flags.CLOSED)
        {
            profit += guardian_deposit;
            guardian_deposit = 0;
            msg.sender.transfer(profit);
            profit = 0;

            Withdraw();
        }
    }


    function extractreceiptsignature(
        address receiver,
        address sender,
        uint32 open_block_number,
        uint32 t_start,
        uint32 t_expire,
        bytes32 image,
        bytes32 balance_message_hash,
        bytes monitor_sig)
        returns(address)
    {

        bytes32 receipt_hash = keccak256(
            keccak256(
                'address customer',
                'address sender',
                'uint32 block created',
                'uint32 start time',
                'uint32 expire time',
                'bytes32 image',
                'bytes32 evidence'
            ),
            keccak256(
                msg.sender,
                sender,
                open_block_number,
                t_start,
                t_expire,
                image,
                balance_message_hash
            )
        );
   
        address signer = ECVerify.ecverify(receipt_hash, monitor_sig);
        return signer;
    }

//    event StealMonitorDeposit(uint192 indexed _monitor_balance, uint192 indexed _closing_balance);
//    event LeaveMonitorDeposit(uint192 indexed _monitor_balance, uint192 indexed _closing_balance);
//    event DebugSigner(address _signer, bytes32 _image);
//    event ClosingInfo(uint192 indexed _closing_balance, uint192 indexed _monitor_balance, uint32 indexed _settle_block);
    //event DebugHash(bytes32 indexed _image, bytes32 indexed _hash);
    event RecourseResult(bytes32 indexed _evidence, bytes32 _receipt_hash, bool indexed _cheated);

    function recourse(
        address sender,
        uint32 open_block_number,
        bytes32 image,
        uint32 t_start,
        uint32 t_expire,
        bytes32 balance_message_hash,
        bytes monitor_sig,
        uint32 pre_image)
        external
    {        
        require(flag != Flags.CHEATED);
        require(keccak256(pre_image) == image);
//        DebugHash(image, keccak256(pre_image));

        address signer = extractreceiptsignature(
            msg.sender,
            sender,
            open_block_number,
            t_start,
            t_expire,
            image,
            balance_message_hash,
            monitor_sig
        );

        require(signer == monitor);

//        uint192 closing_balance;
//        uint192 monitor_balance;
        uint32 block_number;
        bytes32 evidence;
        //bool cheated = false;

//        (closing_balance, monitor_balance, block_number) = ID[msg.sender].caddr.getClosingInfo(sender, msg.sender, open_block_number);
        (block_number, evidence) = ID[msg.sender].caddr.getClosingInfo(sender, msg.sender, open_block_number);


        //ClosingInfo(closing_balance, monitor_balance, block_number);

        if (
            open_block_number <= t_start &&
            block_number < t_expire &&
            evidence != balance_message_hash)
            //closing_balance > monitor_balance)
        {
            flag = Flags.CHEATED;
            //cheated = true;
            RecourseResult(evidence, balance_message_hash, true); 
        } else {
            RecourseResult(evidence, balance_message_hash, false);
        }
    } 


    function balance(address customer)
        external
        returns(uint)
    {
        return ID[customer].deposit;
    }

}
