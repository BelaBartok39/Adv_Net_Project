"""Translate Monitor.sample() plus separately collected health and capacity evidence."""
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, Field

from .contracts import Contract, DeviceState, Observation
from .stability_policy import HealthMeasurement, LinkMeasurement, NetworkObservation


@dataclass
class Frame:
    source: str
    signal: NetworkObservation
    model_input: Observation | None = None
    runtime: dict | None = None


class CapacityEvidence(Contract):
    mbps: float = Field(gt=0)
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    evidence: str = Field(min_length=1)


def monitor_frame(raw, *, observation_id, generation, rate_window_start=None,
                  capacities=None, reachability=None, ospf=None, model_input=None,
                  manifest=None, max_config_age=30):
    """generation belongs to this counter collection. Preserve cached health tags.

    Only use a capacity which covers the whole counter interval. Health inputs
    must be separately timestamped, scoped checks. Monitor's container status
    and aggregate link state do not establish either required health check.
    """
    at = datetime.fromisoformat(raw['sampled_at'])
    links = []
    rows = raw['links']
    if manifest is not None:
        def endpoints(row):
            return tuple(row[key] for key in ('a', 'a_interface', 'b', 'b_interface'))
        measured = {endpoints(row): row for row in rows}
        inventory = {endpoints(row) for row in manifest['links']}
        if len(measured) != len(rows) or not measured.keys() <= inventory:
            raise ValueError('Unexpected or duplicate measured links')
        rows = [measured.get(endpoints(row), {k: row[k] for k in ('a', 'a_interface', 'b', 'b_interface')})
                for row in manifest['links']]
    for row in rows:
        for side in ('a', 'b'):
            key = f'{row[side]}:{row[side + "_interface"]}'
            rate = row.get(side + '_mbps')
            capacity = (capacities or {}).get(key)
            fields = {}
            if capacity is not None:
                capacity = CapacityEvidence.model_validate(capacity)
                if rate_window_start is not None and capacity.valid_from <= rate_window_start < at <= capacity.valid_until:
                    fields.update(effective_capacity_mbps=capacity.mbps, capacity_evidence=capacity.evidence)
            links.append(LinkMeasurement(link_id=key, observed_at=at, measurement_generation=generation,
                                         throughput_mbps=rate, throughput_evidence='Monitor interface counters'
                                         if rate is not None else None, **fields))

    def scoped(sample):
        if sample is None:
            return None
        value = HealthMeasurement.model_validate(sample)
        return value if value.scope_fingerprint else None

    signal = NetworkObservation(observation_id=observation_id, observed_at=at, links=links,
                                reachability=scoped(reachability), ospf=scoped(ospf))
    if manifest is not None and model_input is None:
        devices = {}
        for name, inventory in manifest['routers'].items():
            node = raw['nodes'].get(name, {})
            if not node.get('config') or node.get('config_error') or not node.get('config_at'):
                break
            config_at = datetime.fromisoformat(node['config_at'])
            if not 0 <= (at-config_at).total_seconds() <= max_config_age:
                break
            devices[name] = DeviceState(interfaces=inventory['interfaces'], running_config=node['config'],
                                        telemetry={'config_at': config_at.isoformat()})
        if devices and devices.keys() == manifest['routers'].keys():
            topology = [f"{r['a']}:{r['a_interface']} -- {r['b']}:{r['b_interface']}" for r in manifest['links']]
            topology.extend(f"{n}:{r['host_interface']} -- {r['host']}:eth1"
                            for n, r in manifest['routers'].items())
            model_input = Observation(scenario_id=observation_id, observed_at=at, devices=devices, topology=topology,
                                      objective='Restore connectivity and OSPF health or relieve sustained congestion',
                                      requirements=manifest['requirements'], measurements=signal.model_dump(mode='json'))
    return Frame(raw['source'], signal, model_input)
