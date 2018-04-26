from .crypto import (
    generate_privkey,
    pubkey_to_addr,
    privkey_to_addr,
    addr_from_sig,
    pack,
    keccak256,
    keccak256_hex,
    sign,
    sign_transaction,
    eth_message_hash,
    eth_sign,
    eth_verify,
    eth_sign_typed_data_message,
    eth_sign_typed_data_message2,
    eth_sign_typed_data,
    eth_sign_typed_data_message_eip,
    eth_sign_typed_data_eip,
    get_balance_message,
    get_monitor_balance_message,
    get_monitor_balance_message2,
    sign_balance_proof,
    sign_monitor_balance_proof,
    sign_monitor_balance_proof2,
    verify_balance_proof,
    verify_monitor_balance_proof,
    verify_monitor_balance_proof2,
    sign_close,
    verify_closing_sig,
    get_receipt_message,
    sign_receipt,
    sign_receipt2,
    verify_receipt,
    verify_receipt2,
    sign_cond_payment,
    verify_cond_payment,
    get_state_hash,
    monitor_balance_message_from_state
)

from .contract import (
    create_signed_transaction,
    create_transaction,
    create_signed_contract_transaction,
    create_contract_transaction,
    create_local_contract_transaction,
    create_transaction_data,
    get_logs,
    get_event_blocking,
    wait_for_transaction
)

from .private_key import (
    check_permission_safety,
    get_private_key
)

from .misc import (
    get_function_kwargs,
    pop_function_kwargs
)

__all__ = [
    generate_privkey,
    pubkey_to_addr,
    privkey_to_addr,
    addr_from_sig,
    pack,
    keccak256,
    keccak256_hex,
    sign,
    sign_transaction,
    eth_message_hash,
    eth_sign,
    eth_verify,
    eth_sign_typed_data_message,
    eth_sign_typed_data_message2,
    eth_sign_typed_data,
    eth_sign_typed_data_message_eip,
    eth_sign_typed_data_eip,
    get_balance_message,
    sign_balance_proof,
    verify_balance_proof,
    sign_close,
    verify_closing_sig,

    create_signed_transaction,
    create_transaction,
    create_signed_contract_transaction,
    create_contract_transaction,
    create_transaction_data,
    get_logs,
    get_event_blocking,
    wait_for_transaction,

    check_permission_safety,
    get_private_key,

    get_function_kwargs,
    pop_function_kwargs,
]

def debug_print(i):
    for x in i:
        print(type(x),x)

import inspect, re

def varname(p):
  for line in inspect.getframeinfo(inspect.currentframe().f_back)[3]:
    m = re.search(r'\bvarname\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)', line)
    if m:
      return m.group(1)

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

