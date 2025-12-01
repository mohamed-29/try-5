"""
VMC <-> WebSocket bridge (upper computer is SLAVE)

Features:
- Upper computer never sends POLL. VMC is host, we are slave.
- Implements:
    * direct_vend (0x06)
    * buy / select_to_buy (0x03)
    * set_price (0x12)
    * get_slots  -> information sync (0x31), VMC will respond with 0x11 packets.
- Retries: send command up to 5 times if no ACK.
- Reduced logging:
    * NO spam for idle POLL/ACK.
    * WebSocket gets:
        - command_queued
        - command_sent  (TX to VMC)
        - vmc_ack       (ACK from VMC)
        - vmc_data      (generic data from VMC)
        - slot_info     (decoded 0x11: selection price/inventory/capacity/product_id/status)
        - vend_status   (decoded 0x04: dispensing status)
        - command_finished / command_timeout
"""

import json
import threading
import time

import serial
from flask import Flask, send_from_directory
from flask_sock import Sock

# -------------------- CONFIG --------------------

SERIAL_PORT = "/dev/ttyS1"   # e.g. "COM3" on Windows
BAUDRATE    = 57600
SER_TIMEOUT = 0.05

STX1 = 0xFA
STX2 = 0xFB

CMD_POLL       = 0x41
CMD_ACK        = 0x42

CMD_SELECT_BUY = 0x03
CMD_DIRECT_VEND = 0x06
CMD_SET_PRICE  = 0x12

CMD_INFO_SYNC  = 0x31   # Information synchronization (slots info)
CMD_SLOT_INFO  = 0x11   # VMC reports selection price/inventory/capacity/product ID
CMD_VEND_STATUS = 0x04  # VMC dispensing status


# -------------------- HELPERS --------------------

def to_hex(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


def build_packet(cmd: int, payload: bytes = b"") -> bytes:
    """
    Generic frame builder: FA FB CMD LEN [PAYLOAD] XOR
    XOR over all bytes except XOR itself.
    """
    header = bytes([STX1, STX2, cmd, len(payload)])
    xor_ = 0
    for x in header + payload:
        xor_ ^= x
    return header + payload + bytes([xor_])


# -------------------- VMC CONNECTION --------------------

class VMCConnection:
    """
    One WebSocket client talking to one VMC (we are SLAVE).

    States:
        idle          : no command in progress
        waiting_ack   : command queued, waiting for VMC ACK
        waiting_data  : command ACKed, now receiving DATA until next POLL
    """

    def __init__(self, ser: serial.Serial, ws):
        self.ser = ser
        self.ws = ws
        self.running = True

        self.buffer = bytearray()

        # protects command state
        self.state_lock = threading.Lock()
        self.state = "idle"      # "idle" | "waiting_ack" | "waiting_data"
        self.current_cmd = None  # dict or None
        self.next_comm = 1

        # protects WebSocket send (VERY IMPORTANT)
        self.ws_lock = threading.Lock()

    # ------------- helpers -------------

    def _send_ws(self, obj: dict):
        """Thread-safe send to WebSocket."""
        if not self.running:
            return
        try:
            data = json.dumps(obj)
        except Exception as e:
            print("JSON encode error:", e, obj)
            return

        with self.ws_lock:
            if not self.running:
                return
            try:
                self.ws.send(data)
            except Exception as e:
                print("WebSocket send failed:", e)
                self.running = False

    def _log(self, msg: str):
        """Lightweight log to console + optional WS."""
        print("[VMC]", msg)
        # If you want *no* logs on WS, comment the next line:
        self._send_ws({"event": "log", "message": msg})

    def _next_comm(self) -> int:
        """
        Return next communication number 1..255.
        Caller must hold state_lock if needed.
        """
        n = self.next_comm
        self.next_comm += 1
        if self.next_comm > 255:
            self.next_comm = 1
        return n

    # ------------- from WebSocket -------------

    def handle_ws_message(self, text: str):
        """Handle JSON from Android/Postman."""
        self._log(f"WS message received: {text!r}")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            self._send_ws({"event": "error", "error": f"invalid JSON: {e}"})
            return

        t = data.get("type")
        if not t:
            self._send_ws({"event": "error", "error": "missing 'type'"})
            return

        try:
            if t == "direct_vend":
                self._ws_direct_vend(data)
            elif t == "buy":
                self._ws_buy(data)
            elif t == "set_price":
                self._ws_set_price(data)
            elif t == "get_slots":
                self._ws_get_slots(data)
            else:
                self._send_ws({"event": "error", "error": f"unknown type '{t}'"})
        except Exception as e:
            self._send_ws({"event": "error", "error": f"exception: {e}"})

    def _queue_command(self, cmd: int, text_bytes: bytes, desc: str):
        """
        Register a new command and wait for next POLL to send it.
        text_bytes: TEXT part WITHOUT CommNo; we insert CommNo as first byte.
        """
        self._log(f"Trying to queue command: {desc}")

        with self.state_lock:
            if self.state != "idle" and self.current_cmd and not self.current_cmd.get("done", False):
                raise RuntimeError("another command is already in progress")

            comm_no = self._next_comm()
            self.current_cmd = {
                "cmd": cmd,
                "text": text_bytes,
                "comm": comm_no,
                "retries": 0,
                "acked": False,
                "done": False,
                "desc": desc,
            }
            self.state = "waiting_ack"

        self._log(f"Command queued: {desc}, cmd=0x{cmd:02X}, comm={comm_no}")
        self._send_ws({
            "event": "command_queued",
            "cmd": f"0x{cmd:02X}",
            "comm_no": comm_no,
            "description": desc,
        })

    # ---- JSON → specific commands ----

    def _ws_direct_vend(self, data: dict):
        selection = int(data["selection"])
        use_drop  = 1 if data.get("use_drop_sensor", True) else 0
        use_elev  = 1 if data.get("use_elevator", True) else 0
        cart      = 1 if data.get("shopping_cart", False) else 0

        # drop(1) + elevator(1) + selection(2) + cart(1)
        text = bytes([use_drop, use_elev]) \
               + selection.to_bytes(2, "big") \
               + bytes([cart])

        self._queue_command(
            CMD_DIRECT_VEND,
            text,
            f"direct_vend selection={selection}"
        )

    def _ws_buy(self, data: dict):
        selection = int(data["selection"])
        text = selection.to_bytes(2, "big")

        self._queue_command(
            CMD_SELECT_BUY,
            text,
            f"buy selection={selection}"
        )

    def _ws_set_price(self, data: dict):
        selection = int(data["selection"])
        price     = int(data["price"])

        text = selection.to_bytes(2, "big") + price.to_bytes(4, "big")

        self._queue_command(
            CMD_SET_PRICE,
            text,
            f"set_price selection={selection} price={price}"
        )

    def _ws_get_slots(self, data: dict):
        """
        Request information synchronization (0x31).
        After this, VMC will send 0x11 packets containing:
        selection, price, inventory, capacity, product ID, status.
        """
        self._queue_command(
            CMD_INFO_SYNC,
            b"",                     # only CommNo in payload
            "get_slots (info sync 0x31)"
        )

    # ------------- serial side -------------

    def serial_loop(self):
        self._log("Serial loop started")
        while self.running:
            try:
                chunk = self.ser.read(64)
            except Exception as e:
                self._log(f"Serial read error: {e}")
                time.sleep(1)
                continue

            if chunk:
                self.buffer.extend(chunk)
                self._process_buffer()
            else:
                time.sleep(0.01)
        self._log("Serial loop stopped")

    def _process_buffer(self):
        while True:
            if len(self.buffer) < 5:
                return

            try:
                start = self.buffer.index(bytes([STX1, STX2]))
            except ValueError:
                self.buffer.clear()
                return

            if start > 0:
                del self.buffer[:start]

            if len(self.buffer) < 5:
                return

            cmd = self.buffer[2]
            length = self.buffer[3]
            frame_len = 2 + 1 + 1 + length + 1
            if len(self.buffer) < frame_len:
                return

            frame = bytes(self.buffer[:frame_len])
            del self.buffer[:frame_len]

            xor_calc = 0
            for b in frame[:-1]:
                xor_calc ^= b
            if xor_calc != frame[-1]:
                self._log(f"Bad XOR, dropping frame: {to_hex(frame)}")
                continue

            payload = frame[4:-1]
            self._handle_packet(cmd, payload, frame)

    def _handle_packet(self, cmd: int, payload: bytes, raw: bytes):
        """
        Decide what to do with a packet from VMC.
        We ignore POLL in logs to avoid spam; we log only commands/data.
        """
        if cmd == CMD_POLL:
            self._handle_poll()
        elif cmd == CMD_ACK:
            self._handle_ack_from_vmc(raw)
        else:
            self._handle_data_from_vmc(cmd, payload, raw)

    # --- reactions to packets ---

    def _send_ack(self, reason: str = "", log: bool = True):
        """Send ACK to VMC. You can turn off logs for idle POLL acks."""
        pkt = build_packet(CMD_ACK, b"")
        try:
            self.ser.write(pkt)
        except Exception as e:
            self._log(f"Serial write error (ACK): {e}")
            return

        if log:
            msg = f"Sent ACK: {to_hex(pkt)}"
            if reason:
                msg += f" ({reason})"
            self._log(msg)

    def _send_current_command(self):
        """Actually send the current command to VMC."""
        with self.state_lock:
            c = self.current_cmd
            if not c:
                return
            payload = bytes([c["comm"]]) + c["text"]
            frame = build_packet(c["cmd"], payload)
            c["retries"] += 1

        try:
            self.ser.write(frame)
        except Exception as e:
            self._log(f"Serial write error (CMD): {e}")
            return

        self._log(
            f"TX CMD 0x{c['cmd']:02X}, comm={c['comm']}, "
            f"retry={c['retries']}: {to_hex(frame)}"
        )
        self._send_ws({
            "event": "command_sent",
            "direction": "TX",
            "cmd": f"0x{c['cmd']:02X}",
            "comm_no": c["comm"],
            "retries": c["retries"],
            "raw": to_hex(frame),
            "description": c["desc"],
        })

    def _handle_poll(self):
        """
        VMC asks us every ~200ms:
        - if no command -> just ACK (no logs)
        - if waiting_ack -> send command (max 5 retries)
        - if waiting_data -> next POLL means command finished
        """
        with self.state_lock:
            state = self.state
            c = self.current_cmd

        if state == "idle" or not c:
            # no command to send; keep completely quiet on WS
            self._send_ack(log=False)
            return

        if state == "waiting_ack":
            if c["retries"] < 5:
                self._send_current_command()
            else:
                # timeout
                self._send_ack("command timeout, no ACK", log=True)
                with self.state_lock:
                    c["done"] = True
                    self.state = "idle"
                    self.current_cmd = None
                self._send_ws({
                    "event": "command_timeout",
                    "cmd": f"0x{c['cmd']:02X}",
                    "comm_no": c["comm"],
                    "description": c["desc"],
                })

        elif state == "waiting_data":
            # POLL after ACK & data => command finished
            self._send_ack("command finished", log=True)
            with self.state_lock:
                c["done"] = True
                self.state = "idle"
                self.current_cmd = None
            self._send_ws({
                "event": "command_finished",
                "cmd": f"0x{c['cmd']:02X}",
                "comm_no": c["comm"],
                "description": c["desc"],
            })

    def _handle_ack_from_vmc(self, raw: bytes):
        with self.state_lock:
            c = self.current_cmd
            state = self.state

        self._send_ws({
            "event": "vmc_ack",
            "cmd": "0x42",
            "raw": to_hex(raw),
        })

        if c and state == "waiting_ack":
            self._log(f"ACK from VMC for cmd=0x{c['cmd']:02X}, comm={c['comm']}")
            with self.state_lock:
                c["acked"] = True
                self.state = "waiting_data"
        else:
            self._log("ACK from VMC but no command waiting_ack")

    def _handle_data_from_vmc(self, cmd: int, payload: bytes, raw: bytes):
        """
        Any VMC packet that is not POLL/ACK.
        We always ACK it and forward to WebSocket with details.
        """
        # ACK back (but don't spam WS with this ACK)
        self._send_ack("data packet", log=False)

        base = {
            "cmd": f"0x{cmd:02X}",
            "raw": to_hex(raw),
        }

        # If we know the structure, decode nicely
        if cmd == CMD_SLOT_INFO and len(payload) >= 12:
            # payload = [PackNO][sel_hi][sel_lo][price(4)][inv][cap][prod_hi][prod_lo][status]
            p = payload
            pack_no     = p[0]
            selection   = int.from_bytes(p[1:3], "big")
            price       = int.from_bytes(p[3:7], "big")
            inventory   = p[7]
            capacity    = p[8]
            product_id  = int.from_bytes(p[9:11], "big")
            status      = p[11]

            event = {
                "event": "slot_info",
                "pack_no": pack_no,
                "selection": selection,
                "price": price,
                "inventory": inventory,
                "capacity": capacity,
                "product_id": product_id,
                "status": status,
            }
            event.update(base)
            self._send_ws(event)
            return

        if cmd == CMD_VEND_STATUS and len(payload) >= 4:
            # common 0x04 form: [PackNO][status][sel_hi][sel_lo][(optional microwave)]
            p = payload
            pack_no   = p[0]
            status    = p[1]
            selection = int.from_bytes(p[2:4], "big")

            event = {
                "event": "vend_status",
                "pack_no": pack_no,
                "selection": selection,
                "status": status,
            }
            event.update(base)
            self._send_ws(event)
            return

        # Generic fallback for any other command we don't decode yet
        event = {
            "event": "vmc_data",
        }
        if payload:
            pack_no = payload[0]
            text = payload[1:]
            event["pack_no"] = pack_no
            event["data_hex"] = to_hex(text)
        event.update(base)
        self._send_ws(event)

    # ------------- cleanup -------------

    def stop(self):
        self.running = False
        try:
            self.ser.close()
        except Exception:
            pass


# ------------- Flask + WS -------------

app = Flask(__name__, static_folder="./")
sock = Sock(app)
@app.route("/")
def home():
    return send_from_directory(".", "page.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(".", "manifest.json")
@sock.route("/ws/vmc")
def vmc_ws(ws):
    try:
        ser = serial.Serial(
            SERIAL_PORT,
            BAUDRATE,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=SER_TIMEOUT,
        )
    except Exception as e:
        ws.send(json.dumps({
            "event": "error",
            "error": f"Cannot open serial port {SERIAL_PORT}: {e}",
        }))
        return

    vmc = VMCConnection(ser, ws)
    t = threading.Thread(target=vmc.serial_loop, daemon=True)
    t.start()

    ws.send(json.dumps({
        "event": "connected",
        "serial_port": SERIAL_PORT,
        "baudrate": BAUDRATE,
        "role": "slave (upper computer)"
    }))

    while True:
        try:
            msg = ws.receive()
        except Exception as e:
            print("WS receive error:", e)
            break
        if msg is None:
            print("WS closed by client")
            break

        print("WS RAW MESSAGE:", repr(msg))
        vmc.handle_ws_message(msg)

    vmc.stop()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
