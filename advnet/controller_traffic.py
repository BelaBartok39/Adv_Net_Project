"""Paced UDP traffic with receiver measurements while the flows are running."""
from dataclasses import dataclass, field
from errno import ECONNREFUSED, EHOSTDOWN, EHOSTUNREACH, ENETDOWN, ENETUNREACH, ENOBUFS
from itertools import groupby
from select import select
import socket
from struct import Struct
from threading import Event, Lock, Thread
import time
from uuid import uuid4

from .controller_execution import fingerprint
from .dynamic_scheduler import FlowExit


DATAGRAM_BYTES = 1200
HEADER = Struct('!16sQQ')
PAYLOAD = bytes(DATAGRAM_BYTES - HEADER.size)
MBITS_PER_PACKET = DATAGRAM_BYTES * 8 / 1e6
RECEIVER_GRACE_SECONDS = 0.25


def flow_profile(flow, rate):
    return {'source': flow.source, 'destination': flow.destination,
            'destination_port': flow.destination_port, 'offered_mbps': rate}


def demand_id(profile):
    return fingerprint(sorted(profile, key=lambda row: (row['source'], row['destination'],
                                                        row['destination_port'])))


def reference_profiles(schedule):
    """One reference for each demand composition between scheduled events."""
    profiles = {demand_id([]): []}
    if schedule is not None:
        active = {}
        flows = {flow.flow_id: flow for flow in schedule.flows}
        for _, events in groupby(schedule.events, key=lambda event: event.at_seconds):
            for event in events:
                if event.action == 'stop':
                    del active[event.flow_id]
                else:
                    active[event.flow_id] = (flows[event.flow_id], event.offered_mbps)
            profile = list(active.values())
            profiles[demand_id([flow_profile(*entry) for entry in profile])] = profile
    return profiles


@dataclass
class FlowCapture:
    index: int
    start: float
    end: float
    first: int | None = None
    sent: int = 0
    received: set = field(default_factory=set)

    def result(self, seconds, offered_mbps):
        actual_mbps = self.sent * MBITS_PER_PACKET / seconds
        if not self.sent or not 0.9 * offered_mbps <= actual_mbps <= 1.1 * offered_mbps:
            raise ValueError('UDP sender did not deliver the planned offered rate')
        received = sum(self.first <= sequence < self.first + self.sent
                       for sequence in self.received.copy())
        return {'seconds': seconds, 'sent_packets': self.sent, 'received_packets': received,
                'actual_offered_mbps': actual_mbps,
                'received_mbps': received * MBITS_PER_PACKET / seconds,
                'loss_fraction': (self.sent - received) / self.sent}


class UdpStream:
    """A connectionless sender and receiver remain available across routing faults."""
    def __init__(self, flow, rate, lab, socket_at):
        source, destination = (lab.routers[node] for node in (flow.source, flow.destination))
        self.source = source['host_ip']
        self.destination = (destination['host_ip'], flow.destination_port)
        self.flow, self.rate = flow, rate
        self.nonce = uuid4().bytes
        self.capture = None
        self.stop = Event()
        self.error = None
        self.rx = socket_at(destination['host'])
        try:
            self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            self.rx.bind(self.destination)
            self.rx.setblocking(False)
            self.tx = socket_at(source['host'])
            try:
                self.tx.bind((self.source, 0))
                self.tx.setblocking(False)
                self.peer = self.tx.getsockname()
                self.thread = Thread(target=self.run, name='udp-' + flow.flow_id, daemon=True)
                self.thread.start()
            except BaseException:
                self.tx.close()
                raise
        except BaseException:
            self.rx.close()
            raise

    def run(self):
        sequence, credit = 0, 0.0
        last = time.monotonic()
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                rate = self.rate / MBITS_PER_PACKET
                credit = min(credit + (now - last) * rate, max(1.0, rate * 0.01))
                last = now
                for _ in range(int(credit)):
                    capture = self.capture
                    marked = capture is not None and capture.start <= time.monotonic() < capture.end
                    index = capture.index if marked else 0
                    try:
                        self.tx.sendto(HEADER.pack(self.nonce, index, sequence) + PAYLOAD,
                                       self.destination)
                    except BlockingIOError:
                        break
                    except OSError as error:
                        if error.errno not in (ECONNREFUSED, EHOSTDOWN, EHOSTUNREACH, ENETDOWN,
                                               ENETUNREACH, ENOBUFS):
                            raise
                    if marked:
                        if capture.first is None:
                            capture.first = sequence
                        capture.sent += 1
                    sequence += 1
                    credit -= 1
                delay = min(0.002, max(0, (1 - credit) / rate))
                if select([self.rx], [], [], delay)[0]:
                    for _ in range(256):
                        try:
                            packet, peer = self.rx.recvfrom(DATAGRAM_BYTES + 1)
                        except BlockingIOError:
                            break
                        capture = self.capture
                        if capture is None or peer != self.peer or len(packet) != DATAGRAM_BYTES:
                            continue
                        nonce, index, packet_sequence = HEADER.unpack_from(packet)
                        if (nonce == self.nonce and index == capture.index
                                and time.monotonic() <= capture.end + RECEIVER_GRACE_SECONDS):
                            capture.received.add(packet_sequence)
        except OSError as error:
            self.error = type(error).__name__
        finally:
            self.tx.close()
            self.rx.close()

    def close(self, timeout):
        self.stop.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError('UDP stream did not stop')


@dataclass
class Capture:
    start: float
    seconds: int
    epoch: str
    flows: list

    @property
    def end(self):
        return self.start + self.seconds

    @property
    def profile(self):
        return [profile for _, profile, _ in self.flows]


class ReceiverTraffic:
    """TrafficBackend for the existing scheduler, with exact packet-window attribution."""
    def __init__(self, lab, socket_at):
        self.lab, self.socket_at = lab, socket_at
        self.lock = Lock()
        self.active = {}
        self.session = uuid4().hex
        self.generation = 0
        self.index = 0
        self.closed = False

    @property
    def epoch(self):
        return f'{self.session}:{self.generation}'

    def start(self, flow, offered_mbps, *, timeout_seconds):
        with self.lock:
            if self.closed or flow.flow_id in self.active:
                raise ValueError('Cannot start UDP flow')
            self.active[flow.flow_id] = UdpStream(flow, offered_mbps, self.lab, self.socket_at)
            self.generation += 1

    def set_rate(self, flow_id, offered_mbps, *, timeout_seconds):
        with self.lock:
            self.active[flow_id].rate = offered_mbps
            self.generation += 1

    def stop(self, flow_id, *, timeout_seconds):
        with self.lock:
            self.generation += 1
            self.active[flow_id].close(timeout_seconds)
            del self.active[flow_id]

    def poll(self, flow_id, *, timeout_seconds):
        with self.lock:
            stream = self.active[flow_id]
            return (None if stream.thread.is_alive() else
                    FlowExit(returncode=1, error=stream.error or 'UDP worker exited'))

    def cleanup(self, *, timeout_seconds):
        end = time.monotonic() + timeout_seconds
        with self.lock:
            self.closed = True
            self.generation += 1
            for stream in self.active.values():
                stream.stop.set()
            for flow_id, stream in list(self.active.items()):
                stream.close(max(0, end - time.monotonic()))
                del self.active[flow_id]

    def current(self):
        with self.lock:
            return self.epoch, demand_id([flow_profile(stream.flow, stream.rate)
                                          for stream in self.active.values()])

    def begin(self, seconds):
        with self.lock:
            self.index += 1
            start = time.monotonic()
            flows = []
            for stream in self.active.values():
                record = FlowCapture(self.index, start, start + seconds)
                profile = flow_profile(stream.flow, stream.rate)
                stream.capture = record
                flows.append((stream, profile, record))
            return Capture(start, seconds, self.epoch, flows)

    def finish(self, capture, *, measured):
        with self.lock:
            for stream, _, _ in capture.flows:
                stream.capture = None
            if not measured:
                return None
            if capture.epoch != self.epoch:
                raise ValueError('Traffic demand changed during the window')
            if time.monotonic() < capture.end + RECEIVER_GRACE_SECONDS:
                raise ValueError('UDP measurement window has not closed')
            if any(not stream.thread.is_alive() for stream, _, _ in capture.flows):
                raise ValueError('UDP flow exited during its measurement')
            return [{**profile, **record.result(capture.seconds, profile['offered_mbps'])}
                    for _, profile, record in capture.flows]
