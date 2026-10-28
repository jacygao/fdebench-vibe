"""Public signal and decision contracts."""

from enum import StrEnum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)

ActionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class Category(StrEnum):
    CREW_ACCESS = "Crew Access & Biometrics"
    STRUCTURAL = "Hull & Structural Systems"
    COMMUNICATIONS = "Communications & Navigation"
    SOFTWARE = "Flight Software & Instruments"
    THREAT = "Threat Detection & Containment"
    DATA = "Telemetry & Data Banks"
    BRIEFING = "Mission Briefing Request"
    NOISE = "Not a Mission Signal"


class Priority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class Team(StrEnum):
    IDENTITY = "Crew Identity & Airlock Control"
    SYSTEMS = "Spacecraft Systems Engineering"
    COMMUNICATIONS = "Deep Space Communications"
    SOFTWARE = "Mission Software Operations"
    THREAT = "Threat Response Command"
    DATA = "Telemetry & Data Core"
    NONE = "None"


class MissingInfo(StrEnum):
    AFFECTED_SUBSYSTEM = "affected_subsystem"
    ANOMALY_READOUT = "anomaly_readout"
    SEQUENCE_TO_REPRODUCE = "sequence_to_reproduce"
    AFFECTED_CREW = "affected_crew"
    HABITAT_CONDITIONS = "habitat_conditions"
    STARDATE = "stardate"
    PREVIOUS_SIGNAL_ID = "previous_signal_id"
    CREW_CONTACT = "crew_contact"
    MODULE_SPECS = "module_specs"
    SOFTWARE_VERSION = "software_version"
    SECTOR_COORDINATES = "sector_coordinates"
    MISSION_IMPACT = "mission_impact"
    RECURRENCE_PATTERN = "recurrence_pattern"
    SENSOR_LOG_OR_CAPTURE = "sensor_log_or_capture"
    BIOMETRIC_METHOD = "biometric_method"
    SYSTEM_CONFIGURATION = "system_configuration"


class Channel(StrEnum):
    RELAY = "subspace_relay"
    COMM = "holodeck_comm"
    TERMINAL = "bridge_terminal"
    BEACON = "emergency_beacon"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reporter(ContractModel):
    name: str = Field(max_length=200)
    email: EmailStr
    department: str = Field(max_length=200)


class Signal(ContractModel):
    ticket_id: str = Field(pattern=r"^SIG-[0-9]+$", max_length=80)
    subject: str = Field(max_length=1000)
    description: str = Field(max_length=20000)
    reporter: Reporter
    created_at: AwareDatetime
    channel: Channel
    attachments: list[Annotated[str, Field(max_length=255)]] = Field(
        default_factory=list, max_length=30
    )


class TriageDecision(ContractModel):
    ticket_id: str = Field(pattern=r"^SIG-[0-9]+$", max_length=80)
    category: Category
    priority: Priority
    assigned_team: Team
    needs_escalation: bool = Field(strict=True)
    missing_information: list[MissingInfo] = Field(max_length=16)
    next_best_action: ActionText
    remediation_steps: list[ActionText] = Field(max_length=10)

    @model_validator(mode="after")
    def validate_routing(self) -> "TriageDecision":
        """Reject contradictory noise routing and duplicate information requests."""
        if (self.category == Category.NOISE) != (self.assigned_team == Team.NONE):
            raise ValueError("Only non-mission signals may be assigned to None")
        if self.category == Category.NOISE and (
            self.priority != Priority.P4
            or self.needs_escalation
            or self.missing_information
            or self.remediation_steps
        ):
            raise ValueError(
                "Non-mission noise must be P4 without operational actions or escalation"
            )
        if len(set(self.missing_information)) != len(self.missing_information):
            raise ValueError("Missing information must not contain duplicates")
        return self
