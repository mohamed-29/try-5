"""
vmc_transport.py
Handles Serial I/O, POLL/ACK handshake, and threading.
"""
import serial
import threading
import time
import vmc_protocol as p

class VMCTransport:
    def __init__(self, port, baudrate, on_packet_received, on_log):
        self.ser = serial.Serial(port, baudrate, timeout=0.05)
        self.on_packet = on_packet_received # Callback for valid data
        self.on_log = on_log                # Callback for logging
        self.running = True
        
        # State
        self.lock = threading.Lock()
        self.state = "idle" # idle, waiting_ack, waiting_data
        self.next_comm_no = 1
        
        # Current outgoing command
        self.pending_cmd = None # {cmd_id, payload, retries, desc}

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False
        if self.ser.is_open: self.ser.close()

    def send_command(self, cmd_id, payload_bytes, description):
        """Queue a command to be sent on next POLL"""
        with self.lock:
            # Simple flow: only one command at a time
            if self.state != "idle": 
                raise RuntimeError("Busy: Command in progress")
            
            comm = self.next_comm_no
            self.next_comm_no = (self.next_comm_no + 1) if self.next_comm_no < 255 else 1
            
            self.pending_cmd = {
                "cmd_id": cmd_id,
                "comm_no": comm,
                "payload": payload_bytes,
                "retries": 0,
                "desc": description
            }
            self.state = "waiting_ack"
            self.on_log(f"Queued: {description} (Comm: {comm})")

    def _loop(self):
        buffer = bytearray()
        while self.running:
            try:
                if self.ser.in_waiting:
                    buffer.extend(self.ser.read(self.ser.in_waiting))
                    self._process_buffer(buffer)
                time.sleep(0.01)
            except Exception as e:
                self.on_log(f"Serial Error: {e}")
                time.sleep(1)

    def _process_buffer(self, buffer):
        while len(buffer) >= 5: # Min packet size
            # [cite_start]Find Start of Frame [cite: 87]
            if buffer[0] != p.STX1 or buffer[1] != p.STX2:
                buffer.pop(0)
                continue
                
            cmd = buffer[2]
            length = buffer[3]
            frame_len = 5 + length # STX(2)+CMD(1)+LEN(1)+XOR(1) + payload
            
            if len(buffer) < frame_len:
                break # Wait for more data

            frame = buffer[:frame_len]
            payload = frame[4:-1]
            received_xor = frame[-1]
            del buffer[:frame_len]

            # [cite_start]Verify Checksum [cite: 91]
            calc_xor = 0
            for b in frame[:-1]: calc_xor ^= b
            
            if calc_xor == received_xor:
                self._handle_valid_packet(cmd, payload)
            else:
                self.on_log(f"Checksum Error on cmd {cmd:02X}")

    def _handle_valid_packet(self, cmd, payload):
        # [cite_start]1. Handle POLL (Heartbeat) [cite: 70]
        if cmd == p.CMD_POLL:
            with self.lock:
                if self.state == "waiting_ack" and self.pending_cmd:
                    # [cite_start]We have a command waiting, send it now [cite: 74]
                    self._transmit_pending()
                elif self.state == "waiting_data" and self.pending_cmd:
                    # We were waiting for data, but got POLL. Means transaction finished.
                    self.on_log(f"Finished: {self.pending_cmd['desc']}")
                    self.state = "idle"
                    self.pending_cmd = None
                    self._send_ack()
                else:
                    # [cite_start]Nothing to do, just ACK [cite: 75]
                    self._send_ack()
        
        # [cite_start]2. Handle ACK (Response to our command) [cite: 76]
        elif cmd == p.CMD_ACK:
            with self.lock:
                if self.state == "waiting_ack":
                    self.state = "waiting_data" # Now wait for the data response
                    self.on_log("ACK received from VMC")

        # 3. Handle DATA (Info from VMC)
        else:
            self._send_ack() # Always ACK data
            self.on_packet(cmd, payload)

    def _transmit_pending(self):
        c = self.pending_cmd
        if c['retries'] >= 5:
            self.on_log(f"Timeout: {c['desc']}")
            self.state = "idle"
            self.pending_cmd = None
            self._send_ack()
            return

        packet = p.build_frame(c['cmd_id'], c['comm_no'], c['payload'])
        self.ser.write(packet)
        c['retries'] += 1

    def _send_ack(self):
        # ACK Packet
        ack = bytes([p.STX1, p.STX2, p.CMD_ACK, 0x00, 0x43]) # 0x43 is XOR of FA+FB+42+00
        self.ser.write(ack)