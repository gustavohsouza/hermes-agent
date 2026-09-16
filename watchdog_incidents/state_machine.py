from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Lifecycle(str, Enum):
    OPEN = "open"
    GONE = "gone"


class TransitionKind(str, Enum):
    NEW = "new"
    DUPLICATE_NEW = "duplicate_new"
    UNCHANGED_ACTIVE = "unchanged_active"
    RECOVERED_ACTIVE = "recovered_active"
    GONE = "gone"
    DUPLICATE_GONE = "duplicate_gone"
    GONE_ORPHAN = "gone_orphan"
    RECURRENCE_REOPEN = "recurrence_reopen"
    RECURRENCE_LINKED = "recurrence_linked"
    HEARTBEAT = "heartbeat"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class IncidentState:
    stable_key: str
    lifecycle: Lifecycle
    incident_id: str
    latest_applied_at: str
    latest_applied_event_id: str
    latest_applied_digest: str
    recurrence_root_event_id: str
    recurrence_version: int = 0


@dataclass(frozen=True)
class Transition:
    stable_key: str
    kind: TransitionKind
    action: str | None = None
    state_changed: bool = False
    linked_incident_id: str | None = None
    diagnostic: str | None = None


class StateMachine:
    """Pure in-memory model; callers persist state and execute actions separately."""

    def __init__(self, *, reopen_supported: bool = True, states: Iterable[IncidentState] = ()):
        self.reopen_supported = reopen_supported
        self.states = {state.stable_key: state for state in states}

    def apply(
        self,
        *,
        observed_at: str,
        event_id: str,
        digest: str,
        active_keys: Iterable[str] = (),
        new_keys: Iterable[str] = (),
        gone_keys: Iterable[str] = (),
    ) -> list[Transition]:
        active = set(active_keys)
        new = set(new_keys)
        gone = set(gone_keys)
        if not (new <= active and not (gone & active) and not (new & gone)):
            raise ValueError("invalid active/new/gone key relation")
        if not (active or gone):
            return [Transition("", TransitionKind.HEARTBEAT)]

        transitions: list[Transition] = []
        for key in sorted(active | gone):
            state = self.states.get(key)
            ordering = self._ordering(state, observed_at, digest)
            if ordering is not None:
                transitions.append(Transition(key, ordering))
                continue
            if key in gone:
                transitions.append(self._apply_gone(key, state, observed_at, event_id, digest))
            elif key in new:
                transitions.append(self._apply_new(key, state, observed_at, event_id, digest))
            else:
                transitions.append(self._apply_active(key, state, observed_at, event_id, digest))
        return transitions

    @staticmethod
    def _ordering(state: IncidentState | None, observed_at: str, digest: str) -> TransitionKind | None:
        if state is None:
            return None
        if observed_at < state.latest_applied_at:
            return TransitionKind.STALE
        if observed_at == state.latest_applied_at:
            if digest == state.latest_applied_digest:
                return TransitionKind.STALE
            return TransitionKind.CONFLICT
        return None

    def _apply_new(self, key: str, state: IncidentState | None, at: str, event_id: str, digest: str) -> Transition:
        if state is None:
            self._set_open(key, at, event_id, digest, recurrence_root_event_id=event_id)
            return Transition(key, TransitionKind.NEW, "create", True)
        if state.lifecycle is Lifecycle.OPEN:
            return Transition(key, TransitionKind.DUPLICATE_NEW)
        return self._recur(key, state, at, event_id, digest)

    def _apply_active(self, key: str, state: IncidentState | None, at: str, event_id: str, digest: str) -> Transition:
        if state is None:
            self._set_open(key, at, event_id, digest, recurrence_root_event_id=event_id)
            return Transition(key, TransitionKind.RECOVERED_ACTIVE, "create", True, diagnostic="active_without_known_incident")
        if state.lifecycle is Lifecycle.OPEN:
            return Transition(key, TransitionKind.UNCHANGED_ACTIVE)
        return self._recur(key, state, at, event_id, digest)

    def _apply_gone(self, key: str, state: IncidentState | None, at: str, event_id: str, digest: str) -> Transition:
        if state is None:
            return Transition(key, TransitionKind.GONE_ORPHAN, diagnostic="gone_without_known_incident")
        if state.lifecycle is Lifecycle.GONE:
            return Transition(key, TransitionKind.DUPLICATE_GONE)
        self.states[key] = IncidentState(
            **{**state.__dict__, "lifecycle": Lifecycle.GONE, "latest_applied_at": at,
               "latest_applied_event_id": event_id, "latest_applied_digest": digest}
        )
        return Transition(key, TransitionKind.GONE, "wake_update", True)

    def _recur(self, key: str, state: IncidentState, at: str, event_id: str, digest: str) -> Transition:
        version = state.recurrence_version + 1
        if self.reopen_supported:
            self._set_open(key, at, event_id, digest, incident_id=state.incident_id,
                           recurrence_root_event_id=state.recurrence_root_event_id, recurrence_version=version)
            return Transition(key, TransitionKind.RECURRENCE_REOPEN, "reopen", True)
        new_incident = self._incident_id(key, version)
        self._set_open(key, at, event_id, digest, incident_id=new_incident,
                       recurrence_root_event_id=state.recurrence_root_event_id, recurrence_version=version)
        return Transition(key, TransitionKind.RECURRENCE_LINKED, "create_linked", True, state.incident_id)

    def _set_open(self, key: str, at: str, event_id: str, digest: str, *, incident_id: str | None = None,
                  recurrence_root_event_id: str, recurrence_version: int = 0) -> None:
        self.states[key] = IncidentState(
            stable_key=key,
            lifecycle=Lifecycle.OPEN,
            incident_id=incident_id or self._incident_id(key, recurrence_version),
            latest_applied_at=at,
            latest_applied_event_id=event_id,
            latest_applied_digest=digest,
            recurrence_root_event_id=recurrence_root_event_id,
            recurrence_version=recurrence_version,
        )

    @staticmethod
    def _incident_id(key: str, version: int) -> str:
        return f"wd-incident-v1-{key}-r{version}"
