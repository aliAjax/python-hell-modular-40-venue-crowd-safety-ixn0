import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine, max_severity
from src.service import DomainService


class IncidentMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "merging.db"),
            RuleEngine(),
        )
        self.admin = Actor("admin-1", "admin")
        self.coordinator = Actor("venue-commander", "coordinator")
        self.other_coordinator = Actor("other-venue-commander", "coordinator")
        self.supervisor = Actor("safety-supervisor", "supervisor")
        self.operator = Actor("gate-operator", "operator")
        self.venue, self.zone = self._venue_zone()

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_zone(self, coordinator_ids=None):
        venue = self.service.create(
            self.admin,
            "venue",
            {
                "name": "Grand Hall",
                "address": "1 Stadium Road",
                "coordinator_ids": coordinator_ids or ["venue-commander"],
            },
        )
        zone = self.service.create(
            self.admin,
            "zone",
            {"venue_id": venue["id"], "name": "North Stand", "capacity": 1000},
        )
        return venue, zone

    def _report(self, source_ref, severity="medium", channel="radio", incident_type="crowd"):
        return self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "source_ref": source_ref,
                "incident_type": incident_type,
                "severity": severity,
                "channel": channel,
                "reported_at": "2026-10-06T02:00:00Z",
            },
        )

    def _draft_task(self, incident_id, team_id):
        return self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": incident_id,
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "team_id": team_id,
                "task_type": "medical",
            },
        )

    # ---- 台账与最高严重度 --------------------------------------------------

    def test_sources_enter_pending_ledger_with_distinct_channels(self):
        self._report("radio-17", channel="radio")
        self._report("video-09", channel="video")
        self._report("phone-03", channel="phone")
        pending = self.service.pending_sources()
        self.assertEqual({item["source_ref"] for item in pending},
                         {"radio-17", "video-09", "phone-03"})
        self.assertTrue(all(item["status"] == "pending" for item in pending))
        channels = {item["channel"] for item in pending}
        self.assertEqual(channels, {"radio", "video", "phone"})

    def test_max_severity_helper(self):
        self.assertEqual(max_severity("low", "medium", "critical", "high"), "critical")
        self.assertEqual(max_severity("medium", "low"), "medium")

    # ---- 归并主流程 --------------------------------------------------------

    def test_merge_regrades_by_highest_severity_and_moves_draft_tasks(self):
        primary = self._report("radio-17", severity="medium")
        video = self._report("video-09", severity="high")
        phone = self._report("phone-03", severity="critical")

        draft_on_video = self._draft_task(video["id"], "team-pending")
        dispatched = self._draft_task(phone["id"], "team-alpha")
        self.service.transition(
            self.coordinator, dispatched["id"], "assign", {"assigned_at": "t1"}
        )

        record = self.service.merge_incidents(
            self.coordinator,
            primary["id"],
            [video["id"], phone["id"]],
            expected_version=primary["version"],
        )
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["data"]["source_incident_ids"], [video["id"], phone["id"]])

        merged_primary = self.service.get(primary["id"])
        self.assertEqual(merged_primary["data"]["severity"], "critical")
        self.assertEqual(len(merged_primary["merge"]["sources"]), 3)
        self.assertTrue(all(item["source_ref"] for item in merged_primary["merge"]["sources"]))

        video_after = self.service.repository.get_entity(video["id"])
        phone_after = self.service.repository.get_entity(phone["id"])
        self.assertEqual(video_after["status"], "merged")
        self.assertEqual(phone_after["status"], "merged")
        self.assertEqual(video_after["data"]["merged_into"], primary["id"])

        # 未执行任务转到主事件，已派出任务留在来源事件。
        moved_task = self.service.repository.get_entity(draft_on_video["id"])
        self.assertEqual(moved_task["data"]["incident_id"], primary["id"])
        self.assertEqual(moved_task["data"]["moved_from"], video["id"])
        kept_task = self.service.repository.get_entity(dispatched["id"])
        self.assertEqual(kept_task["data"]["incident_id"], phone["id"])

        # 未决任务数覆盖整个归并组（draft 1 + assigned 1）。
        detail = self.service.incident_detail(primary["id"])
        self.assertEqual(detail["merge"]["open_task_count"], 2)
        self.assertEqual(len(detail["merge"]["retained_tasks"]), 1)
        self.assertEqual(detail["merge"]["retained_tasks"][0]["incident_id"], phone["id"])

        # 从来源事件查详情，看到的是同一个主事件。
        via_video = self.service.get(video["id"])
        self.assertEqual(via_video["id"], primary["id"])
        self.assertEqual(via_video["merge"]["role"], "merged_source")

        # 台账中的来源已标记为已归并。
        pending = self.service.pending_sources()
        self.assertNotIn("video-09", {item["source_ref"] for item in pending})
        ledger = self.service.pending_sources(include_merged=True)
        video_entry = next(item for item in ledger if item["source_ref"] == "video-09")
        self.assertEqual(video_entry["status"], "merged")
        self.assertEqual(video_entry["merge_record_id"], record["id"])

    def test_dispatched_task_is_not_duplicated_on_primary(self):
        primary = self._report("radio-1", severity="medium")
        source = self._report("video-1", severity="high")
        task = self._draft_task(source["id"], "team-bravo")
        self.service.transition(self.coordinator, task["id"], "assign", {"assigned_at": "t1"})
        self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])
        primary_tasks = self.service.repository.find_entities("task", "incident_id", primary["id"])
        source_tasks = self.service.repository.find_entities("task", "incident_id", source["id"])
        self.assertEqual(primary_tasks, [])
        self.assertEqual(len(source_tasks), 1)

    # ---- 并发版本冲突 ------------------------------------------------------

    def test_concurrent_merges_second_committer_sees_conflict_without_losing_sources(self):
        a = self._report("radio-1", severity="low")
        b = self._report("video-1", severity="medium")
        c = self._report("phone-1", severity="high")

        record = self.service.merge_incidents(
            self.coordinator, a["id"], [b["id"]], expected_version=a["version"]
        )
        self.assertEqual(record["status"], "active")

        # 第二名协调员拿着旧版本号提交另一组归并：版本冲突。
        with self.assertRaises(ConflictError):
            self.service.merge_incidents(
                self.coordinator, a["id"], [c["id"]], expected_version=a["version"]
            )

        # 冲突没有副作用：c 仍是待归并来源，未被吞并。
        c_after = self.service.repository.get_entity(c["id"])
        self.assertNotEqual(c_after["status"], "merged")
        pending = {item["source_ref"] for item in self.service.pending_sources()}
        self.assertIn("phone-1", pending)

        # 刷新版本号后即可提交。
        a_after = self.service.repository.get_entity(a["id"])
        self.service.merge_incidents(
            self.coordinator, a_after["id"], [c["id"]], expected_version=a_after["version"]
        )
        self.assertEqual(self.service.repository.get_entity(c["id"])["status"], "merged")
        detail = self.service.incident_detail(a["id"])
        self.assertEqual(len(detail["merge"]["sources"]), 3)

    def test_concurrent_unmerge_conflicts_on_stale_record_version(self):
        primary = self._report("radio-1", severity="low")
        source = self._report("video-1", severity="medium")
        record = self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])

        # 并发撤销：第一条成功后，第二条拿旧版本号失败。
        restored = self.service.unmerge_incidents(
            self.coordinator, record["id"], expected_version=record["version"]
        )
        self.assertEqual(restored["status"], "revoked")
        with self.assertRaises(ConflictError):
            self.service.unmerge_incidents(
                self.coordinator, record["id"], expected_version=record["version"]
            )

    # ---- 撤销恢复 ----------------------------------------------------------

    def test_unmerge_restores_incidents_status_data_and_tasks(self):
        primary = self._report("radio-1", severity="medium")
        source = self._report("video-1", severity="critical")
        draft = self._draft_task(source["id"], "team-pending")
        dispatched = self._draft_task(source["id"], "team-out")
        self.service.transition(self.coordinator, dispatched["id"], "assign", {"assigned_at": "t1"})

        record = self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])
        self.assertEqual(self.service.get(primary["id"])["data"]["severity"], "critical")
        self.assertEqual(
            self.service.repository.get_entity(draft["id"])["data"]["incident_id"],
            primary["id"],
        )

        restored_record = self.service.unmerge_incidents(
            self.coordinator, record["id"], expected_version=record["version"]
        )
        self.assertEqual(restored_record["status"], "revoked")

        primary_after = self.service.get(primary["id"])
        source_after = self.service.repository.get_entity(source["id"])
        self.assertEqual(primary_after["data"]["severity"], "medium")
        self.assertEqual(primary_after["merge"]["role"], "standalone")
        self.assertEqual(primary_after["merge"]["active_merge_record_ids"], [])
        self.assertEqual(source_after["status"], "reported")
        self.assertNotIn("merged_into", source_after["data"])
        self.assertNotIn("merge_record_id", source_after["data"])

        draft_after = self.service.repository.get_entity(draft["id"])
        self.assertEqual(draft_after["data"]["incident_id"], source["id"])
        self.assertNotIn("moved_from", draft_after["data"])
        dispatched_after = self.service.repository.get_entity(dispatched["id"])
        self.assertEqual(dispatched_after["data"]["incident_id"], source["id"])

        pending = {item["source_ref"] for item in self.service.pending_sources()}
        self.assertIn("video-1", pending)

        # 撤销后可重新归并。
        new_record = self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])
        self.assertEqual(new_record["status"], "active")
        self.assertEqual(self.service.repository.get_entity(source["id"])["status"], "merged")

    def test_revoked_merge_cannot_be_unmerged_again(self):
        primary = self._report("radio-1", severity="low")
        source = self._report("video-1", severity="low")
        record = self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])
        self.service.unmerge_incidents(self.coordinator, record["id"])
        with self.assertRaises(InvalidTransition):
            self.service.unmerge_incidents(self.coordinator, record["id"])

    # ---- 越权拒绝 ----------------------------------------------------------

    def test_only_venue_coordinator_can_unmerge(self):
        primary = self._report("radio-1", severity="low")
        source = self._report("video-1", severity="medium")
        record = self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])

        # 主管/操作员无权撤销。
        for actor in (self.supervisor, self.operator, self.admin):
            with self.assertRaises(PermissionDenied):
                self.service.unmerge_incidents(actor, record["id"])

        # 别馆协调员越权撤销：拒绝。
        with self.assertRaises(PermissionDenied):
            self.service.unmerge_incidents(self.other_coordinator, record["id"])

        # 本馆协调员成功。
        restored = self.service.unmerge_incidents(self.coordinator, record["id"])
        self.assertEqual(restored["status"], "revoked")

    def test_merge_requires_venue_coordinator_and_same_venue(self):
        primary = self._report("radio-1", severity="low")
        source = self._report("video-1", severity="medium")
        with self.assertRaises(PermissionDenied):
            self.service.merge_incidents(self.supervisor, primary["id"], [source["id"]])
        with self.assertRaises(PermissionDenied):
            self.service.merge_incidents(self.other_coordinator, primary["id"], [source["id"]])

        other_venue = self.service.create(
            self.admin,
            "venue",
            {"name": "Other Hall", "address": "2 Arena Road",
             "coordinator_ids": ["other-venue-commander"]},
        )
        other_zone = self.service.create(
            self.admin,
            "zone",
            {"venue_id": other_venue["id"], "name": "East Stand", "capacity": 100},
        )
        foreign = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": other_venue["id"],
                "zone_id": other_zone["id"],
                "source_ref": "radio-99",
                "incident_type": "crowd",
                "severity": "low",
                "reported_at": "2026-10-06T02:00:00Z",
            },
        )
        with self.assertRaises(PermissionDenied):
            self.service.merge_incidents(self.coordinator, primary["id"], [foreign["id"]])

    # ---- 断网重试幂等 ------------------------------------------------------

    def test_retry_after_reconnect_does_not_readmit_or_regenerate(self):
        payload = {
            "venue_id": self.venue["id"],
            "zone_id": self.zone["id"],
            "source_ref": "radio-42",
            "incident_type": "medical",
            "severity": "high",
            "reported_at": "2026-10-06T02:00:00Z",
        }
        first = self.service.create(self.operator, "incident", payload, idempotency_key="report-42")
        # 断网恢复后原请求重试：幂等键直接返回同一事件。
        retry = self.service.create(self.operator, "incident", payload, idempotency_key="report-42")
        self.assertEqual(first["id"], retry["id"])

        # 即使换了幂等键，同来源再次入库也被登记表唯一约束拒绝。
        with self.assertRaises(ConflictError):
            self.service.create(self.operator, "incident", dict(payload), idempotency_key="report-42b")

        # 归并后同来源重试，仍然拒绝，不会产生第二条任务线索。
        dup = self._report("video-42", severity="low")
        record = self.service.merge_incidents(self.coordinator, first["id"], [dup["id"]])
        with self.assertRaises(ConflictError):
            self.service.create(self.operator, "incident", payload, idempotency_key="report-42c")

        # 归并请求带幂等键重试，返回同一条归并记录，不重复归并。
        primary2 = self._report("radio-50", severity="low")
        source2 = self._report("video-50", severity="low")
        merged = self.service.merge_incidents(
            self.coordinator, primary2["id"], [source2["id"]], idempotency_key="merge-50"
        )
        retried = self.service.merge_incidents(
            self.coordinator, primary2["id"], [source2["id"]], idempotency_key="merge-50"
        )
        self.assertEqual(merged["id"], retried["id"])

        # 不能对已归并来源建任务。
        with self.assertRaises(ConflictError):
            self._draft_task(dup["id"], "team-ghost")

    # ---- 非法归并 ----------------------------------------------------------



    def test_cannot_merge_merged_or_missing_sources(self):
        primary = self._report("radio-1", severity="low")
        source = self._report("video-1", severity="low")
        self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])

        with self.assertRaises(InvalidTransition):
            self.service.merge_incidents(self.coordinator, primary["id"], [source["id"]])
        with self.assertRaises(InvalidTransition):
            self.service.merge_incidents(self.coordinator, source["id"], [primary["id"]])
        with self.assertRaises(InvalidTransition):
            self.service.merge_incidents(self.coordinator, primary["id"], [])
        with self.assertRaises(InvalidTransition):
            self.service.merge_incidents(self.coordinator, primary["id"], [primary["id"]])

    def test_generic_transition_rejects_merge_action(self):
        primary = self._report("radio-1", severity="low")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.coordinator, primary["id"], "merge", {})

    # ---- 独立事件详情 ------------------------------------------------------

    def test_standalone_incident_detail_shape(self):
        incident = self._report("radio-7", severity="low")
        detail = self.service.incident_detail(incident["id"])
        self.assertEqual(detail["merge"]["role"], "standalone")
        self.assertEqual(len(detail["merge"]["sources"]), 1)
        self.assertEqual(detail["merge"]["sources"][0]["is_primary"], True)
        self.assertEqual(detail["merge"]["open_task_count"], 0)
        self.assertIsNone(detail["merge"]["merge_record_id"])


if __name__ == "__main__":
    unittest.main()
