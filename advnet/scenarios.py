"""Validated private fault definitions and post-trial routing evaluation."""
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .catalog import catalog_hash
from .contracts import Contract, Proposal


MAX_CONCURRENT_FLOWS = 4


class TrafficFlow(Contract):
    source: str
    destination: str
    seconds: int = Field(default=12,ge=1,le=60)
    offered_mbps: float = Field(gt=0,le=1000)


class ConcurrentFlow(TrafficFlow):
    destination_port: int = Field(default=5201,ge=1,le=65535)


class TrafficProfile(Contract):
    flow: TrafficFlow | None = None
    flows: list[ConcurrentFlow] | None = Field(default=None,min_length=2,max_length=MAX_CONCURRENT_FLOWS)

    @model_validator(mode='after')
    def one_workload(self):
        if (self.flow is None) == (self.flows is None):
            raise ValueError('Specify exactly one of flow or flows')
        return self


def validate_flow_endpoints(flows,routers):
    destinations=set()
    for flow in flows:
        source,destination=flow['source'],flow['destination']
        if source not in routers or destination not in routers or source==destination:
            raise ValueError('Traffic requires distinct known source/destination hosts')
        endpoint=(routers[destination]['host'],flow.get('destination_port',5201))
        if endpoint in destinations:
            raise ValueError('Concurrent flows cannot share a destination host/port; iperf3 serves one client at a time')
        destinations.add(endpoint)


def load_traffic_profile(path,manifest):
    profile=TrafficProfile.model_validate_json(Path(path).read_text())
    values=profile.model_dump(mode='json',exclude_none=True)
    validate_flow_endpoints(values.get('flows') or [values['flow']],manifest['routers'])
    return values


class ScenarioEvaluation(Contract):
    family: str
    expected_route: Literal['catalog','escalate']
    expected_actions: list[str] = Field(default_factory=list)
    catalog_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    explanation: str

    @model_validator(mode='after')
    def route_actions(self):
        if (self.expected_route=='catalog') != bool(self.expected_actions):
            raise ValueError('Only catalog-eligible cases specify expected actions')
        return self


class Scenario(Contract):
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9-]*$')
    objective: Literal['repair','optimization']
    objective_text: str = Field(min_length=1)
    fault: Proposal | None = None
    flow: TrafficFlow | None = None
    flows: list[ConcurrentFlow] | None = Field(default=None,min_length=2,max_length=MAX_CONCURRENT_FLOWS)
    capacities_mbps: dict[str,float] = Field(default_factory=dict)
    evaluation: ScenarioEvaluation | None = None
    reference_repair: Proposal | None = None

    @model_validator(mode='after')
    def impairment(self):
        if self.flow is not None and self.flows is not None:
            raise ValueError('Specify either flow or flows, not both')
        if self.objective=='repair' and self.fault is None:
            raise ValueError('Repair scenarios need a configuration fault')
        if self.objective=='optimization' and ((self.flow is None and self.flows is None) or not (self.fault or self.capacities_mbps)):
            raise ValueError('Optimization scenarios need a flow and an impairment')
        for proposal in (self.fault,self.reference_repair):
            if proposal is not None and proposal.outcome!='candidate':
                raise ValueError('Fault and reference repair must contain candidate changes')
        for flow in ([self.flow] if self.flow else self.flows or []):
            if flow.source==flow.destination:
                raise ValueError('Traffic endpoints must differ')
        if any(not 0<rate<=1000 for rate in self.capacities_mbps.values()):
            raise ValueError('Capacities must be finite and in (0, 1000] Mbps')
        return self


def load_scenario(path,manifest,catalog):
    scenario=Scenario.model_validate_json(Path(path).read_text())
    for proposal in (scenario.fault,scenario.reference_repair):
        if proposal:
            for change in proposal.changes:
                router=manifest['routers'].get(change.device)
                if router is None or change.interface not in router['interfaces']:
                    raise ValueError('Scenario changes an unknown device/interface')
    flows=[flow.model_dump() for flow in ([scenario.flow] if scenario.flow else scenario.flows or [])]
    validate_flow_endpoints(flows,manifest['routers'])
    if set(scenario.capacities_mbps)-{link['id'] for link in manifest['links']}:
        raise ValueError('Unknown shaped link')
    if scenario.evaluation:
        if scenario.evaluation.catalog_sha256!=catalog_hash(catalog):
            raise ValueError('Scenario routing labels were defined for a different catalog')
        if set(scenario.evaluation.expected_actions)-set(catalog):
            raise ValueError('Expected action is absent from the shared catalog')
    return scenario.model_dump(mode='json',exclude_none=True)


def routing_evaluation(result):
    """Grade the first decision only. Never feed these private labels to a model."""
    expected=result.get('evaluation') or {}
    decisions=result.get('decisions') or []
    screen=decisions[0].get('screening',{}) if decisions else {}
    selected=screen.get('selected')
    effective=screen.get('effective_route')
    forced=bool(result.get('decision_settings',{}).get('forced_escalation',False))
    scored=bool(expected and decisions and result.get('system_one_enabled') and not forced)
    allowed=expected.get('expected_actions',[]) if expected.get('expected_route')=='catalog' else ['escalate']
    return {'scenario_family':expected.get('family'),'expected_route':expected.get('expected_route'),
            'forced_escalation':forced,'selected_route':selected,'effective_route':effective,
            'classifier_route_correct':selected in allowed if scored and selected is not None else None,
            'effective_route_correct':effective in allowed if scored and effective is not None else None,
            'unnecessary_escalation':effective=='escalate' if scored and expected.get('expected_route')=='catalog' else None,
            'incorrect_catalog_choice':effective not in (None,'escalate','no_change') if scored and expected.get('expected_route')=='escalate' else None,
            'incorrect_no_change':effective=='no_change' if scored else None,
            'screening_error':screen.get('error_type')}
