"""Compare FIFO, strict priority, Round Robin, and Weighted Round Robin.

Run: python3 qos_simulator.py
Rates are bits/second, sizes are bits, and times are seconds.
Arrivals stop at the configured duration; the simulator then drains accepted
packets so every generated packet is ultimately counted as sent or dropped.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import csv
import heapq
import random
import os
from pathlib import Path
from typing import Deque

LINK_RATE_BPS = 10_000_000
QUEUE_LIMIT_PACKETS = 100  # waiting packets per flow; in-service packet is separate
SIMULATION_TIME_S = 10.0
PRIORITIES = {"voice": 3, "video": 2, "file": 1}
WRR_WEIGHTS = {"voice": 3, "video": 2, "file": 1}


@dataclass(frozen=True)
class FlowConfig:
    name: str
    packet_size_bits: int
    packet_interval_s: float
    start_time_s: float = 0.0
    stop_time_s: float = SIMULATION_TIME_S
    active_windows: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True)
class Packet:
    flow: str
    size_bits: int
    arrival_time_s: float
    sequence: int


def _bits(bytes_count: int) -> int:
    return bytes_count * 8


def nominal_offered_load_mbps(flows: tuple[FlowConfig, ...], duration_s: float) -> float:
    """Return the mean source rate over the measurement window."""
    total_bps = 0.0
    for flow in flows:
        windows = flow.active_windows or ((flow.start_time_s, min(flow.stop_time_s, duration_s)),)
        active_s = sum(max(0.0, min(stop, flow.stop_time_s, duration_s) -
                         max(start, flow.start_time_s, 0.0)) for start, stop in windows)
        total_bps += (active_s / duration_s) * flow.packet_size_bits / flow.packet_interval_s
    return total_bps / 1_000_000


def scale_flow_load(flows: tuple[FlowConfig, ...], target_mbps: float,
                    duration_s: float = SIMULATION_TIME_S) -> tuple[FlowConfig, ...]:
    """Scale all source rates proportionally while preserving the flow mix."""
    if target_mbps <= 0:
        raise ValueError("Target offered load must be positive")
    current = nominal_offered_load_mbps(flows, duration_s)
    factor = current / target_mbps
    return tuple(FlowConfig(flow.name, flow.packet_size_bits,
                            flow.packet_interval_s * factor,
                            flow.start_time_s, flow.stop_time_s, flow.active_windows)
                 for flow in flows)


FLOWS = (
    FlowConfig("voice", _bits(200), 0.020),
    FlowConfig("video", _bits(1200), 0.002),
    FlowConfig("file", _bits(1500), 0.001),
)

# Stress profile: voice alone offers 4 Mbps, so it must compete with video
# and file traffic for the 10 Mbps link.
VOICE_CONGESTION_FLOWS = (
    FlowConfig("voice", _bits(200), 0.0004),
    FlowConfig("video", _bits(1200), 0.002),
    FlowConfig("file", _bits(1500), 0.001),
)

# Voice is present from the start; video and file traffic join halfway through.
DYNAMIC_TRAFFIC_FLOWS = (
    FlowConfig("voice", _bits(200), 0.0004),
    FlowConfig("video", _bits(1200), 0.002, start_time_s=5.0),
    FlowConfig("file", _bits(1500), 0.001, start_time_s=5.0),
)

# All sources emit only during synchronized half-second bursts.
BURSTY_FLOWS = tuple(
    FlowConfig(flow.name, flow.packet_size_bits, flow.packet_interval_s,
               active_windows=((0.0, 0.5), (2.0, 2.5), (4.0, 4.5),
                               (6.0, 6.5), (8.0, 8.5)))
    for flow in FLOWS
)


class Scheduler:
    """Choose a packet from nonempty per-flow queues."""

    def __init__(self, flow_names: list[str]):
        self.flow_names = flow_names

    def select(self, queues: dict[str, Deque[Packet]], now: float) -> Packet:
        raise NotImplementedError


class FIFOScheduler(Scheduler):
    def select(self, queues: dict[str, Deque[Packet]], now: float) -> Packet:
        name = min((n for n in self.flow_names if queues[n]),
                   key=lambda n: (queues[n][0].arrival_time_s,
                                  queues[n][0].sequence))
        return queues[name].popleft()


class PriorityScheduler(Scheduler):
    def select(self, queues: dict[str, Deque[Packet]], now: float) -> Packet:
        name = max((n for n in self.flow_names if queues[n]),
                   key=lambda n: (PRIORITIES.get(n, 0), -queues[n][0].sequence))
        return queues[name].popleft()


class RoundRobinScheduler(Scheduler):
    def __init__(self, flow_names: list[str]):
        super().__init__(flow_names)
        self.cursor = -1

    def select(self, queues: dict[str, Deque[Packet]], now: float) -> Packet:
        for step in range(1, len(self.flow_names) + 1):
            idx = (self.cursor + step) % len(self.flow_names)
            name = self.flow_names[idx]
            if queues[name]:
                self.cursor = idx
                return queues[name].popleft()
        raise RuntimeError("Scheduler called with no packets queued")


class WeightedRoundRobinScheduler(Scheduler):
    """Packet-based WRR: serve each active flow up to its weight per cycle."""

    def __init__(self, flow_names: list[str]):
        super().__init__(flow_names)
        self.weights = {n: max(1, WRR_WEIGHTS.get(n, 1)) for n in flow_names}
        self.cursor = 0
        self.remaining = self.weights[self.flow_names[0]] if self.flow_names else 0

    def select(self, queues: dict[str, Deque[Packet]], now: float) -> Packet:
        if not self.flow_names:
            raise RuntimeError("No flows configured")
        for _ in range(len(self.flow_names) * 2):
            name = self.flow_names[self.cursor]
            if queues[name] and self.remaining > 0:
                packet = queues[name].popleft()
                self.remaining -= 1
                return packet
            self.cursor = (self.cursor + 1) % len(self.flow_names)
            self.remaining = self.weights[self.flow_names[self.cursor]]
        # If active queues were skipped because a flow emptied mid-cycle,
        # restart the cycle at the next active flow.
        for offset in range(len(self.flow_names)):
            idx = (self.cursor + offset) % len(self.flow_names)
            name = self.flow_names[idx]
            if queues[name]:
                self.cursor = idx
                self.remaining = self.weights[name] - 1
                return queues[name].popleft()
        raise RuntimeError("Scheduler called with no packets queued")


class AdaptiveWRRScheduler(Scheduler):
    """Smooth WRR that increases voice share when its queue breaches QoS targets."""

    BASE_WEIGHTS = {"voice": 3, "video": 2, "file": 1}
    MAX_VOICE_WEIGHT = 12
    VOICE_DELAY_TARGET_S = 0.020
    VOICE_QUEUE_TARGET_PACKETS = 12

    def __init__(self, flow_names: list[str]):
        super().__init__(flow_names)
        self.voice_weight = self.BASE_WEIGHTS.get("voice", 1)
        self.credit = {name: 0.0 for name in flow_names}
        self.tie_cursor = -1
        self.weight_changes = 0
        self.adaptation_log: list[tuple[float, int, int, float]] = [(0.0, self.voice_weight, 0, 0.0)]

    def select(self, queues: dict[str, Deque[Packet]], now: float) -> Packet:
        voice_queue = queues.get("voice", deque())
        queue_depth = len(voice_queue)
        head_wait = max(0.0, now - voice_queue[0].arrival_time_s) if voice_queue else 0.0
        previous_weight = self.voice_weight
        overloaded = head_wait > self.VOICE_DELAY_TARGET_S or queue_depth >= self.VOICE_QUEUE_TARGET_PACKETS
        recovered = head_wait < self.VOICE_DELAY_TARGET_S / 4 and queue_depth <= 2
        if overloaded:
            self.voice_weight = min(self.MAX_VOICE_WEIGHT, self.voice_weight + 1)
        elif recovered:
            self.voice_weight = max(self.BASE_WEIGHTS.get("voice", 1), self.voice_weight - 1)
        if self.voice_weight != previous_weight:
            self.weight_changes += 1
            self.adaptation_log.append((now, self.voice_weight, queue_depth, head_wait))

        active = [name for name in self.flow_names if queues[name]]
        weights = {name: self.BASE_WEIGHTS.get(name, 1) for name in active}
        if "voice" in weights:
            weights["voice"] = self.voice_weight
        total_weight = sum(weights.values())
        for name in active:
            self.credit[name] += weights[name]
        index_by_name = {name: self.flow_names.index(name) for name in active}
        selected = max(active, key=lambda name: (self.credit[name],
                                                 -(index_by_name[name] - self.tie_cursor) % len(self.flow_names)))
        self.credit[selected] -= total_weight
        packet = queues[selected].popleft()
        self.tie_cursor = index_by_name[selected]
        if not queues[selected]:
            self.credit[selected] = 0.0
        return packet


SCHEDULERS = {
    "FIFO": FIFOScheduler,
    "Priority": PriorityScheduler,
    "Round Robin": RoundRobinScheduler,
    "Weighted Round Robin": WeightedRoundRobinScheduler,
    "Adaptive WRR": AdaptiveWRRScheduler,
}


def simulate(
    flows: tuple[FlowConfig, ...] = FLOWS,
    link_rate_bps: int = LINK_RATE_BPS,
    queue_limit_packets: int = QUEUE_LIMIT_PACKETS,
    duration_s: float = SIMULATION_TIME_S,
    scheduler_name: str = "FIFO",
    shaper_name: str = "None",
    shaper_rate_bps: int = 5_000_000,
    bucket_capacity_bits: int = 1_000_000,
    include_trace: bool = False,
    trace_packet_limit: int = 900,
    arrival_model: str = "periodic",
    random_seed: int = 0,
) -> dict[str, dict[str, float | int]]:
    """Simulate periodic traffic with a bounded queue per flow, then drain it."""
    if link_rate_bps <= 0 or queue_limit_packets < 0 or duration_s <= 0:
        raise ValueError("Link rate and duration must be positive; queue limit cannot be negative")
    if trace_packet_limit <= 0 or trace_packet_limit > 5000:
        raise ValueError("Trace packet limit must be between 1 and 5000")
    if arrival_model not in {"periodic", "poisson"}:
        raise ValueError("arrival_model must be 'periodic' or 'poisson'")
    if scheduler_name not in SCHEDULERS:
        raise ValueError(f"Unknown scheduler {scheduler_name!r}; choose from {', '.join(SCHEDULERS)}")
    if shaper_name not in {"None", "Token Bucket", "Leaky Bucket"}:
        raise ValueError("shaper_name must be 'None', 'Token Bucket', or 'Leaky Bucket'")
    if shaper_rate_bps <= 0 or bucket_capacity_bits <= 0:
        raise ValueError("Shaper rate and bucket capacity must be positive")
    names = [flow.name for flow in flows]
    if len(set(names)) != len(names):
        raise ValueError("Flow names must be unique")

    # Event tuple: (time, kind priority, packet sequence, kind, packet).
    # Arrivals are processed before completions at equal timestamps.
    events: list[tuple[float, int, int, str, Packet]] = []
    sequence = 0
    generated_by_flow = {name: 0 for name in names}
    shaper_drops_by_flow = {name: 0 for name in names}
    source_packets_by_flow: dict[str, list[Packet]] = {}
    arrival_rng = random.Random(random_seed)
    for flow in flows:
        if flow.packet_size_bits <= 0 or flow.packet_interval_s <= 0:
            raise ValueError(f"Invalid packet size or interval for flow {flow.name!r}")
        windows = flow.active_windows or ((flow.start_time_s, min(flow.stop_time_s, duration_s)),)
        source_packets: list[Packet] = []
        for window_start, window_stop in windows:
            t = max(window_start, flow.start_time_s)
            stop = min(window_stop, flow.stop_time_s, duration_s)
            if arrival_model == "periodic":
                while t < stop:
                    source_packets.append(Packet(flow.name, flow.packet_size_bits, t, sequence))
                    generated_by_flow[flow.name] += 1
                    sequence += 1
                    t += flow.packet_interval_s
            else:
                rate_packets_s = 1.0 / flow.packet_interval_s
                while True:
                    t += arrival_rng.expovariate(rate_packets_s)
                    if t >= stop:
                        break
                    source_packets.append(Packet(flow.name, flow.packet_size_bits, t, sequence))
                    generated_by_flow[flow.name] += 1
                    sequence += 1

        source_packets_by_flow[flow.name] = source_packets

    trace_stride = max(1, (sequence + trace_packet_limit - 1) // trace_packet_limit)
    traced_packet_ids = set(range(0, sequence, trace_stride)) if include_trace else set()
    trace_events: list[dict[str, str | float | int]] = []

    def add_trace(time_s: float, event_name: str, packet: Packet,
                  queue_packets: int = 0, voice_weight: int = 0) -> None:
        if packet.sequence in traced_packet_ids:
            trace_events.append({
                "time_s": time_s,
                "event": event_name,
                "flow": packet.flow,
                "packet_id": packet.sequence,
                "network_queue_packets": queue_packets,
                "voice_weight": voice_weight,
            })

    for flow in flows:
        source_packets = source_packets_by_flow[flow.name]
        for packet in source_packets:
            add_trace(packet.arrival_time_s, "source", packet)

        if shaper_name == "None":
            for packet in source_packets:
                add_trace(packet.arrival_time_s, "shaper_release", packet)
                heapq.heappush(events, (packet.arrival_time_s, 0, packet.sequence, "arrival", packet))
        elif shaper_name == "Token Bucket":
            tokens = float(bucket_capacity_bits)
            last_token_time = 0.0
            last_release_time = 0.0
            for packet in source_packets:
                if packet.size_bits > bucket_capacity_bits:
                    shaper_drops_by_flow[flow.name] += 1
                    add_trace(packet.arrival_time_s, "shaper_drop", packet)
                    continue
                eligible = max(packet.arrival_time_s, last_release_time)
                tokens = min(float(bucket_capacity_bits), tokens +
                             (eligible - last_token_time) * shaper_rate_bps)
                release_time = eligible
                if tokens < packet.size_bits:
                    release_time += (packet.size_bits - tokens) / shaper_rate_bps
                    tokens = 0.0
                else:
                    tokens -= packet.size_bits
                last_token_time = release_time
                last_release_time = release_time
                if release_time > packet.arrival_time_s:
                    add_trace(packet.arrival_time_s, "shaper_wait", packet)
                add_trace(release_time, "shaper_release", packet)
                heapq.heappush(events, (release_time, 0, packet.sequence, "arrival", packet))
        else:  # Leaky Bucket: constant-rate drain with a finite waiting buffer.
            pending: Deque[tuple[float, int]] = deque()
            pending_bits = 0
            last_release_time = 0.0
            for packet in source_packets:
                while pending and pending[0][0] <= packet.arrival_time_s:
                    _, size = pending.popleft()
                    pending_bits -= size
                if packet.size_bits > bucket_capacity_bits or pending_bits + packet.size_bits > bucket_capacity_bits:
                    shaper_drops_by_flow[flow.name] += 1
                    add_trace(packet.arrival_time_s, "shaper_drop", packet)
                    continue
                release_time = max(packet.arrival_time_s, last_release_time) + packet.size_bits / shaper_rate_bps
                if release_time > packet.arrival_time_s:
                    add_trace(packet.arrival_time_s, "shaper_wait", packet)
                add_trace(release_time, "shaper_release", packet)
                pending.append((release_time, packet.size_bits))
                pending_bits += packet.size_bits
                last_release_time = release_time
                heapq.heappush(events, (release_time, 0, packet.sequence, "arrival", packet))

    queues: dict[str, Deque[Packet]] = {name: deque() for name in names}
    scheduler = SCHEDULERS[scheduler_name](names)
    active_packet: Packet | None = None
    stats: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"generated": 0, "transmitted": 0, "dropped": 0,
                 "shaper_dropped": 0,
                 "bits_transmitted_in_window": 0, "delay_sum_s": 0.0,
                 "last_delay_s": 0.0, "jitter_sum_s": 0.0,
                 "delay_samples": 0, "jitter_samples": 0}
    )
    for name in names:
        stats[name]["generated"] = generated_by_flow[name]
        stats[name]["shaper_dropped"] = shaper_drops_by_flow[name]
    queue_area = 0.0
    last_queue_time = 0.0
    max_queue_length = 0
    queue_history: list[dict[str, float | int]] = ([{"time_s": 0.0, "queue_packets": 0}]
                                                   if include_trace else [])

    def queued_count() -> int:
        return sum(map(len, queues.values()))

    def record_queue(now: float) -> None:
        if include_trace:
            queue_history.append({"time_s": now, "queue_packets": queued_count()})

    def start_next(now: float) -> None:
        nonlocal active_packet
        if active_packet is not None or not queued_count():
            return
        active_packet = scheduler.select(queues, now)
        record_queue(now)
        weight = scheduler.voice_weight if isinstance(scheduler, AdaptiveWRRScheduler) else 0
        add_trace(now, "service_start", active_packet, queued_count(), weight)
        finish = now + active_packet.size_bits / link_rate_bps
        heapq.heappush(events, (finish, 1, active_packet.sequence, "complete", active_packet))

    while events:
        now = events[0][0]
        # Time-weighted queue length is measured only during offered-traffic time.
        capped_now = min(now, duration_s)
        if capped_now > last_queue_time:
            queue_area += queued_count() * (capped_now - last_queue_time)
            last_queue_time = capped_now

        while events and events[0][0] == now:
            _, _, _, kind, packet = heapq.heappop(events)
            if kind == "arrival":
                if len(queues[packet.flow]) >= queue_limit_packets:
                    stats[packet.flow]["dropped"] += 1
                    add_trace(now, "network_drop", packet, queued_count())
                else:
                    queues[packet.flow].append(packet)
                    max_queue_length = max(max_queue_length, queued_count())
                    record_queue(now)
                    add_trace(now, "enqueue", packet, queued_count())
            else:
                active_packet = None
                delay = now - packet.arrival_time_s
                row = stats[packet.flow]
                row["transmitted"] += 1
                if now <= duration_s:
                    row["bits_transmitted_in_window"] += packet.size_bits
                row["delay_sum_s"] += delay
                row["delay_samples"] += 1
                if row["delay_samples"] > 1:
                    row["jitter_sum_s"] += abs(delay - float(row["last_delay_s"]))
                    row["jitter_samples"] += 1
                row["last_delay_s"] = delay
                weight = scheduler.voice_weight if isinstance(scheduler, AdaptiveWRRScheduler) else 0
                add_trace(now, "transmit", packet, queued_count(), weight)
            start_next(now)

    # Include any final queue area through the measurement endpoint.
    if last_queue_time < duration_s:
        queue_area += queued_count() * (duration_s - last_queue_time)

    results: dict[str, dict[str, float | int]] = {}
    for flow in flows:
        row = stats[flow.name]
        generated = int(row["generated"])
        transmitted = int(row["transmitted"])
        total_dropped = int(row["dropped"]) + int(row["shaper_dropped"])
        results[flow.name] = {
            "generated_packets": generated,
            "transmitted_packets": transmitted,
            "dropped_packets": int(row["dropped"]),
            "shaper_dropped_packets": int(row["shaper_dropped"]),
            "packet_loss_percent": 100.0 * total_dropped / generated if generated else 0.0,
            "throughput_mbps": float(row["bits_transmitted_in_window"]) / duration_s / 1_000_000,
            "average_delay_ms": 1000.0 * float(row["delay_sum_s"]) / transmitted if transmitted else 0.0,
            "average_jitter_ms": 1000.0 * float(row["jitter_sum_s"]) / int(row["jitter_samples"])
            if int(row["jitter_samples"]) else 0.0,
        }
    results["_queue"] = {
        "maximum_queue_packets": max_queue_length,
        "average_queue_packets": queue_area / duration_s,
        "queue_limit_packets": queue_limit_packets,
        "queue_capacity_total_packets": queue_limit_packets * len(names),
    }
    throughputs = [float(results[name]["throughput_mbps"]) for name in names]
    throughput_sum = sum(throughputs)
    results["_summary"] = {
        "jain_fairness_index": (throughput_sum ** 2 / (len(throughputs) * sum(x * x for x in throughputs)))
        if throughputs and any(throughputs) else 0.0,
    }
    if isinstance(scheduler, AdaptiveWRRScheduler):
        results["_summary"]["adaptive_weight_changes"] = scheduler.weight_changes
        results["_summary"]["adaptive_max_voice_weight"] = max(
            (entry[1] for entry in scheduler.adaptation_log), default=scheduler.voice_weight)
        results["_summary"]["adaptive_weight_trace"] = scheduler.adaptation_log
    if include_trace:
        order = {"source": 0, "shaper_wait": 1, "shaper_release": 2, "shaper_drop": 2,
                 "enqueue": 3, "network_drop": 3, "service_start": 4, "transmit": 5}
        trace_events.sort(key=lambda row: (float(row["time_s"]), order[str(row["event"])],
                                           int(row["packet_id"])))
        results["_summary"]["event_trace"] = trace_events
        if len(queue_history) > 800:
            bucket_size = (len(queue_history) + 199) // 200
            reduced: list[dict[str, float | int]] = []
            for offset in range(0, len(queue_history), bucket_size):
                bucket = queue_history[offset:offset + bucket_size]
                indexes = {0, len(bucket) - 1,
                           min(range(len(bucket)), key=lambda i: int(bucket[i]["queue_packets"])),
                           max(range(len(bucket)), key=lambda i: int(bucket[i]["queue_packets"]))}
                reduced.extend(bucket[index] for index in sorted(indexes))
            queue_history = reduced[:800]
        results["_summary"]["queue_history"] = queue_history
    return results


def print_report(scheduler_name: str, results: dict[str, dict[str, float | int]]) -> None:
    print(f"{scheduler_name} | link={LINK_RATE_BPS / 1_000_000:g} Mbps | "
          f"arrival window={SIMULATION_TIME_S:g} s (accepted packets drained afterward)")
    print(f"{'Flow':<10} {'Generated':>10} {'Sent':>8} {'Net drop':>9} {'Shape drop':>11} {'Loss %':>9} "
          f"{'Mbps':>9} {'Delay ms':>10} {'Jitter ms':>11}")
    for flow_name, row in results.items():
        if flow_name.startswith("_"):
            continue
        print(f"{flow_name:<10} {row['generated_packets']:>10} {row['transmitted_packets']:>8} "
              f"{row['dropped_packets']:>9} {row['shaper_dropped_packets']:>11} "
              f"{row['packet_loss_percent']:>8.2f}% "
              f"{row['throughput_mbps']:>9.3f} {row['average_delay_ms']:>10.3f} "
              f"{row['average_jitter_ms']:>11.3f}")
    queue = results["_queue"]
    print(f"Queue: average={queue['average_queue_packets']:.2f} packets, "
          f"maximum total={queue['maximum_queue_packets']} packets; "
          f"limit={queue['queue_limit_packets']} packets per flow")
    print(f"Jain throughput fairness: {results['_summary']['jain_fairness_index']:.3f} (1.0 = equal throughput)")
    if "adaptive_max_voice_weight" in results["_summary"]:
        print(f"Adaptive voice weight: max={results['_summary']['adaptive_max_voice_weight']}, "
              f"adjustments={results['_summary']['adaptive_weight_changes']}")


def run_experiments(output_dir: Path = Path("results")) -> list[dict[str, str | float | int]]:
    """Run the comparison matrix, save one CSV row per flow, and create plots."""
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ".matplotlib").mkdir(exist_ok=True)

    default_variants = tuple((name, "None", 5_000_000, 1_000_000) for name in SCHEDULERS)
    bursty_variants: list[tuple[str, str, int, int]] = [("Priority", "None", 5_000_000, 1_000_000)]
    sweep_rates_bps = (2_000_000, 5_000_000, 8_000_000)
    sweep_capacities_bits = (250_000, 1_000_000, 2_000_000)
    for shaper in ("Token Bucket", "Leaky Bucket"):
        bursty_variants.extend(("Priority", shaper, rate, 1_000_000)
                               for rate in sweep_rates_bps)
        bursty_variants.extend(("Priority", shaper, 5_000_000, capacity)
                               for capacity in sweep_capacities_bits)
    # Remove the duplicated 5 Mbps / 1 Mb setting shared by the two sweeps.
    bursty_variants = list(dict.fromkeys(bursty_variants))
    experiment_sets = [
        ("Default traffic", FLOWS, default_variants,
         nominal_offered_load_mbps(FLOWS, SIMULATION_TIME_S)),
        ("Congested voice", VOICE_CONGESTION_FLOWS, default_variants,
         nominal_offered_load_mbps(VOICE_CONGESTION_FLOWS, SIMULATION_TIME_S)),
        ("Changing traffic", DYNAMIC_TRAFFIC_FLOWS, default_variants,
         nominal_offered_load_mbps(DYNAMIC_TRAFFIC_FLOWS, SIMULATION_TIME_S)),
        ("Bursty traffic", BURSTY_FLOWS, tuple(bursty_variants),
         nominal_offered_load_mbps(BURSTY_FLOWS, SIMULATION_TIME_S)),
    ]
    load_levels_mbps = (5.0, 10.0, 15.0, 20.0)
    for target_load in load_levels_mbps:
        scaled = scale_flow_load(FLOWS, target_load)
        experiment_sets.append((f"Offered load {target_load:g} Mbps", scaled,
                                default_variants, target_load))
    records: list[dict[str, str | float | int]] = []
    controller_trace: list[dict[str, str | float | int]] = []
    for scenario, scenario_flows, variants, offered_load in experiment_sets:
        for scheduler_name, shaper_name, rate_bps, capacity_bits in variants:
            metrics = simulate(flows=scenario_flows, scheduler_name=scheduler_name,
                               shaper_name=shaper_name, shaper_rate_bps=rate_bps,
                               bucket_capacity_bits=capacity_bits)
            queue = metrics["_queue"]
            fairness = float(metrics["_summary"]["jain_fairness_index"])
            adaptive_changes = int(metrics["_summary"].get("adaptive_weight_changes", 0))
            adaptive_max_weight = int(metrics["_summary"].get("adaptive_max_voice_weight", 3))
            for timestamp, voice_weight, voice_queue_packets, voice_head_wait in metrics["_summary"].get(
                    "adaptive_weight_trace", []):
                controller_trace.append({
                    "scenario": scenario,
                    "scheduler": scheduler_name,
                    "time_s": float(timestamp),
                    "voice_weight": int(voice_weight),
                    "voice_queue_packets": int(voice_queue_packets),
                    "voice_head_wait_ms": 1000.0 * float(voice_head_wait),
                })
            for flow in scenario_flows:
                row = metrics[flow.name]
                records.append({
                    "scenario": scenario,
                    "scheduler": scheduler_name,
                    "shaper": shaper_name,
                    "link_rate_mbps": LINK_RATE_BPS / 1_000_000,
                    "duration_s": SIMULATION_TIME_S,
                    "offered_load_mbps": offered_load,
                    "queue_limit_packets_per_flow": QUEUE_LIMIT_PACKETS,
                    "shaper_rate_mbps_per_flow": rate_bps / 1_000_000,
                    "bucket_capacity_mb_per_flow": capacity_bits / 1_000_000,
                    "flow": flow.name,
                    "generated_packets": int(row["generated_packets"]),
                    "transmitted_packets": int(row["transmitted_packets"]),
                    "network_dropped_packets": int(row["dropped_packets"]),
                    "shaper_dropped_packets": int(row["shaper_dropped_packets"]),
                    "packet_loss_percent": float(row["packet_loss_percent"]),
                    "throughput_mbps": float(row["throughput_mbps"]),
                    "average_delay_ms": float(row["average_delay_ms"]),
                    "average_jitter_ms": float(row["average_jitter_ms"]),
                    "average_queue_packets": float(queue["average_queue_packets"]),
                    "maximum_queue_packets": int(queue["maximum_queue_packets"]),
                    "jain_fairness_index": fairness,
                    "adaptive_voice_weight_changes": adaptive_changes,
                    "adaptive_max_voice_weight": adaptive_max_weight,
                })

    csv_path = output_dir / "experiment_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    trace_path = output_dir / "adaptive_controller_trace.csv"
    with trace_path.open("w", newline="", encoding="utf-8") as trace_file:
        fields = ("scenario", "scheduler", "time_s", "voice_weight",
                  "voice_queue_packets", "voice_head_wait_ms")
        writer = csv.DictWriter(trace_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(controller_trace)
    create_plots(records, plots_dir)
    create_bursty_sweep_plots(records, plots_dir)
    create_load_sweep_plot(records, plots_dir)
    create_adaptive_plot(controller_trace, plots_dir)
    stochastic_rows = run_stochastic_experiments(output_dir)
    print(f"Saved {len(records)} flow-level experiment rows to {csv_path}")
    print(f"Saved {len(controller_trace)} controller changes to {trace_path}")
    print(f"Saved {len(stochastic_rows)} stochastic seed runs under {output_dir}")
    print(f"Saved comparison plots to {plots_dir}")
    return records


def run_stochastic_experiments(output_dir: Path = Path("results"),
                               seeds: tuple[int, ...] = tuple(range(1, 21))) -> list[dict[str, str | float | int]]:
    """Repeat scheduler comparisons across traffic profiles and shapers with Poisson arrivals."""
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise ValueError("Stochastic experiments require at least two unique random seeds")
    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios = (
        ("Default traffic", FLOWS),
        ("Congested voice", VOICE_CONGESTION_FLOWS),
        ("Changing traffic", DYNAMIC_TRAFFIC_FLOWS),
        ("Bursty traffic", BURSTY_FLOWS),
    )
    shaper_configs = (
        ("None", 5_000_000, 1_000_000),
        ("Token Bucket", 5_000_000, 1_000_000),
        ("Leaky Bucket", 5_000_000, 1_000_000),
    )
    raw_rows: list[dict[str, str | float | int]] = []
    metric_names = ("throughput_mbps", "packet_loss_percent", "average_delay_ms",
                    "average_jitter_ms", "average_queue_packets", "jain_fairness_index")
    grouped: dict[tuple[str, str, str, str], dict[str, list[float]]] = {}
    for seed in seeds:
        for scenario, flows in scenarios:
            for shaper_name, shaper_rate_bps, capacity_bits in shaper_configs:
                for scheduler_name in SCHEDULERS:
                    metrics = simulate(flows=flows,
                                       scheduler_name=scheduler_name,
                                       shaper_name=shaper_name,
                                       shaper_rate_bps=shaper_rate_bps,
                                       bucket_capacity_bits=capacity_bits,
                                       arrival_model="poisson", random_seed=seed)
                    fairness = float(metrics["_summary"]["jain_fairness_index"])
                    queue = metrics["_queue"]
                    for flow in flows:
                        row = metrics[flow.name]
                        values = {
                            "throughput_mbps": float(row["throughput_mbps"]),
                            "packet_loss_percent": float(row["packet_loss_percent"]),
                            "average_delay_ms": float(row["average_delay_ms"]),
                            "average_jitter_ms": float(row["average_jitter_ms"]),
                            "average_queue_packets": float(queue["average_queue_packets"]),
                            "jain_fairness_index": fairness,
                        }
                        raw_rows.append({
                            "scenario": scenario, "arrival_model": "Poisson",
                            "shaper": shaper_name,
                            "shaper_rate_mbps_per_flow": shaper_rate_bps / 1_000_000,
                            "bucket_capacity_mb_per_flow": capacity_bits / 1_000_000,
                            "seed": seed, "scheduler": scheduler_name,
                            "flow": flow.name,
                            "generated_packets": int(row["generated_packets"]),
                            "transmitted_packets": int(row["transmitted_packets"]),
                            "network_dropped_packets": int(row["dropped_packets"]),
                            "shaper_dropped_packets": int(row["shaper_dropped_packets"]),
                            **values,
                        })
                        key = (scenario, shaper_name, scheduler_name, flow.name)
                        bucket = grouped.setdefault(key, {name: [] for name in metric_names})
                        for name, value in values.items():
                            bucket[name].append(value)

    raw_path = output_dir / "stochastic_seed_results.csv"
    with raw_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(raw_rows[0]))
        writer.writeheader()
        writer.writerows(raw_rows)

    # Two-sided 95% Student-t critical values for common sample sizes; normal
    # approximation is used for larger samples.
    t_critical_95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
                     7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262, 11: 2.228,
                     12: 2.201, 13: 2.179, 14: 2.160, 15: 2.145, 16: 2.131,
                     17: 2.120, 18: 2.110, 19: 2.101, 20: 2.093, 21: 2.086,
                     22: 2.080, 23: 2.074, 24: 2.069, 25: 2.064, 26: 2.060,
                     27: 2.056, 28: 2.052, 29: 2.048, 30: 2.045}
    critical_value = t_critical_95.get(len(seeds), 1.96)
    summary_rows: list[dict[str, str | float | int]] = []
    for (scenario, shaper_name, scheduler_name, flow_name), metrics_by_name in grouped.items():
        row: dict[str, str | float | int] = {
            "scenario": scenario,
            "arrival_model": "Poisson",
            "shaper": shaper_name,
            "scheduler": scheduler_name,
            "flow": flow_name,
            "seed_runs": len(seeds),
        }
        for metric_name, observations in metrics_by_name.items():
            average = sum(observations) / len(observations)
            sample_sd = (sum((value - average) ** 2 for value in observations) /
                         (len(observations) - 1)) ** 0.5 if len(observations) > 1 else 0.0
            margin = critical_value * sample_sd / len(observations) ** 0.5
            row[f"{metric_name}_mean"] = average
            row[f"{metric_name}_ci95_low"] = average - margin
            row[f"{metric_name}_ci95_high"] = average + margin
        summary_rows.append(row)
    summary_path = output_dir / "stochastic_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    # Paired differences use each seed's identical source trace for both schedulers.
    by_run = {(str(row["scenario"]), str(row["shaper"]), str(row["scheduler"]),
               str(row["flow"]), int(row["seed"])): row for row in raw_rows}
    adaptive_name = "Adaptive WRR"
    comparisons: list[dict[str, str | float | int]] = []
    for scenario, flows in scenarios:
        for shaper_name, _, _ in shaper_configs:
            for scheduler_name in SCHEDULERS:
                if scheduler_name == adaptive_name:
                    continue
                for flow in flows:
                    for metric_name in metric_names:
                        differences = [
                            float(by_run[(scenario, shaper_name, scheduler_name, flow.name, seed)][metric_name]) -
                            float(by_run[(scenario, shaper_name, adaptive_name, flow.name, seed)][metric_name])
                            for seed in seeds
                        ]
                        average = sum(differences) / len(differences)
                        sample_sd = (sum((value - average) ** 2 for value in differences) /
                                     (len(differences) - 1)) ** 0.5
                        margin = critical_value * sample_sd / len(differences) ** 0.5
                        comparisons.append({
                            "scenario": scenario,
                            "shaper": shaper_name,
                            "flow": flow.name,
                            "metric": metric_name,
                            "comparison": f"{scheduler_name} minus {adaptive_name}",
                            "seed_pairs": len(differences),
                            "mean_difference": average,
                            "ci95_low": average - margin,
                            "ci95_high": average + margin,
                            "interpretation": "negative favors Adaptive WRR" if metric_name != "jain_fairness_index"
                                              else "positive favors Adaptive WRR",
                        })
    paired_path = output_dir / "stochastic_paired_comparisons.csv"
    with paired_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(comparisons[0]))
        writer.writeheader()
        writer.writerows(comparisons)
    create_stochastic_voice_loss_plot(summary_rows, output_dir / "plots" / "stochastic_voice_loss.png")
    return raw_rows


def create_stochastic_voice_loss_plot(
    summary_rows: list[dict[str, str | float | int]], output_path: Path
) -> None:
    """Plot voice packet loss means and 95% confidence intervals for the seed matrix."""
    os.environ.setdefault("MPLCONFIGDIR", str(output_path.parent.parent / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    scenarios = ("Default traffic", "Congested voice", "Changing traffic", "Bursty traffic")
    shapers = ("None", "Token Bucket", "Leaky Bucket")
    scheduler_labels = {
        "FIFO": "FIFO", "Priority": "Priority", "Round Robin": "RR",
        "Weighted Round Robin": "WRR", "Adaptive WRR": "Adaptive",
    }
    scheduler_colors = {
        "FIFO": "#4e79a7", "Priority": "#e15759", "Round Robin": "#59a14f",
        "Weighted Round Robin": "#f28e2b", "Adaptive WRR": "#8064a2",
    }
    by_group = {(str(row["scenario"]), str(row["shaper"]), str(row["scheduler"])): row
                for row in summary_rows if row["flow"] == "voice"}
    fig, axes = plt.subplots(len(scenarios), len(shapers), figsize=(13, 10), sharey=True)
    x_values = list(range(len(SCHEDULERS)))
    for row_index, scenario in enumerate(scenarios):
        for column_index, shaper in enumerate(shapers):
            axis = axes[row_index, column_index]
            for x, scheduler in zip(x_values, SCHEDULERS):
                result = by_group[(scenario, shaper, scheduler)]
                mean = float(result["packet_loss_percent_mean"])
                lower = float(result["packet_loss_percent_ci95_low"])
                upper = float(result["packet_loss_percent_ci95_high"])
                axis.errorbar(
                    x, mean,
                    yerr=[[max(0.0, mean - lower)], [max(0.0, upper - mean)]],
                    fmt="o", markersize=5, capsize=2.5, linewidth=1.2,
                    color=scheduler_colors[scheduler],
                )
            axis.set_title(f"{scenario}\n{shaper}", fontsize=9)
            axis.set_xticks(x_values, [scheduler_labels[name] for name in SCHEDULERS],
                            rotation=30, ha="right")
            axis.set_ylim(0, 100)
            axis.set_yticks((0, 25, 50, 75, 100))
            axis.grid(axis="y", color="#d9dee7", linewidth=0.7)
            axis.set_axisbelow(True)
            if column_index == 0:
                axis.set_ylabel("Voice packet loss (%)")
    fig.suptitle("Stochastic voice loss by traffic profile, shaper, and scheduler",
                 fontsize=15, fontweight="bold", y=0.99)
    fig.text(0.5, 0.015, "Points show 20-seed means; whiskers show two-sided 95% t confidence intervals.",
             ha="center", fontsize=9, color="#465166")
    fig.tight_layout(rect=(0.025, 0.045, 1, 0.96), h_pad=1.5, w_pad=1.0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def create_plots(records: list[dict[str, str | float | int]], plots_dir: Path) -> None:
    """Create report-ready scheduler and shaper comparison charts."""
    os.environ.setdefault("MPLCONFIGDIR", str(plots_dir.parent / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    scenarios = ("Default traffic", "Congested voice", "Changing traffic")
    for scenario in scenarios:
        subset = [r for r in records if r["scenario"] == scenario]
        variants = list(dict.fromkeys((str(r["scheduler"]), str(r["shaper"])) for r in subset))
        labels = [s if shaper == "None" else shaper for s, shaper in variants]
        groups = [[r for r in subset if (r["scheduler"], r["shaper"]) == variant]
                  for variant in variants]
        x = list(range(len(variants)))
        fig, axes = plt.subplots(2, 2, figsize=(13, 8.2))
        fig.suptitle(f"{scenario}: QoS and fairness comparison", fontsize=15, fontweight="bold")

        mean_delay = [sum(float(r["average_delay_ms"]) for r in group) / len(group) for group in groups]
        axes[0, 0].bar(x, mean_delay, color="#4472C4")
        axes[0, 0].set_title("Mean per-flow delay")
        axes[0, 0].set_ylabel("Milliseconds")

        mean_loss = [sum(float(r["packet_loss_percent"]) for r in group) / len(group) for group in groups]
        axes[0, 1].bar(x, mean_loss, color="#ED7D31")
        axes[0, 1].set_title("Mean per-flow packet loss")
        axes[0, 1].set_ylabel("Percent")

        flow_names = list(dict.fromkeys(str(r["flow"]) for r in subset))
        width = 0.8 / max(1, len(flow_names))
        for flow_index, flow_name in enumerate(flow_names):
            vals = []
            for group in groups:
                match = next((float(r["throughput_mbps"]) for r in group if r["flow"] == flow_name), 0.0)
                vals.append(match)
            offsets = [i - 0.4 + width * (flow_index + 0.5) for i in x]
            axes[1, 0].bar(offsets, vals, width=width, label=flow_name)
        axes[1, 0].set_title("Throughput by flow")
        axes[1, 0].set_ylabel("Mbps")
        axes[1, 0].legend(frameon=False, fontsize=8)

        fairness = [float(group[0]["jain_fairness_index"]) for group in groups]
        axes[1, 1].bar(x, fairness, color="#70AD47")
        axes[1, 1].set_title("Jain throughput fairness")
        axes[1, 1].set_ylabel("Index (0–1)")
        axes[1, 1].set_ylim(0, 1.05)

        for ax in axes.flat:
            ax.set_xticks(x, labels, rotation=20, ha="right")
            ax.grid(axis="y", alpha=0.25)
            ax.set_axisbelow(True)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        filename = scenario.lower().replace(" ", "_") + ".png"
        fig.savefig(plots_dir / filename, dpi=180, bbox_inches="tight")
        plt.close(fig)


def create_bursty_sweep_plots(records: list[dict[str, str | float | int]], plots_dir: Path) -> None:
    """Plot how shaper rate and buffer capacity affect bursty traffic results."""
    os.environ.setdefault("MPLCONFIGDIR", str(plots_dir.parent / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = [r for r in records if r["scenario"] == "Bursty traffic"]

    def summarize(shaper: str, rate: float | None = None,
                  capacity: float | None = None) -> dict[str, float] | None:
        group = [r for r in data if r["shaper"] == shaper
                 and (rate is None or float(r["shaper_rate_mbps_per_flow"]) == rate)
                 and (capacity is None or float(r["bucket_capacity_mb_per_flow"]) == capacity)]
        if not group:
            return None
        total_generated = sum(int(r["generated_packets"]) for r in group)
        total_dropped = sum(int(r["network_dropped_packets"]) + int(r["shaper_dropped_packets"])
                            for r in group)
        return {
            "delay": sum(float(r["average_delay_ms"]) for r in group) / len(group),
            "loss": 100.0 * total_dropped / total_generated if total_generated else 0.0,
            "throughput": sum(float(r["throughput_mbps"]) for r in group),
            "fairness": float(group[0]["jain_fairness_index"]),
        }

    def make_sweep(filename: str, title: str, x_values: tuple[float, ...],
                   x_field: str, fixed_field: str, fixed_value: float) -> None:
        fig, axes = plt.subplots(2, 2, figsize=(12, 8))
        fig.suptitle(title, fontsize=15, fontweight="bold")
        series = (("delay", "Mean per-flow delay", "Milliseconds"),
                  ("loss", "Total packet loss", "Percent"),
                  ("throughput", "Aggregate throughput", "Mbps"),
                  ("fairness", "Jain throughput fairness", "Index (0–1)"))
        for shaper in ("Token Bucket", "Leaky Bucket"):
            summaries = []
            for x_value in x_values:
                filters = {x_field: x_value, fixed_field: fixed_value}
                summary = summarize(shaper, rate=filters.get("shaper_rate_mbps_per_flow"),
                                    capacity=filters.get("bucket_capacity_mb_per_flow"))
                summaries.append(summary)
            for ax, (metric, metric_title, ylabel) in zip(axes.flat, series):
                values = [s[metric] if s is not None else float("nan") for s in summaries]
                ax.plot(x_values, values, marker="o", linewidth=2, label=shaper)
                ax.set_title(metric_title)
                ax.set_ylabel(ylabel)
                ax.grid(alpha=0.25)
                if metric == "fairness":
                    ax.set_ylim(0, 1.05)
                ax.legend(frameon=False, fontsize=8)
        for ax in axes[1, :]:
            ax.set_xlabel("Shaper rate (Mbps per flow)" if x_field == "shaper_rate_mbps_per_flow"
                          else "Bucket / buffer capacity (Mb per flow)")
        for ax in axes[0, :]:
            ax.set_xlabel("Shaper rate (Mbps per flow)" if x_field == "shaper_rate_mbps_per_flow"
                          else "Bucket / buffer capacity (Mb per flow)")
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fig.savefig(plots_dir / filename, dpi=180, bbox_inches="tight")
        plt.close(fig)

    make_sweep("bursty_rate_sweep.png", "Bursty traffic: effect of shaper rate",
               (2.0, 5.0, 8.0), "shaper_rate_mbps_per_flow",
               "bucket_capacity_mb_per_flow", 1.0)
    make_sweep("bursty_capacity_sweep.png", "Bursty traffic: effect of bucket capacity",
               (0.25, 1.0, 2.0), "bucket_capacity_mb_per_flow",
               "shaper_rate_mbps_per_flow", 5.0)


def create_load_sweep_plot(records: list[dict[str, str | float | int]], plots_dir: Path) -> None:
    """Plot scheduler QoS metrics across offered load levels."""
    os.environ.setdefault("MPLCONFIGDIR", str(plots_dir.parent / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = [r for r in records if str(r["scenario"]).startswith("Offered load ")]
    load_levels = sorted({float(r["offered_load_mbps"]) for r in data})
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.suptitle("Scheduler performance versus offered load", fontsize=15, fontweight="bold")
    metrics = (("delay", "Mean per-flow delay", "Milliseconds"),
               ("loss", "Total packet loss", "Percent"),
               ("throughput", "Aggregate throughput", "Mbps"),
               ("fairness", "Jain throughput fairness", "Index (0–1)"))

    for scheduler_name in SCHEDULERS:
        summaries: list[dict[str, float]] = []
        for level in load_levels:
            group = [r for r in data if r["scheduler"] == scheduler_name
                     and float(r["offered_load_mbps"]) == level]
            generated = sum(int(r["generated_packets"]) for r in group)
            dropped = sum(int(r["network_dropped_packets"]) + int(r["shaper_dropped_packets"])
                          for r in group)
            summaries.append({
                "delay": sum(float(r["average_delay_ms"]) for r in group) / len(group),
                "loss": 100.0 * dropped / generated if generated else 0.0,
                "throughput": sum(float(r["throughput_mbps"]) for r in group),
                "fairness": float(group[0]["jain_fairness_index"]),
            })
        for ax, (metric, metric_title, ylabel) in zip(axes.flat, metrics):
            ax.plot(load_levels, [s[metric] for s in summaries], marker="o", linewidth=2,
                    label=scheduler_name)
            ax.set_title(metric_title)
            ax.set_xlabel("Offered load (Mbps)")
            ax.set_ylabel(ylabel)
            ax.grid(alpha=0.25)
            if metric == "fairness":
                ax.set_ylim(0, 1.05)
            ax.legend(frameon=False, fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(plots_dir / "offered_load_sweep.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def create_adaptive_plot(trace: list[dict[str, str | float | int]], plots_dir: Path) -> None:
    """Show the adaptive controller's voice weight and observed queue signals."""
    os.environ.setdefault("MPLCONFIGDIR", str(plots_dir.parent / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    changing = sorted((row for row in trace if row["scenario"] == "Changing traffic"),
                      key=lambda row: float(row["time_s"]))
    if not changing:
        return
    times = [float(row["time_s"]) for row in changing]
    weights = [int(row["voice_weight"]) for row in changing]
    waits = [float(row["voice_head_wait_ms"]) for row in changing]
    depths = [int(row["voice_queue_packets"]) for row in changing]
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    fig.suptitle("Adaptive WRR response to changing traffic", fontsize=15, fontweight="bold")
    axes[0].step(times, weights, where="post", color="#7a4fc2", linewidth=2.2)
    axes[0].axvline(5.0, color="#d58a2b", linestyle="--", linewidth=1.5,
                    label="Video and file traffic start")
    axes[0].set_ylabel("Voice packet weight")
    axes[0].set_ylim(2, 13)
    axes[0].grid(alpha=0.25)
    axes[0].legend(frameon=False)
    axes[1].plot(times, waits, color="#d9534f", linewidth=1.8, label="Voice head-of-line wait")
    axes[1].axhline(AdaptiveWRRScheduler.VOICE_DELAY_TARGET_S * 1000,
                    color="#d9534f", linestyle=":", alpha=0.75, label="Delay target")
    axes[1].set_ylabel("Head wait (ms)")
    axes[1].set_xlabel("Simulation time (s)")
    ax_depth = axes[1].twinx()
    ax_depth.plot(times, depths, color="#3676db", alpha=0.75, linewidth=1.6,
                  label="Voice queue depth")
    ax_depth.axhline(AdaptiveWRRScheduler.VOICE_QUEUE_TARGET_PACKETS,
                     color="#3676db", linestyle=":", alpha=0.75, label="Queue target")
    ax_depth.set_ylabel("Queued voice packets")
    handles1, labels1 = axes[1].get_legend_handles_labels()
    handles2, labels2 = ax_depth.get_legend_handles_labels()
    axes[1].legend(handles1 + handles2, labels1 + labels2, frameon=False, loc="upper left", ncol=2)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(plots_dir / "adaptive_wrr_response.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    scenarios = (("Default traffic", FLOWS),
                 ("Congested voice (voice offers 4 Mbps)", VOICE_CONGESTION_FLOWS))
    for scenario_name, scenario_flows in scenarios:
        print(f"=== {scenario_name} ===")
        for algorithm in SCHEDULERS:
            print_report(algorithm, simulate(flows=scenario_flows, scheduler_name=algorithm))
            print()
    print("=== Bursty traffic shaping comparison (Priority scheduler) ===")
    for shaper in ("None", "Token Bucket", "Leaky Bucket"):
        results = simulate(flows=BURSTY_FLOWS, scheduler_name="Priority", shaper_name=shaper)
        print(f"Shaper: {shaper} | rate=5 Mbps per flow | bucket/buffer=1 Mb per flow")
        print_report("Priority", results)
        print()
    run_experiments()
