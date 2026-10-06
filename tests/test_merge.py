import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class MergeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "merge.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("coord-1", "coordinator")
        self.coordinator_b = Actor("coord-2", "coordinator")
        self.supervisor = Actor("supervisor-1", "supervisor")
        self.operator = Actor("operator-1", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_zone_gate(self, capacity=1000):
        venue = self.service.create(
            self.coordinator, "venue", {"name": "V", "address": "A"}
        )
        zone = self.service.create(
            self.coordinator, "zone",
            {"venue_id": venue["id"], "name": "Z", "capacity": capacity},
        )
        gate = self.service.create(
            self.coordinator, "gate",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [zone["id"]]},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "op"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
        return venue, zone, gate

    def _incident(self, venue, zone, ref, severity, itype="medical"):
        return self.service.create(
            self.operator, "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": ref,
                "incident_type": itype,
                "severity": severity,
                "reported_at": "2026-10-06T18:00:00Z",
            },
        )

    def _task(self, venue, zone, incident, team, status=None):
        task = self.service.create(
            self.supervisor, "task",
            {
                "incident_id": incident["id"],
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "team_id": team,
                "task_type": "medical",
            },
        )
        if status == "assigned":
            self.service.transition(
                self.coordinator, task["id"], "assign", {"assigned_at": "t"}
            )
        return task


class MergeWorkflowTest(MergeTestBase):
    def test_merge_rerates_by_highest_severity_and_moves_draft_tasks(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-1", "high", "fire")
        video = self._incident(venue, zone, "video-1", "critical", "stampede")
        phone = self._incident(venue, zone, "phone-1", "medium", "medical")

        # Unexecuted (draft) task on the radio source -> must move to the main.
        draft_task = self._task(venue, zone, radio, "team-draft")
        # Dispatched task on the video source -> must stay, not be re-created.
        dispatched_task = self._task(venue, zone, video, "team-disp", status="assigned")

        merge = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"], phone["id"]]
        )

        self.assertEqual(merge["status"], "active")
        # Re-rated to the highest severity across all sources.
        self.assertEqual(merge["data"]["severity"], "critical")
        self.assertEqual(merge["data"]["severity_rank"], 4)
        self.assertEqual(merge["data"]["main_incident_id"], radio["id"])
        self.assertCountEqual(
            merge["data"]["source_incident_ids"],
            [radio["id"], video["id"], phone["id"]],
        )

        # Main keeps its id but is re-rated and linked to the merge.
        main = self.service.get(radio["id"])
        self.assertEqual(main["data"]["severity"], "critical")
        self.assertEqual(main["data"]["merge_id"], merge["id"])
        # Sources are marked merged and point at the main.
        for source in (video, phone):
            entity = self.service.get(source["id"])
            self.assertEqual(entity["status"], "merged")
            self.assertEqual(entity["data"]["merged_into"], radio["id"])
            self.assertEqual(entity["data"]["merge_id"], merge["id"])

        # Draft task moved onto the main; dispatched task stays on its source.
        self.assertEqual(
            self.service.get(draft_task["id"])["data"]["incident_id"], radio["id"]
        )
        self.assertEqual(
            self.service.get(dispatched_task["id"])["data"]["incident_id"], video["id"]
        )
        # No duplicate tasks were generated.
        tasks = self.service.list("task")
        self.assertEqual(len(tasks), 2)

    def test_pending_ledger_excludes_merged_sources(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-2", "high")
        video = self._incident(venue, zone, "video-2", "low")
        self.assertEqual(len(self.service.pending_sources(self.coordinator)), 2)

        self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]]
        )
        pending = self.service.pending_sources(self.coordinator)
        self.assertEqual(pending, [])

    def test_incident_detail_shows_main_sources_and_pending_count(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-3", "high")
        video = self._incident(venue, zone, "video-3", "critical")
        phone = self._incident(venue, zone, "phone-3", "medium")
        # One draft task on the main counts as pending after the move.
        self._task(venue, zone, radio, "team-a")
        # One dispatched task on a source also counts as pending.
        self._task(venue, zone, video, "team-b", status="assigned")

        merge = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"], phone["id"]]
        )
        detail = self.service.incident_detail(self.coordinator, radio["id"])

        self.assertEqual(detail["main_incident"]["id"], radio["id"])
        self.assertCountEqual(
            [s["id"] for s in detail["sources"]],
            [radio["id"], video["id"], phone["id"]],
        )
        # draft task moved to main (1) + dispatched task on video (1) = 2 pending.
        self.assertEqual(detail["pending_task_count"], 2)
        self.assertEqual(detail["merge"]["id"], merge["id"])


class MergeConcurrencyTest(MergeTestBase):
    def test_late_merge_sees_conflict_and_keeps_own_sources(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-4", "high")
        video = self._incident(venue, zone, "video-4", "critical")
        phone = self._incident(venue, zone, "phone-4", "medium")
        other = self._incident(venue, zone, "phone-4b", "low")

        # Coordinator A merges radio+video+phone into radio.
        self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"], phone["id"]]
        )

        # Coordinator B concurrently tries to merge video (already merged) with
        # their own source other -> must conflict, not lose other.
        with self.assertRaises(ConflictError):
            self.service.merge_incidents(
                self.coordinator_b, video["id"], [video["id"], other["id"]]
            )
        # B's source was not swallowed: it is still pending and unmerged.
        still_pending = self.service.get(other["id"])
        self.assertEqual(still_pending["status"], "reported")
        self.assertNotIn("merge_id", still_pending["data"])
        # B can retry with just their own source.
        retry = self.service.merge_incidents(
            self.coordinator_b, other["id"], [other["id"]]
        )
        self.assertEqual(retry["status"], "active")

    def test_retry_after_network_drop_does_not_remerge(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-5", "high")
        video = self._incident(venue, zone, "video-5", "critical")
        self._task(venue, zone, radio, "team-c")

        first = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]],
            idempotency_key="drop-key-1",
        )
        # Retry with the same key after reconnect: same ledger row, no new tasks.
        second = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]],
            idempotency_key="drop-key-1",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.list("incident_merge")), 1)
        self.assertEqual(len(self.service.list("task")), 1)

    def test_retry_without_key_returns_existing_merge(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-6", "high")
        video = self._incident(venue, zone, "video-6", "critical")

        first = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]]
        )
        # Same merge submitted again without a key: idempotent, not a conflict.
        second = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]]
        )
        self.assertEqual(first["id"], second["id"])


class MergeCancelTest(MergeTestBase):
    def test_cancel_restores_incidents_and_tasks(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-7", "high", "fire")
        video = self._incident(venue, zone, "video-7", "critical")
        phone = self._incident(venue, zone, "phone-7", "medium")
        draft_task = self._task(venue, zone, radio, "team-d")

        merge = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"], phone["id"]]
        )
        cancelled = self.service.cancel_merge(
            self.coordinator, merge["id"], "false alarm"
        )

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["data"]["cancelled_by"], self.coordinator.user_id)
        # Sources return to their pre-merge status and lose the merge link.
        for source in (radio, video, phone):
            entity = self.service.get(source["id"])
            self.assertEqual(entity["status"], "reported")
            self.assertNotIn("merge_id", entity["data"])
            self.assertNotIn("merged_into", entity["data"])
        # Main is restored to its original severity/priority.
        main = self.service.get(radio["id"])
        self.assertEqual(main["data"]["severity"], "high")
        # The moved task returns to its original incident.
        self.assertEqual(
            self.service.get(draft_task["id"])["data"]["incident_id"], radio["id"]
        )

    def test_cancel_with_stale_version_conflicts(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-8", "high")
        video = self._incident(venue, zone, "video-8", "critical")
        merge = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]]
        )
        with self.assertRaises(ConflictError):
            self.service.cancel_merge(
                self.coordinator, merge["id"], "stale", expected_version=999
            )
        # The merge is still active after the failed cancel.
        self.assertEqual(self.service.get(merge["id"])["status"], "active")

    def test_only_coordinator_can_cancel(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-9", "high")
        video = self._incident(venue, zone, "video-9", "critical")
        merge = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]]
        )
        for actor in (self.supervisor, self.operator):
            with self.assertRaises(PermissionDenied):
                self.service.cancel_merge(actor, merge["id"], "not allowed")
        # Coordinator can cancel.
        self.assertEqual(
            self.service.cancel_merge(self.coordinator, merge["id"], "ok")["status"],
            "cancelled",
        )

    def test_cancel_requires_reason(self):
        venue, zone, _ = self._venue_zone_gate()
        radio = self._incident(venue, zone, "radio-10", "high")
        video = self._incident(venue, zone, "video-10", "critical")
        merge = self.service.merge_incidents(
            self.coordinator, radio["id"], [radio["id"], video["id"]]
        )
        with self.assertRaises(ValidationError):
            self.service.cancel_merge(self.coordinator, merge["id"], "")


if __name__ == "__main__":
    unittest.main()
