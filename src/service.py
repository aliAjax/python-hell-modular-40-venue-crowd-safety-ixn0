import copy
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied
from .repository import utcnow
from .rules import RuleEngine, incident_priority, max_severity

TERMINAL_TASK_STATUSES = {"completed", "cancelled"}
MERGED_SOURCE_FIELDS = ("merge_record_id", "merged_into")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ---- 基础实体 ----------------------------------------------------------

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "incident":
            return self._create_incident(actor, payload, idempotency_key)
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        return self._persist_created(actor, kind, payload, idempotency_key)

    def _persist_created(self, actor, kind, payload, idempotency_key=None):
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_incident(self, actor, payload, idempotency_key):
        """事件入库即进入待归并台账：实体与来源登记在同一事务内写入。

        断网重试时，同一来源（venue_id + source_ref）命中登记表唯一约束，
        以冲突失败，不会再次入库或派生任务。
        """
        validated = self.rules.validate_create(actor, "incident", payload, self._lookup)
        payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status("incident")
        with self.repository.unit_of_work() as uow:
            if idempotency_key:
                existing = uow.get_idempotency(actor.user_id, idempotency_key)
                if existing:
                    entity = uow.get_entity(existing)
                    if entity:
                        return entity
            if uow.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            # 来源登记唯一约束是去重的最终防线（与 incident_key 双重保障）。
            uow.register_source(
                payload["venue_id"],
                payload["source_ref"],
                entity_id,
                payload.get("channel", "radio"),
            )
            entity = uow.create_entity(entity_id, "incident", status, payload, actor.user_id)
            uow.append_audit(entity_id, actor.user_id, actor.role, "create", None, status,
                             {"kind": "incident", "source_ref": payload["source_ref"]})
            if idempotency_key:
                uow.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        if action in ("merge", "unmerge"):
            raise InvalidTransition(
                "use the dedicated merge endpoint for action %s" % action
            )
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(entity["kind"]) == "incident":
            return self.incident_detail(entity_id, entity=entity)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ---- 待归并台账 --------------------------------------------------------

    def pending_sources(self, include_merged=False, venue_id=None):
        """待归并台账。默认只看尚未归并的来源。"""
        status = None if include_merged else "pending"
        return self.repository.list_sources(venue_id=venue_id, status=status)

    # ---- 场馆协调员权限 ----------------------------------------------------

    def _venue_of_incident(self, incident):
        venue = self.repository.get_entity(incident["data"].get("venue_id"))
        if not venue:
            raise NotFoundError("venue not found for incident")
        return venue

    def _require_venue_coordinator(self, actor, venue):
        if actor.role != "coordinator":
            raise PermissionDenied("only venue coordinators may perform this action")
        if actor.user_id not in (venue["data"].get("coordinator_ids") or []):
            raise PermissionDenied("actor is not a coordinator of this venue")

    # ---- 事件归并 ----------------------------------------------------------

    def merge_incidents(self, actor, primary_id, source_ids, expected_version=None,
                        idempotency_key=None):
        """选主事件并把若干待归并来源并入。

        - 主事件保留，来源事件置为 merged；
        - 主事件按全体来源的最高严重度重新定级；
        - 未执行（draft）任务转到主事件；已派出的任务留在来源事件，不重复生成；
        - 乐观锁：expected_version 基于主事件，并发归并后到的一方收到 409。
        """
        primary = self.repository.get_entity(primary_id)
        if not primary or primary["kind"] != "incident":
            raise NotFoundError("primary incident not found: " + str(primary_id))
        if primary["status"] == "merged":
            raise InvalidTransition("a merged incident cannot be the primary")
        venue = self._venue_of_incident(primary)
        self._require_venue_coordinator(actor, venue)

        ordered_ids = []
        for source_id in source_ids or []:
            if source_id not in ordered_ids:
                ordered_ids.append(source_id)
        if not ordered_ids:
            raise InvalidTransition("at least one source incident is required")
        if primary_id in ordered_ids:
            raise InvalidTransition("primary incident cannot also be a source")

        expected = int(expected_version) if expected_version is not None else primary["version"]

        with self.repository.unit_of_work() as uow:
            if idempotency_key:
                existing_id = uow.get_idempotency(actor.user_id, idempotency_key)
                if existing_id:
                    existing = uow.get_entity(existing_id)
                    if existing:
                        return existing

            locked_primary = uow.get_entity(primary_id)
            if not locked_primary or locked_primary["kind"] != "incident":
                raise NotFoundError("primary incident not found: " + str(primary_id))
            if locked_primary["version"] != expected:
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected, locked_primary["version"])
                )
            if locked_primary["status"] == "merged":
                raise InvalidTransition("a merged incident cannot be the primary")

            sources = []
            for source_id in ordered_ids:
                source = uow.get_entity(source_id)
                if not source or source["kind"] != "incident":
                    raise NotFoundError("source incident not found: " + str(source_id))
                if source["data"].get("venue_id") != venue["id"]:
                    raise PermissionDenied("all incidents must belong to the primary venue")
                if source["status"] != "reported" and source["status"] != "triaged" \
                        and source["status"] != "dispatched" and source["status"] != "reopened":
                    raise InvalidTransition(
                        "source incident %s is not pending merge (status %s)"
                        % (source_id, source["status"])
                    )
                registry = uow.get_source(venue["id"], source["data"]["source_ref"])
                if not registry or registry["status"] != "pending":
                    raise ConflictError(
                        "source already merged: %s" % source["data"]["source_ref"]
                    )
                sources.append((source, registry))

            # 重新定级：取主事件和全部来源的最高严重度，主事件类型不变。
            severities = [locked_primary["data"].get("severity")]
            severities.extend(source["data"].get("severity") for source, _ in sources)
            highest = max_severity(*severities)

            record_id = str(uuid4())
            primary_data = dict(locked_primary["data"])
            snapshot_primary = {
                "status": locked_primary["status"],
                "data": copy.deepcopy(primary_data),
                "version": locked_primary["version"],
            }
            primary_sources = list(primary_data.get("sources") or [])
            primary_source_refs = {item["source_ref"] for item in primary_sources}

            snapshot_sources = []
            moved_tasks = []

            def collect_primary_sources(incident, include_self):
                """汇总归并组来源清单（主事件自身 + 来源事件）。"""
                nonlocal primary_sources, primary_source_refs
                if include_self:
                    summary = self._source_summary(incident, uow)
                    if summary["source_ref"] not in primary_source_refs:
                        primary_sources.append(summary)
                        primary_source_refs.add(summary["source_ref"])
                # 主事件既有的来源清单也要带上来（重复归并防护下通常为空）。
                for item in incident["data"].get("sources") or []:
                    if item["source_ref"] not in primary_source_refs:
                        primary_sources.append(item)
                        primary_source_refs.add(item["source_ref"])

            def transfer_draft_tasks(incident):
                """未执行（draft）任务转到主事件；其余任务保留、不重复生成。"""
                for task in uow.list_by_field("task", "incident_id", incident["id"]):
                    if task["status"] == "draft":
                        task_data = dict(task["data"])
                        task_data["incident_id"] = primary_id
                        task_data["moved_from"] = incident["id"]
                        task_data["merge_record_id"] = record_id
                        moved = uow.update_entity(task["id"], task["version"], task["status"], task_data)
                        moved_tasks.append({"task_id": task["id"], "from": incident["id"],
                                            "to": primary_id, "action": "moved"})
                        uow.append_audit(task["id"], actor.user_id, actor.role,
                                         "merge_transfer", task["status"], moved["status"],
                                         {"merge_record_id": record_id,
                                          "from_incident": incident["id"],
                                          "to_incident": primary_id})
                    else:
                        # 已派出的任务保留在来源事件，不重复生成，仅建立关联供详情展示。
                        moved_tasks.append({"task_id": task["id"], "from": incident["id"],
                                            "to": incident["id"], "action": "retained",
                                            "status": task["status"]})

            for source, _registry in sources:
                snapshot_sources.append({
                    "incident_id": source["id"],
                    "status": source["status"],
                    "data": dict(source["data"]),
                    "version": source["version"],
                })
                collect_primary_sources(source, include_self=True)
                transfer_draft_tasks(source)
                source_data = dict(source["data"])
                source_data["merged_into"] = primary_id
                source_data["merge_record_id"] = record_id
                uow.update_entity(source["id"], source["version"], "merged", source_data)

            collect_primary_sources(locked_primary, include_self=True)

            primary_data["severity"] = highest
            primary_data["priority_score"] = incident_priority(
                highest, primary_data.get("incident_type")
            )
            primary_data["sources"] = primary_sources
            primary_data["merge_record_ids"] = list(
                primary_data.get("merge_record_ids") or []
            ) + [record_id]
            updated_primary = uow.update_entity(
                primary_id, expected, locked_primary["status"], primary_data
            )

            for source, registry in sources:
                uow.mark_source(venue["id"], registry["source_ref"], "merged", record_id)

            record = uow.create_entity(
                record_id,
                "merge_record",
                "active",
                {
                    "venue_id": venue["id"],
                    "primary_incident_id": primary_id,
                    "source_incident_ids": ordered_ids,
                    "moved_tasks": moved_tasks,
                    "snapshot": {
                        "primary": snapshot_primary,
                        "sources": snapshot_sources,
                    },
                    "created_by": actor.user_id,
                    "revoked_at": None,
                    "revoked_by": None,
                },
                actor.user_id,
            )
            uow.append_audit(record_id, actor.user_id, actor.role, "merge",
                             None, "active",
                             {"primary": primary_id, "sources": ordered_ids,
                              "moved_tasks": moved_tasks, "severity": highest})
            uow.append_audit(primary_id, actor.user_id, actor.role, "merged_primary",
                             locked_primary["status"], updated_primary["status"],
                             {"merge_record_id": record_id, "sources": ordered_ids,
                              "severity": highest})
            for source, _registry in sources:
                uow.append_audit(source["id"], actor.user_id, actor.role, "merged_into",
                                 source["status"], "merged",
                                 {"merge_record_id": record_id, "primary": primary_id})
            if idempotency_key:
                uow.save_idempotency(actor.user_id, idempotency_key, record_id)

        return self.repository.get_entity(record_id)

    # ---- 撤销归并 ----------------------------------------------------------

    def unmerge_incidents(self, actor, merge_record_id, expected_version=None):
        """撤销一次归并：原事件（状态与数据）和任务全部恢复。

        只有本场馆协调员可撤销；expected_version 基于归并记录，
        与并发的归并/撤销冲突时返回 409。
        """
        record = self.repository.get_entity(merge_record_id)
        if not record or record["kind"] != "merge_record":
            raise NotFoundError("merge record not found: " + str(merge_record_id))
        venue = self.repository.get_entity(record["data"].get("venue_id"))
        if not venue:
            raise NotFoundError("venue not found for merge record")
        # 越权（含别馆协调员、其他角色）直接拒绝。
        self._require_venue_coordinator(actor, venue)

        expected = int(expected_version) if expected_version is not None else record["version"]

        with self.repository.unit_of_work() as uow:
            locked_record = uow.get_entity(merge_record_id)
            if not locked_record or locked_record["kind"] != "merge_record":
                raise NotFoundError("merge record not found: " + str(merge_record_id))
            if locked_record["version"] != expected:
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected, locked_record["version"])
                )
            if locked_record["status"] != "active":
                raise InvalidTransition("merge record is already %s" % locked_record["status"])

            record_data = dict(locked_record["data"])
            primary_id = record_data["primary_incident_id"]
            source_ids = list(record_data["source_incident_ids"])

            primary = uow.get_entity(primary_id)
            if not primary:
                raise NotFoundError("primary incident missing: " + primary_id)

            # 恢复任务：当时转到主事件的 draft 任务退回原来源事件；
            # 已派出而保留的任务无需处理。
            for moved in record_data.get("moved_tasks") or []:
                if moved.get("action") != "moved":
                    continue
                task = uow.get_entity(moved["task_id"])
                if not task:
                    continue
                if task["data"].get("incident_id") != primary_id:
                    # 任务已被另行调度/改派，保留现状，不强行回滚。
                    continue
                task_data = dict(task["data"])
                task_data["incident_id"] = moved["from"]
                task_data.pop("moved_from", None)
                task_data.pop("merge_record_id", None)
                restored = uow.update_entity(task["id"], task["version"], task["status"], task_data)
                uow.append_audit(task["id"], actor.user_id, actor.role,
                                 "merge_transfer_restore", restored["status"], restored["status"],
                                 {"merge_record_id": merge_record_id,
                                  "incident_id": moved["from"]})

            # 恢复来源事件到归并前的状态和数据。
            snapshot_sources = {
                item["incident_id"]: item
                for item in record_data.get("snapshot", {}).get("sources", [])
            }
            restored_refs = set()
            for source_id in source_ids:
                snapshot = snapshot_sources.get(source_id)
                source = uow.get_entity(source_id)
                if not snapshot or not source:
                    continue
                source_data = dict(source["data"])
                source_data.pop("merged_into", None)
                source_data.pop("merge_record_id", None)
                # 以快照为准恢复归并前字段，保留归并后的其他字段不动。
                for key, value in snapshot["data"].items():
                    if key in MERGED_SOURCE_FIELDS:
                        continue
                    source_data[key] = value
                uow.update_entity(source_id, source["version"], snapshot["status"], source_data)
                restored_refs.add(snapshot["data"].get("source_ref"))
                uow.append_audit(source_id, actor.user_id, actor.role, "merge_restored",
                                 source["status"], snapshot["status"],
                                 {"merge_record_id": merge_record_id})

            # 主事件恢复归并前数据（含严重度、来源清单），当前状态保留归并后的流转。
            primary_snapshot = record_data.get("snapshot", {}).get("primary") or {}
            primary_data = dict(primary["data"])
            for key, value in (primary_snapshot.get("data") or {}).items():
                primary_data[key] = value
            primary_data["merge_record_ids"] = [
                rid for rid in (primary_data.get("merge_record_ids") or [])
                if rid != merge_record_id
            ]
            updated_primary = uow.update_entity(
                primary_id, primary["version"], primary["status"], primary_data
            )

            for source_ref in restored_refs:
                uow.mark_source(venue["id"], source_ref, "pending", None)

            record_data["revoked_at"] = utcnow()
            record_data["revoked_by"] = actor.user_id
            uow.update_entity(merge_record_id, expected, "revoked", record_data)

            uow.append_audit(merge_record_id, actor.user_id, actor.role, "unmerge",
                             "active", "revoked",
                             {"primary": primary_id, "sources": source_ids})
            uow.append_audit(primary_id, actor.user_id, actor.role, "merge_revoked_primary",
                             primary["status"], updated_primary["status"],
                             {"merge_record_id": merge_record_id})

        return self.repository.get_entity(merge_record_id)

    # ---- 事件详情 ----------------------------------------------------------

    @staticmethod
    def _source_summary(incident, uow):
        return {
            "source_ref": incident["data"].get("source_ref"),
            "channel": incident["data"].get("channel"),
            "incident_id": incident["id"],
            "incident_type": incident["data"].get("incident_type"),
            "severity": incident["data"].get("severity"),
            "reported_at": incident["data"].get("reported_at"),
            "status": incident["status"],
        }

    def _merge_group(self, incident, repository):
        """返回 (主事件, 归并记录id或None, 来源事件列表)。"""
        if incident["status"] == "merged":
            primary_id = incident["data"].get("merged_into")
            primary = repository.get_entity(primary_id) if primary_id else None
            if not primary:
                return incident, None, [incident]
            record_id = incident["data"].get("merge_record_id")
            return primary, record_id, self._group_source_incidents(primary, repository)
        return incident, None, self._group_source_incidents(incident, repository)

    def _group_source_incidents(self, primary, repository):
        ids = []
        for record_id in primary["data"].get("merge_record_ids") or []:
            record = repository.get_entity(record_id)
            if record and record["status"] == "active":
                for source_id in record["data"].get("source_incident_ids") or []:
                    if source_id not in ids:
                        ids.append(source_id)
        incidents = [primary]
        for source_id in ids:
            source = repository.get_entity(source_id)
            if source:
                incidents.append(source)
        return incidents

    def incident_detail(self, incident_id, entity=None):
        """事件详情：主事件、来源清单、未决任务数（覆盖整个归并组）。"""
        incident = entity or self.repository.get_entity(incident_id)
        if not incident or incident["kind"] != "incident":
            raise NotFoundError("incident not found: " + str(incident_id))
        primary, via_record, group = self._merge_group(incident, self.repository)

        source_list = []
        open_task_count = 0
        retained_tasks = []
        for member in group:
            source_list.append({
                "source_ref": member["data"].get("source_ref"),
                "channel": member["data"].get("channel"),
                "incident_id": member["id"],
                "incident_type": member["data"].get("incident_type"),
                "severity": member["data"].get("severity"),
                "reported_at": member["data"].get("reported_at"),
                "status": member["status"],
                "is_primary": member["id"] == primary["id"],
            })
            for task in self.repository.find_entities("task", "incident_id", member["id"]):
                if task["status"] not in TERMINAL_TASK_STATUSES:
                    open_task_count += 1
                    if member["id"] != primary["id"]:
                        retained_tasks.append({
                            "task_id": task["id"],
                            "incident_id": member["id"],
                            "status": task["status"],
                        })

        active_records = [
            rid for rid in (primary["data"].get("merge_record_ids") or [])
            if (record := self.repository.get_entity(rid)) and record["status"] == "active"
        ]
        if incident["id"] == primary["id"]:
            role = "primary" if active_records else "standalone"
        else:
            role = "merged_source"
        detail = dict(primary)
        detail["merge"] = {
            "role": role,
            "merge_record_id": via_record or (active_records[0] if active_records else None),
            "active_merge_record_ids": active_records,
            "primary_incident_id": primary["id"],
            "sources": sorted(source_list, key=lambda item: item["reported_at"] or ""),
            "open_task_count": open_task_count,
            "retained_tasks": retained_tasks,
        }
        return detail
