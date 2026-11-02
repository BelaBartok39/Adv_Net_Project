"""JSON contracts shared with the collector, verifier, and deployment controller."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DeviceState(Contract):
    interfaces: list[str] = Field(min_length=1)
    running_config: str
    telemetry: dict[str, JsonValue] = Field(default_factory=dict)


class Observation(Contract):
    schema_version: Literal["1"] = "1"
    scenario_id: str = Field(min_length=1)
    observed_at: datetime
    objective: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    topology: list[str]
    devices: dict[str, DeviceState] = Field(min_length=1)
    measurements: dict[str, JsonValue]

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class Change(Contract):
    device: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    interface: str = Field(pattern=r"^Ethernet[1-9][0-9]*(?:/[1-9][0-9]*)*$")


class CostChange(Change):
    action: Literal["set_ospf_cost"]
    value: int = Field(ge=1, le=65535)


class AreaChange(Change):
    action: Literal["set_ospf_area"]
    value: str

    @field_validator("value")
    @classmethod
    def valid_area(cls, value):
        from ipaddress import IPv4Address
        return str(IPv4Address(value))


class AdminChange(Change):
    action: Literal["set_interface_enabled"]
    value: bool


Patch = Annotated[CostChange | AreaChange | AdminChange, Field(discriminator="action")]


class Proposal(Contract):
    outcome: Literal["candidate", "no_change", "needs_observation"]
    diagnosis: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    changes: list[Patch] = Field(default_factory=list, max_length=12)
    expected_effect: str = Field(min_length=1)
    observation_requests: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def consistent_outcome(self):
        if (self.outcome == "candidate") != bool(self.changes):
            raise ValueError("Only candidate outcomes have changes; candidates need at least one")
        if self.outcome == "needs_observation" and not self.observation_requests:
            raise ValueError("needs_observation requires observation_requests")
        keys = [(c.device, c.interface, c.action) for c in self.changes]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate or conflicting changes to the same setting")
        return self

    def check_scope(self, observation: Observation):
        for change in self.changes:
            device = observation.devices.get(change.device)
            if device is None or change.interface not in device.interfaces:
                raise ValueError(f"Unknown device/interface: {change.device}/{change.interface}")


class CatalogAction(Contract):
    description: str = Field(min_length=1)
    proposal: Proposal

    @field_validator("proposal")
    @classmethod
    def candidate_only(cls, value):
        if value.outcome != "candidate":
            raise ValueError("Catalog actions must be candidates")
        return value


class Feedback(Contract):
    attempt: int = Field(ge=1, le=5)
    candidate_id: str = Field(min_length=1)
    stage: Literal["decision", "verification", "runtime"]
    accepted: bool
    details: list[str] = Field(min_length=1)
    measurements: dict[str, JsonValue] = Field(default_factory=dict)
    rollback_succeeded: bool | None = None


def render_eos(proposal: Proposal) -> dict[str, list[str]]:
    """Render reviewed typed patches, without executing commands or saving config."""
    commands: dict[str, list[str]] = {}
    for change in proposal.changes:
        device = commands.setdefault(change.device, [])
        device.append(f"interface {change.interface}")
        match change.action:
            case "set_ospf_cost":
                device.append(f"ip ospf cost {change.value}")
            case "set_ospf_area":
                device.append(f"ip ospf area {change.value}")
            case "set_interface_enabled":
                device.append("no shutdown" if change.value else "shutdown")
        device.append("exit")
    return commands
