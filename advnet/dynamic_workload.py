"""Offline dynamic UDP demand definitions; no execution or measured results.

Times are experiment-relative seconds. Host names are manifest router keys,
matching the existing traffic profiles. Active lifetimes are [start, stop).
"""
import random
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .scenarios import MAX_CONCURRENT_FLOWS, validate_flow_endpoints


OfferedMbps = Annotated[float, Field(gt=0, le=1000)]
Seconds = Annotated[float, Field(ge=0)]
FlowId = Annotated[str, Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')]


class RateChange(Contract):
    at_seconds: Seconds
    offered_mbps: OfferedMbps


class TrafficBurst(Contract):
    start_seconds: Seconds
    stop_seconds: Seconds
    offered_mbps: OfferedMbps

    @model_validator(mode='after')
    def ordered(self):
        if self.start_seconds >= self.stop_seconds:
            raise ValueError('Burst start must precede stop')
        return self


class DynamicFlow(Contract):
    flow_id: FlowId
    source: str
    destination: str
    destination_port: int = Field(default=5201, ge=1, le=65535)
    offered_mbps: OfferedMbps
    start_seconds: Seconds
    stop_seconds: Seconds
    rate_changes: list[RateChange] = Field(default_factory=list)
    bursts: list[TrafficBurst] = Field(default_factory=list)

    @model_validator(mode='after')
    def timeline(self):
        if self.source == self.destination:
            raise ValueError('Traffic endpoints must differ')
        if self.start_seconds >= self.stop_seconds:
            raise ValueError('Flow start must precede stop')
        previous = self.start_seconds
        for change in self.rate_changes:
            if not previous < change.at_seconds < self.stop_seconds:
                raise ValueError('Rate changes must be strictly ordered inside the flow lifetime')
            previous = change.at_seconds
        previous = self.start_seconds
        for burst in self.bursts:
            if not previous <= burst.start_seconds < burst.stop_seconds <= self.stop_seconds:
                raise ValueError('Bursts must be ordered, non-overlapping, and inside the flow lifetime')
            baseline = self.offered_mbps
            for change in self.rate_changes:
                if change.at_seconds <= burst.start_seconds:
                    baseline = change.offered_mbps
            underlying = [baseline] + [change.offered_mbps for change in self.rate_changes
                                       if burst.start_seconds < change.at_seconds < burst.stop_seconds]
            if burst.offered_mbps <= max(underlying):
                raise ValueError('Burst offered_mbps must exceed the sustained rate throughout the burst')
            previous = burst.stop_seconds
        return self


def _boundaries(flows):
    # Departures precede arrivals so back-to-back lifetimes can reuse a port.
    return sorted((at, priority, index) for index, flow in enumerate(flows)
                  for at, priority in ((flow.stop_seconds, 0), (flow.start_seconds, 1)))


class DynamicWorkload(Contract):
    schema_version: Literal['1'] = '1'
    duration_seconds: float = Field(gt=0, le=86400)
    seed: int | None = None
    flows: list[DynamicFlow] = Field(min_length=1)

    @model_validator(mode='after')
    def workload(self):
        identifiers = [flow.flow_id for flow in self.flows]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError('Duplicate flow IDs')
        if any(flow.stop_seconds > self.duration_seconds for flow in self.flows):
            raise ValueError('Flow stop exceeds total experiment duration')
        active = {}
        for _, priority, index in _boundaries(self.flows):
            if priority == 0:
                del active[index]
            else:
                flow = self.flows[index]
                if len(active) >= MAX_CONCURRENT_FLOWS:
                    raise ValueError(f'At most {MAX_CONCURRENT_FLOWS} concurrent flows are supported')
                if any((other.destination, other.destination_port) ==
                       (flow.destination, flow.destination_port) for other in active.values()):
                    raise ValueError('Overlapping flows cannot share a destination host/port')
                active[index] = flow
        return self

    def validate_inventory(self, manifest):
        """Reuse endpoint checks, including aliases of the same physical host.

        Call again against the target manifest before a future execution. Schema
        parsing alone cannot establish whether a host exists in that manifest.
        """
        routers = manifest['routers']
        for flow in self.flows:
            validate_flow_endpoints([flow.model_dump()], routers)
            if routers[flow.source]['host'] == routers[flow.destination]['host']:
                raise ValueError('Traffic endpoints must be distinct physical hosts')
        active = {}
        for _, priority, index in _boundaries(self.flows):
            if priority == 0:
                del active[index]
            else:
                active[index] = self.flows[index].model_dump()
                validate_flow_endpoints(list(active.values()), routers)
        return self


class WorkloadEvent(Contract):
    sequence: int = Field(ge=0)
    at_seconds: Seconds
    flow_id: FlowId
    action: Literal['start', 'set_rate', 'stop']
    offered_mbps: OfferedMbps | None = None

    @model_validator(mode='after')
    def rate_required(self):
        if (self.action == 'stop') != (self.offered_mbps is None):
            raise ValueError('Start/set_rate require offered_mbps; stop must omit it')
        return self


def _compile_events(flows):
    pending = []
    priorities = {'stop': 0, 'set_rate': 1, 'start': 2}
    for index, flow in enumerate(flows):
        changes = {change.at_seconds: change.offered_mbps for change in flow.rate_changes}
        times = {flow.start_seconds, flow.stop_seconds, *changes}
        for burst in flow.bursts:
            times.update((burst.start_seconds, burst.stop_seconds))
        baseline, last_rate = flow.offered_mbps, None
        for at in sorted(times):
            baseline = changes.get(at, baseline)
            if at == flow.stop_seconds:
                action, rate = 'stop', None
            else:
                rate = next((burst.offered_mbps for burst in flow.bursts
                             if burst.start_seconds <= at < burst.stop_seconds), baseline)
                action = 'start' if at == flow.start_seconds else 'set_rate'
                if action == 'set_rate' and rate == last_rate:
                    continue
            pending.append((at, priorities[action], index, flow.flow_id, action, rate))
            last_rate = rate
    return [WorkloadEvent(sequence=sequence, at_seconds=at, flow_id=flow_id,
                          action=action, offered_mbps=rate)
            for sequence, (at, _, _, flow_id, action, rate) in enumerate(sorted(pending))]


class ResolvedWorkload(DynamicWorkload):
    """Explicit, JSON-serializable intended schedule, with no measurement fields."""
    events: list[WorkloadEvent] = Field(min_length=2)

    @model_validator(mode='after')
    def canonical_schedule(self):
        if self.events != _compile_events(self.flows):
            raise ValueError('Resolved events must match the canonical flow schedule and sequence')
        return self


def resolve_workload(workload, manifest):
    """Validate demand and inventory, then resolve effective offered-rate events.

    Same-time events sort by stop, set_rate, start, then input flow order.
    Bursts override sustained rates; changes hidden by a burst become effective
    when it ends. No-op rate updates are omitted. There is no clock or backend.
    """
    # Reparse model dumps too: callers may have mutated an already parsed model.
    value = workload.model_dump() if isinstance(workload, DynamicWorkload) else workload
    parsed = DynamicWorkload.model_validate(value).validate_inventory(manifest)
    return ResolvedWorkload(**parsed.model_dump(), events=_compile_events(parsed.flows))


class WorkloadGenerator(Contract):
    """Regular arrivals with locally randomized endpoints and uniform rates.

    Only free destination/port pairs are sampled; concurrency is never silently
    reduced and requested starts/durations are never moved to make a plan fit.
    """
    duration_seconds: float = Field(gt=0, le=86400)
    seed: int | None = None
    hosts: list[str] = Field(min_length=2)
    flow_count: int = Field(ge=1)
    arrival_interval_seconds: float = Field(gt=0)
    flow_duration_seconds: float = Field(gt=0)
    min_offered_mbps: OfferedMbps
    max_offered_mbps: OfferedMbps
    destination_port: int = Field(default=5201, ge=1, le=65535)

    @model_validator(mode='after')
    def bounds(self):
        if len(set(self.hosts)) != len(self.hosts):
            raise ValueError('Generator hosts must be unique')
        if self.min_offered_mbps > self.max_offered_mbps:
            raise ValueError('Minimum offered rate exceeds maximum')
        if ((self.flow_count - 1) * self.arrival_interval_seconds + self.flow_duration_seconds
                > self.duration_seconds):
            raise ValueError('Generated flows exceed total experiment duration')
        return self


def generate_workload(specification, manifest):
    """Return explicit flows; save their resolved schedule for exact replay.

    Host candidates are sorted so manifest/host-list insertion order cannot
    affect seeded output. Never reads or reseeds the global random generator.
    """
    value = (specification.model_dump() if isinstance(specification, WorkloadGenerator)
             else specification)
    spec = WorkloadGenerator.model_validate(value)
    routers = manifest['routers']
    if set(spec.hosts) - set(routers):
        raise ValueError('Generator requires known hosts')
    hosts = sorted(spec.hosts)
    rng = random.Random(spec.seed)
    flows = []
    active = []
    for index in range(spec.flow_count):
        start = index * spec.arrival_interval_seconds
        active = [flow for flow in active if flow.stop_seconds > start]
        if len(active) >= MAX_CONCURRENT_FLOWS:
            raise ValueError(f'At most {MAX_CONCURRENT_FLOWS} concurrent flows are supported')
        occupied = {routers[flow.destination]['host'] for flow in active}
        candidates = [(source, destination) for source in hosts for destination in hosts
                      if routers[source]['host'] != routers[destination]['host']
                      and routers[destination]['host'] not in occupied]
        if not candidates:
            raise ValueError('No free destination host/port for generated arrival')
        source, destination = rng.choice(candidates)
        rate = rng.uniform(spec.min_offered_mbps, spec.max_offered_mbps)
        flow = DynamicFlow(flow_id=f'generated-{index + 1}', source=source, destination=destination,
                           destination_port=spec.destination_port, offered_mbps=rate,
                           start_seconds=start, stop_seconds=start + spec.flow_duration_seconds)
        flows.append(flow)
        active.append(flow)
    return DynamicWorkload(duration_seconds=spec.duration_seconds, seed=spec.seed,
                           flows=flows).validate_inventory(manifest)
