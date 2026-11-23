"""Execute resolved demand against a nonblocking, injectable traffic backend.

There is no Docker/iperf transport here. Backend operations must respect their
timeout, start must return without waiting for the flow lifetime, and poll must
only inspect current state. Each backend instance owns one run's resources.
"""
import math
import time
from typing import Literal, Protocol

from pydantic import Field

from .contracts import Contract
from .dynamic_workload import DynamicFlow, ResolvedWorkload, WorkloadEvent


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


class FlowExit(Contract):
    returncode: int
    error: str | None = None


class TrafficBackend(Protocol):
    """Bounded control operations, never a wait for a flow's full duration.

    start registers ownership before allocating resources (even if it raises).
    stop returns only after its receiver endpoint is free for reuse. set_rate
    retains flow identity and leaves the flow available for subsequent calls.
    poll returns None while running; any exit before scheduled stop is early.
    cleanup must attempt all owned resources, including partially failed starts,
    and raise if any remain. It is called even after successful scheduled stops.
    """
    def start(self, flow: DynamicFlow, offered_mbps: float, *, timeout_seconds: float) -> None: ...
    def set_rate(self, flow_id: str, offered_mbps: float, *, timeout_seconds: float) -> None: ...
    def stop(self, flow_id: str, *, timeout_seconds: float) -> None: ...
    def poll(self, flow_id: str, *, timeout_seconds: float) -> FlowExit | None: ...
    def cleanup(self, *, timeout_seconds: float) -> None: ...


class ExecutionError(Contract):
    error_type: str
    message: str
    flow_id: str | None = None


class EventExecution(Contract):
    intended: WorkloadEvent
    actual_seconds: float | None = None
    finished_seconds: float | None = None
    lateness_seconds: float | None = None
    status: Literal['pending', 'completed', 'failed', 'skipped'] = 'pending'
    error: ExecutionError | None = None


class FlowExitRecord(Contract):
    flow_id: str
    at_seconds: float
    exit: FlowExit


class ScheduleResult(Contract):
    status: Literal['completed', 'failed', 'timed_out', 'cancelled', 'interrupted'] = 'completed'
    duration_seconds: float
    timeout_seconds: float
    events: list[EventExecution]
    flow_exits: list[FlowExitRecord] = Field(default_factory=list)
    error: ExecutionError | None = None
    cleanup_succeeded: bool = False
    cleanup_error: ExecutionError | None = None
    cleanup_started_seconds: float | None = None
    cleanup_finished_seconds: float | None = None
    finished_seconds: float | None = None


class ScheduleInterrupted(KeyboardInterrupt):
    """Retain the partial result while preserving KeyboardInterrupt semantics."""
    def __init__(self, result):
        super().__init__('Dynamic schedule interrupted; cleanup attempted')
        self.result = result


class ScheduleCancelled(Exception):
    pass


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be finite and positive')
    return value


def _error(error, flow_id=None):
    return ExecutionError(error_type=type(error).__name__, message=str(error), flow_id=flow_id)


def run_schedule(schedule, manifest, backend: TrafficBackend, *, clock: Clock | None = None,
                 cancelled=None, timeout_seconds=None, operation_timeout_seconds=5,
                 cleanup_timeout_seconds=10, poll_interval_seconds=0.1):
    """Run one validated schedule, returning timings and partial failure evidence.

    Timing origin is one monotonic reading taken before the first backend call.
    Actual/finished times bound backend control calls, not packet transmission.
    Calls run in canonical event order. Sleeps/polls never wait for a flow to
    finish. Controls are checked at every event and at most each poll interval
    while idle, plus backend control-call latency. Short call latency may delay
    dispatch; it never moves the intended timeline or resets its origin.

    timeout_seconds is an exclusive run budget relative to that origin; default
    is workload duration + one operation timeout, allowing the final stop at the
    duration boundary. Cleanup has its own budget and ignores cancellation.
    Operational failures abort further events and return a result. KeyboardInterrupt
    raises ScheduleInterrupted with that result after cleanup; SystemExit is
    re-raised after cleanup with schedule_result attached. Invalid inputs fail
    before touching the backend.
    """
    value = schedule.model_dump() if isinstance(schedule, ResolvedWorkload) else schedule
    resolved = ResolvedWorkload.model_validate(value).validate_inventory(manifest)
    operation_timeout_seconds = _positive(operation_timeout_seconds, 'operation_timeout_seconds')
    cleanup_timeout_seconds = _positive(cleanup_timeout_seconds, 'cleanup_timeout_seconds')
    poll_interval_seconds = _positive(poll_interval_seconds, 'poll_interval_seconds')
    budget = _positive(resolved.duration_seconds + operation_timeout_seconds
                       if timeout_seconds is None else timeout_seconds, 'timeout_seconds')
    clock = SystemClock() if clock is None else clock
    if cancelled is not None and not callable(cancelled):
        raise ValueError('cancelled must be a callable, such as threading.Event.is_set')
    origin = clock.monotonic()
    if type(origin) not in (int, float) or not math.isfinite(origin):
        raise ValueError('Clock must return finite monotonic seconds')
    deadline = origin + budget
    last_time = origin
    active = set()
    flows = {flow.flow_id: flow for flow in resolved.flows}
    report = ScheduleResult(duration_seconds=resolved.duration_seconds, timeout_seconds=budget,
                            events=[EventExecution(intended=event) for event in resolved.events])
    interruption = None
    context_flow = None

    def elapsed():
        nonlocal last_time
        current = clock.monotonic()
        if (type(current) not in (int, float) or not math.isfinite(current)
                or current < last_time):
            raise RuntimeError('Clock must return finite, nondecreasing monotonic seconds')
        last_time = current
        return current - origin

    def recorded_time():
        # A broken injected clock must not prevent cleanup or mask the failure.
        try:
            return elapsed()
        except Exception as error:
            if report.error is None:
                report.error = _error(error)
            if report.status == 'completed':
                report.status = 'failed'
            return None

    def check_controls():
        if cancelled is not None and cancelled():
            raise ScheduleCancelled('Dynamic schedule cancelled')
        elapsed()
        remaining = deadline - last_time
        if remaining <= 0:
            raise TimeoutError('Dynamic schedule deadline exceeded')
        return min(operation_timeout_seconds, remaining)

    def monitor():
        nonlocal context_flow
        # Inventory order is not execution order; poll in input flow order.
        for flow_id in flows:
            if flow_id not in active:
                continue
            context_flow = flow_id
            limit = check_controls()
            before = elapsed()
            exit_status = backend.poll(flow_id, timeout_seconds=limit)
            if elapsed() - before > limit:
                raise TimeoutError('Backend poll exceeded its operation timeout')
            check_controls()
            if exit_status is not None:
                exit_status = FlowExit.model_validate(exit_status)
                report.flow_exits.append(FlowExitRecord(flow_id=flow_id, at_seconds=elapsed(), exit=exit_status))
                raise RuntimeError(f'Flow {flow_id} exited before scheduled stop '
                                   f'(returncode={exit_status.returncode}): {exit_status.error or "early exit"}')
        context_flow = None

    def wait_until(target):
        target_time = origin + target
        while True:
            check_controls()
            if last_time >= target_time:
                return
            monitor()
            elapsed()
            delay = min(poll_interval_seconds, target_time - last_time, deadline - last_time)
            if delay > 0:
                clock.sleep(delay)

    try:
        for record in report.events:
            event = record.intended
            wait_until(event.at_seconds)
            context_flow = event.flow_id
            limit = check_controls()
            record.actual_seconds = elapsed()
            record.lateness_seconds = max(0, record.actual_seconds - event.at_seconds)
            try:
                if event.action != 'stop' and last_time >= origin + flows[event.flow_id].stop_seconds:
                    raise TimeoutError('Flow lifetime elapsed before event could be executed')
                if event.action == 'start':
                    backend.start(flows[event.flow_id].model_copy(deep=True), event.offered_mbps,
                                  timeout_seconds=limit)
                    active.add(event.flow_id)
                elif event.action == 'set_rate':
                    backend.set_rate(event.flow_id, event.offered_mbps, timeout_seconds=limit)
                else:
                    backend.stop(event.flow_id, timeout_seconds=limit)
                    active.remove(event.flow_id)
                if elapsed() - record.actual_seconds > limit:
                    raise TimeoutError('Backend control call exceeded its operation timeout')
                check_controls()
                record.status = 'completed'
            except BaseException as error:
                record.status = 'failed'
                record.error = _error(error, event.flow_id)
                raise
            finally:
                record.finished_seconds = recorded_time()
            context_flow = None
        # Preserve the declared experiment duration, including an idle tail.
        wait_until(resolved.duration_seconds)
    except BaseException as error:
        report.error = _error(error, context_flow)
        report.status = ('cancelled' if isinstance(error, ScheduleCancelled) else
                         'timed_out' if isinstance(error, TimeoutError) else
                         'interrupted' if not isinstance(error, Exception) else 'failed')
        if not isinstance(error, Exception):
            interruption = error
    finally:
        report.cleanup_started_seconds = recorded_time()
        try:
            backend.cleanup(timeout_seconds=cleanup_timeout_seconds)
            finished = recorded_time()
            if (finished is not None and report.cleanup_started_seconds is not None
                    and finished - report.cleanup_started_seconds > cleanup_timeout_seconds):
                raise TimeoutError('Backend cleanup exceeded its operation timeout')
            report.cleanup_succeeded = True
        except BaseException as error:
            report.cleanup_error = _error(error)
            if report.status == 'completed':
                report.status = 'failed' if isinstance(error, Exception) else 'interrupted'
            if not isinstance(error, Exception) and interruption is None:
                interruption = error
        report.cleanup_finished_seconds = recorded_time()
        report.finished_seconds = report.cleanup_finished_seconds
        for record in report.events:
            if record.status == 'pending':
                record.status = 'skipped'
    if interruption is not None:
        if isinstance(interruption, KeyboardInterrupt):
            raise ScheduleInterrupted(report) from interruption
        interruption.schedule_result = report
        raise interruption
    return report
