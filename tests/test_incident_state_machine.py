from __future__ import annotations

from datetime import datetime, timezone
import unittest

from watchdog_incidents.state_machine import (
    IncidentState,
    Lifecycle,
    StateMachine,
    TransitionKind,
)


class IncidentStateMachineTests(unittest.TestCase):
    def at(self, second: int) -> str:
        return datetime(2026, 9, 9, 20, 0, second, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    def apply(self, machine: StateMachine, second: int, *, active=(), new=(), gone=(), event_id=None, digest=None):
        return machine.apply(
            observed_at=self.at(second),
            event_id=event_id or f"event-{second}",
            digest=digest or f"digest-{second}",
            active_keys=active,
            new_keys=new,
            gone_keys=gone,
        )

    def test_transition_table(self):
        cases = [
            ("new", True, [(1, ("key",), ("key",), ())], TransitionKind.NEW),
            ("duplicate new", True, [(1, ("key",), ("key",), ()), (2, ("key",), ("key",), ())], TransitionKind.DUPLICATE_NEW),
            ("unchanged active", True, [(1, ("key",), ("key",), ()), (2, ("key",), (), ())], TransitionKind.UNCHANGED_ACTIVE),
            ("recovered active", True, [(1, ("key",), (), ())], TransitionKind.RECOVERED_ACTIVE),
            ("gone", True, [(1, ("key",), ("key",), ()), (2, (), (), ("key",))], TransitionKind.GONE),
            ("duplicate gone", True, [(1, ("key",), ("key",), ()), (2, (), (), ("key",)), (3, (), (), ("key",))], TransitionKind.DUPLICATE_GONE),
            ("orphan gone", True, [(1, (), (), ("key",))], TransitionKind.GONE_ORPHAN),
            ("recurrence reopen", True, [(1, ("key",), ("key",), ()), (2, (), (), ("key",)), (3, ("key",), ("key",), ())], TransitionKind.RECURRENCE_REOPEN),
            ("linked recurrence", False, [(1, ("key",), ("key",), ()), (2, (), (), ("key",)), (3, ("key",), ("key",), ())], TransitionKind.RECURRENCE_LINKED),
            ("heartbeat", True, [(1, (), (), ())], TransitionKind.HEARTBEAT),
        ]
        for name, reopen_supported, events, expected in cases:
            with self.subTest(name=name):
                machine = StateMachine(reopen_supported=reopen_supported)
                result = []
                for second, active, new, gone in events:
                    result = self.apply(machine, second, active=active, new=new, gone=gone)
                self.assertEqual([expected], [item.kind for item in result])

    def test_new_creates_open_incident(self):
        machine = StateMachine()
        result = self.apply(machine, 1, active=("proxy-erros",), new=("proxy-erros",))
        self.assertEqual([TransitionKind.NEW], [item.kind for item in result])
        self.assertEqual(Lifecycle.OPEN, machine.states["proxy-erros"].lifecycle)
        self.assertEqual(0, machine.states["proxy-erros"].recurrence_version)

    def test_unchanged_active_is_noop(self):
        machine = StateMachine()
        self.apply(machine, 1, active=("proxy-erros",), new=("proxy-erros",))
        result = self.apply(machine, 2, active=("proxy-erros",))
        self.assertEqual([TransitionKind.UNCHANGED_ACTIVE], [item.kind for item in result])
        self.assertFalse(result[0].state_changed)
        self.assertIsNone(result[0].action)
        self.assertEqual(self.at(1), machine.states["proxy-erros"].latest_applied_at)

    def test_gone_wakes_but_never_closes_matching_incident(self):
        machine = StateMachine()
        self.apply(machine, 1, active=("proxy-erros",), new=("proxy-erros",))
        result = self.apply(machine, 2, gone=("proxy-erros",))
        self.assertEqual([TransitionKind.GONE], [item.kind for item in result])
        self.assertEqual("wake_update", result[0].action)
        self.assertEqual(Lifecycle.GONE, machine.states["proxy-erros"].lifecycle)

    def test_recurrence_reopens_when_supported(self):
        machine = StateMachine(reopen_supported=True)
        self.apply(machine, 1, active=("proxy-erros",), new=("proxy-erros",))
        self.apply(machine, 2, gone=("proxy-erros",))
        result = self.apply(machine, 3, active=("proxy-erros",), new=("proxy-erros",))
        self.assertEqual([TransitionKind.RECURRENCE_REOPEN], [item.kind for item in result])
        self.assertEqual("reopen", result[0].action)
        self.assertEqual(Lifecycle.OPEN, machine.states["proxy-erros"].lifecycle)
        self.assertEqual(1, machine.states["proxy-erros"].recurrence_version)

    def test_recurrence_creates_linked_card_when_reopen_disallowed(self):
        machine = StateMachine(reopen_supported=False)
        self.apply(machine, 1, active=("proxy-erros",), new=("proxy-erros",))
        original = machine.states["proxy-erros"].incident_id
        self.apply(machine, 2, gone=("proxy-erros",))
        result = self.apply(machine, 3, active=("proxy-erros",), new=("proxy-erros",))
        self.assertEqual([TransitionKind.RECURRENCE_LINKED], [item.kind for item in result])
        self.assertEqual("create_linked", result[0].action)
        self.assertEqual(original, result[0].linked_incident_id)
        self.assertNotEqual(original, machine.states["proxy-erros"].incident_id)

    def test_duplicate_stale_conflict_and_orphan_are_deterministic_noops(self):
        machine = StateMachine()
        self.apply(machine, 2, active=("proxy-erros",), new=("proxy-erros",))
        cases = [
            (3, ("proxy-erros",), ("proxy-erros",), (), "event-3", "digest-3", TransitionKind.DUPLICATE_NEW),
            (1, ("proxy-erros",), (), (), "event-1", "digest-1", TransitionKind.STALE),
            (2, ("proxy-erros",), (), (), "same-time", "other-digest", TransitionKind.CONFLICT),
            (4, (), (), ("unknown-key",), "event-4", "digest-4", TransitionKind.GONE_ORPHAN),
        ]
        for second, active, new, gone, event_id, digest, expected in cases:
            with self.subTest(expected=expected):
                result = self.apply(machine, second, active=active, new=new, gone=gone, event_id=event_id, digest=digest)
                self.assertEqual([expected], [item.kind for item in result])
                self.assertIsNone(result[0].action)

    def test_mixed_keys_are_sorted_and_isolated(self):
        machine = StateMachine()
        self.apply(machine, 1, active=("alpha", "bravo"), new=("alpha", "bravo"))
        result = self.apply(machine, 2, active=("bravo", "charlie"), new=("charlie",), gone=("alpha",))
        self.assertEqual(["alpha", "bravo", "charlie"], [item.stable_key for item in result])
        self.assertEqual([TransitionKind.GONE, TransitionKind.UNCHANGED_ACTIVE, TransitionKind.NEW], [item.kind for item in result])
        self.assertEqual(Lifecycle.GONE, machine.states["alpha"].lifecycle)
        self.assertEqual(Lifecycle.OPEN, machine.states["bravo"].lifecycle)
        self.assertEqual(Lifecycle.OPEN, machine.states["charlie"].lifecycle)

    def test_empty_input_is_heartbeat(self):
        result = self.apply(StateMachine(), 1)
        self.assertEqual([TransitionKind.HEARTBEAT], [item.kind for item in result])
        self.assertIsNone(result[0].action)


if __name__ == "__main__":
    unittest.main()
