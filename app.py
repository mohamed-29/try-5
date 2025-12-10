"""
app.py
Main entry point. Maps WebSocket JSON -> VMC Protocol.
"""
import json
from flask import Flask
from flask_sock import Sock
from vmc_transport import VMCTransport
import vmc_protocol as p

SERIAL_PORT = "/dev/ttyS1"
BAUDRATE = 57600

app = Flask(__name__)
sock = Sock(app)
vmc = None
ws_clients = []

# --- Callbacks ---

def broadcast(msg):
    """Send JSON to all connected Websockets"""
    json_msg = json.dumps(msg)
    for ws in ws_clients[:]:
        try:
            ws.send(json_msg)
        except:
            ws_clients.remove(ws)

def on_vmc_log(msg):
    print(f"[VMC] {msg}")
    broadcast({"event": "log", "message": msg})

# app.py

def on_vmc_packet(cmd_id, payload):
    """
    Received DATA from VMC. 
    If we know the command, parse it detail.
    If we don't know it, use the generic decoder.
    """
    cmd_hex = f"0x{cmd_id:02X}"
    
    # 1. Try to find a specific decoder (e.g., for 0x11 or 0x04)
    if cmd_id in p.DECODERS:
        decoded = p.DECODERS[cmd_id](payload)
        # Add the command ID to the result for clarity
        decoded["cmd"] = cmd_hex
        broadcast(decoded)
        
    # 2. If no specific decoder exists, use the Catch-All
    else:
        # Use the generic decoder we just added to vmc_protocol.py
        generic_data = p.decode_generic(payload)
        generic_data["cmd"] = cmd_hex
        broadcast(generic_data)
# --- WebSocket Handler ---

@sock.route("/ws/vmc")
def vmc_ws(ws):
    global vmc
    ws_clients.append(ws)
    
    # Initialize Transport if not already running
    if vmc is None:
        try:
            vmc = VMCTransport(SERIAL_PORT, BAUDRATE, on_vmc_packet, on_vmc_log)
            vmc.start()
        except Exception as e:
            ws.send(json.dumps({"event": "error", "message": str(e)}))
            return

    while True:
        data = ws.receive()
        if not data: break
        
        try:
            req = json.loads(data)
            msg_type = req.get("type")
            
            # 1. Validate Command exists in Protocol
            if msg_type not in p.COMMAND_MAP:
                ws.send(json.dumps({"event": "error", "message": f"Unknown type: {msg_type}"}))
                continue

            # 2. Get ID and Encoder
            cmd_id = p.COMMAND_MAP[msg_type]
            encoder = p.ENCODERS.get(msg_type)
            
            # 3. Encode Payload (if encoder exists, else empty bytes)
            payload = encoder(req) if encoder else b""
            
            # 4. Send via Transport
            vmc.send_command(cmd_id, payload, f"WS Command: {msg_type}")
            
        except Exception as e:
            ws.send(json.dumps({"event": "error", "message": f"Processing error: {str(e)}"}))

    ws_clients.remove(ws)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
