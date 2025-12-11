"""
vmc_protocol.py
Definitions for Commands, Encoders (JSON->Bytes), and Decoders (Bytes->JSON).
Reference: VMC - Upper computer V3.0
"""
import struct

# --- Protocol Constants ---
STX1, STX2 = 0xFA, 0xFB
CMD_POLL = 0x41
CMD_ACK  = 0x42

# --- Command Registry ---
# Map readable names to Command IDs (Hex)
COMMAND_MAP = {
    "check_selection":  0x01, # [cite: 178]
    "buy":              0x03, # [cite: 187]
    "select_or_cancel": 0x05, # [cite: 201]
    "direct_vend":      0x06, # [cite: 205]
    "slot_info":        0x11, # [cite: 149]
    "set_price":        0x12, # [cite: 155]
    "set_inventory":    0x13, # [cite: 156]
    "add_money":        0x27, # [cite: 266]
    "get_slots":        0x31, # [cite: 236]
    "machine_status":   0x52, # [cite: 257]
    "deduct":           0x64, # [cite: 260]
}

# --- Encoders (PC -> VMC) ---
# Functions to turn Python variables into bytes for the payload

# vmc_protocol.py

# ... existing decoders ...


def decode_generic(payload):
    """
    Fallback decoder for ANY command not explicitly defined.
    Extracts the PackNO (first byte) and returns the rest as raw hex.
    Reference: PDF Page 4, Section 3 (PackNO+Text)
    """
    if len(payload) < 1:
        return {"error": "packet empty"}
    
    return {
        "event": "vmc_data_unknown", # Distinct event name for unhandled commands
        "pack_no": payload[0],       # Always the first byte
        "raw_data": payload[1:].hex().upper(), # The rest is the unknown data
        "raw_payload": payload.hex()
    }

def encode_buy(data):
    # [cite: 187] Selection number (2 byte)
    return int(data['selection']).to_bytes(2, 'big')

def encode_set_price(data):
    # [cite: 155] Selection (2 byte) + Price (4 byte)
    sel = int(data['selection']).to_bytes(2, 'big')
    price = int(data['price']).to_bytes(4, 'big')
    return sel + price

def encode_direct_vend(data):
    # [cite: 205] Drop(1) + Elev(1) + Sel(2) + Cart(1)
    return struct.pack(
        '>BBHB',
        1 if data.get('use_drop', True) else 0,
        1 if data.get('use_elevator', True) else 0,
        int(data['selection']),
        1 if data.get('cart', False) else 0
    )

def encode_deduct(data):
    # [cite: 264] Amount (4 byte)
    return int(data['amount']).to_bytes(4, 'big')

def encode_check_selection(data):
    # [cite: 178] Selection number (2 byte)
    return int(data['selection']).to_bytes(2, 'big')

def encode_set_inventory(data):
    # [cite: 156] Selection (2 byte) + Inventory (1 byte)
    sel = int(data['selection']).to_bytes(2, 'big')
    inv = int(data['inventory']).to_bytes(1, 'big')
    return sel + inv

def encode_select_or_cancel(data):
    # [cite: 201] Selection number (2 byte)
    return int(data['selection']).to_bytes(2, 'big')

def encode_add_money(data):
    # [cite: 266] Amount (4 byte)
    mode = int(1).to_bytes(1, 'big')
    amount = int(data['amount']).to_bytes(4, 'big')
    return mode + amount

# Dispatcher for encoders
ENCODERS = {
    "buy": encode_buy,
    "set_price": encode_set_price,
    "direct_vend": encode_direct_vend,
    "deduct": encode_deduct,
    "check_selection": encode_check_selection,
    "set_inventory": encode_set_inventory,
    "select_or_cancel": encode_select_or_cancel,
    "add_money": encode_add_money,
}

# --- Decoders (VMC -> PC) ---
# Functions to turn raw bytes into Python Dictionaries

def decode_slot_info(payload):
    # [cite: 149] PackNo(1)+Sel(2)+Price(4)+Inv(1)+Cap(1)+ID(2)+Stat(1)
    # Payload[0] is PackNO, so actual data starts at payload[1]
    if len(payload) < 12: return {"error": "packet too short"}
    
    unpacked = struct.unpack('>HIBBHB', payload[1:12])
    return {
        "event": "slot_info",
        "pack_no": payload[0],
        "selection": unpacked[0],
        "price": unpacked[1],
        "inventory": unpacked[2],
        "capacity": unpacked[3],
        "product_id": unpacked[4],
        "status": unpacked[5],
        "raw_payload": payload.hex()
    }

def decode_vend_status(payload):
    # [cite: 195] PackNo(1)+Status(1)+Sel(2)
    if len(payload) < 4: return {"error": "packet too short"}
    return {
        "event": "vend_status",
        "pack_no": payload[0],
        "status_code": payload[1],
        "selection": int.from_bytes(payload[2:4], 'big'),
        "raw_payload": payload.hex()
    }

def decode_machine_status(payload):
    # [cite: 257] Complex packet decoding
    if len(payload) < 25: return {"error": "packet too short"}
    return {
        "event": "machine_status",
        "pack_no": payload[0],
        "temperature": payload[5],
        "door_open": payload[6] == 1,
        "machine_id": payload[15:25].decode('ascii', errors='ignore'),
        "raw_payload": payload.hex()
    }

def decode_selection_status(payload):
    # [cite: 257] Complex packet decoding
    if len(payload) < 4: return {"error": "packet too short"}
    
    status_code = payload[1]
    status_message = {
        0x01: "Normal",
        0x02: "Out of stock",
        0x03: "Selection doesn’t exist",
        0x04: "Selection pause",
        0x05: "There is product inside elevator",
        0x06: "Delivery door unlocked",
        0x07: "Elevator error",
        0x08: "Elevator self-checking faulty",
    }.get(status_code, "Unknown status")

    return {
        "event": "selection_status",
        "pack_no": payload[0],
        "status_code": status_code,
        "status_message": status_message,
        "selection": int.from_bytes(payload[1:3], 'big'),
        "raw_payload": payload.hex()
    }

def decode_select_or_cancel(payload):
    # [cite: 201] Selection number (2 byte)
    if len(payload) < 2: return {"error": "packet too short"}
    return {
        "event": "select_or_cancel",
        "pack_no": payload[0],
        "selection": int.from_bytes(payload[1:3], 'big'),
        "raw_payload": payload.hex()
    }

def decode_receive_money(payload):
    # [cite: 261] Amount (4 byte)
    if len(payload) < 5: return {"error": "packet too short"}
    return {
        "event": "received_money",
        "pack_no": payload[0],
        "mode": payload[1],
        "amount": int.from_bytes(payload[2:6], 'big'),
        "raw_payload": payload.hex()
    }

def decode_current_amount(payload):
    # [cite: 263] Amount (4 byte)
    if len(payload) < 4: return {"error": "packet too short"}
    return {
        "event": "current_amount",
        "pack_no": payload[0],
        "amount": int.from_bytes(payload[1:5], 'big'),
        "raw_payload": payload.hex()
    }

# Dispatcher for decoders based on Command ID
DECODERS = {
    0x11: decode_slot_info,
    0x04: decode_vend_status,
    0x52: decode_machine_status,
    0x02: decode_selection_status,
    0x05: decode_select_or_cancel,
    0x21: decode_receive_money,
    0x23: decode_current_amount,
}

def build_frame(cmd_id, comm_no, payload_bytes):
    """Constructs the full packet with checksum [cite: 86]"""
    # [cite: 90] PackNO is the first byte of text
    full_payload = bytes([comm_no]) + payload_bytes
    length = len(full_payload)
    
    header = bytes([STX1, STX2, cmd_id, length])
    
    # [cite: 91] Checksum calculation
    xor_val = 0
    for b in header + full_payload:
        xor_val ^= b
        
    return header + full_payload + bytes([xor_val])