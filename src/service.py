from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import RuleEngine, SEVERITY_RANK, incident_priority, max_severity


# Incidents that may still be folded into a merge. Resolved and already-merged
# incidents are closed and must not be re-ledgered.
MERGEABLE_INCIDENT_STATUSES = {"reported", "triaged", "dispatched", "reopened"}
# Tasks still waiting on an outcome count as unresolved for the detail view.
PENDING_TASK_STATUSES = {"draft", "assigned", "enroute", "on_scene"}
# Only tasks that have not been dispatched to a team can be moved onto the
# main incident; dispatched tasks stay put so no team is told twice.
TRANSFERABLE_TASK_STATUSES = {"draft"}


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "incident_merge":
            raise ValidationError(
                "incident merges cannot be created directly; use the merge action"
            )
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
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
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # Incident merge (事件归并)
    # ------------------------------------------------------------------
    def _load_incident(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("incident not found: " + entity_id)
        if entity["kind"] != "incident":
            raise ValidationError("not an incident: " + entity_id)
        return entity

    def _load_merge(self, merge_id):
        merge = self.repository.get_entity(merge_id)
        if not merge:
            raise NotFoundError("merge not found: " + merge_id)
        if merge["kind"] != "incident_merge":
            raise ValidationError("not an incident merge: " + merge_id)
        return merge

    def _find_active_merge(self, merge_id):
        merge = self._load_merge(merge_id)
        if merge["status"] != "active":
            raise ConflictError("merge is not active: " + merge_id)
        return merge

    def pending_sources(self, actor, venue_id=None):
        """待归并台账: incidents still available for merging."""
        result = []
        for entity in self.repository.list_entities(kind="incident"):
            if entity["status"] not in MERGEABLE_INCIDENT_STATUSES:
                continue
            if entity["data"].get("merge_id"):
                continue
            if venue_id is not None and entity["data"].get("venue_id") != venue_id:
                continue
            result.append(entity)
        return result

    def merge_incidents(self, actor, main_incident_id, source_incident_ids, idempotency_key=None):
        self.rules._ensure_role(actor, ("coordinator", "admin"))
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                merge = self.repository.get_entity(existing)
                if merge and merge["kind"] == "incident_merge":
                    return merge

        if not source_incident_ids:
            raise ValidationError("source_incident_ids is required")
        source_ids = list(dict.fromkeys(source_incident_ids))
        if main_incident_id not in source_ids:
            source_ids.insert(0, main_incident_id)

        incidents = {}
        for source_id in source_ids:
            incidents[source_id] = self._load_incident(source_id)

        main = incidents[main_incident_id]
        venue_id = main["data"]["venue_id"]
        for source_id, entity in incidents.items():
            if entity["data"].get("venue_id") != venue_id:
                raise ValidationError("all incidents in a merge must belong to the same venue")
            if entity["status"] not in MERGEABLE_INCIDENT_STATUSES:
                raise ConflictError(
                    "incident cannot be merged from status %s: %s" % (entity["status"], source_id)
                )
            if entity["data"].get("merge_id"):
                existing_merge = self.repository.get_entity(entity["data"]["merge_id"])
                if existing_merge and existing_merge["status"] == "active":
                    same_merge = (
                        existing_merge["data"].get("main_incident_id") == main_incident_id
                        and set(existing_merge["data"].get("source_incident_ids") or []) == set(source_ids)
                    )
                    if same_merge:
                        # Retry of the same merge after a network drop: return
                        # the existing ledger row instead of re-merging.
                        if idempotency_key:
                            self.repository.save_idempotency(actor.user_id, idempotency_key, existing_merge["id"])
                        return existing_merge
                    raise ConflictError("incident already merged into another merge: " + source_id)
                raise ConflictError("incident already merged: " + source_id)

        severities = [incidents[source_id]["data"].get("severity") for source_id in source_ids]
        merged_severity = max_severity(severities)
        merged_priority = incident_priority(
            merged_severity, main["data"].get("incident_type")
        )

        # Unexecuted (draft) tasks move onto the main; dispatched tasks stay
        # on their source so no team is re-tasked.
        transferred_tasks = []
        task_updates = []
        for source_id in source_ids:
            for task in self.repository.list_entities(kind="task"):
                if task["data"].get("incident_id") != source_id:
                    continue
                if task["status"] not in TRANSFERABLE_TASK_STATUSES:
                    continue
                transferred_tasks.append({"task_id": task["id"], "from_incident_id": source_id})
                new_data = dict(task["data"])
                new_data["incident_id"] = main_incident_id
                task_updates.append((task["id"], task["version"], new_data))

        merge_id = str(uuid4())
        source_status_before = {source_id: incidents[source_id]["status"] for source_id in source_ids}
        merge_data = {
            "venue_id": venue_id,
            "main_incident_id": main_incident_id,
            "source_incident_ids": source_ids,
            "severity": merged_severity,
            "severity_rank": SEVERITY_RANK[merged_severity],
            "status": "active",
            "source_status_before": source_status_before,
            "main_before": {
                "severity": main["data"].get("severity"),
                "priority_score": main["data"].get("priority_score"),
            },
            "transferred_tasks": transferred_tasks,
            "created_by": actor.user_id,
        }

        main_new_data = dict(main["data"])
        main_new_data["severity"] = merged_severity
        main_new_data["priority_score"] = merged_priority
        main_new_data["merge_id"] = merge_id
        main_update = (main_incident_id, main["version"], main["status"], main_new_data)

        source_updates = []
        for source_id in source_ids:
            if source_id == main_incident_id:
                continue
            entity = incidents[source_id]
            new_data = dict(entity["data"])
            new_data["merge_id"] = merge_id
            new_data["merged_into"] = main_incident_id
            source_updates.append((source_id, entity["version"], "merged", new_data))

        merge_row = {
            "id": merge_id,
            "kind": "incident_merge",
            "status": "active",
            "data": merge_data,
            "created_by": actor.user_id,
        }
        self.repository.apply_merge(
            merge_row=merge_row,
            main_update=main_update,
            source_updates=source_updates,
            task_updates=task_updates,
        )

        self.audit.record(
            merge_id, actor, "merge", None, "active",
            {"main": main_incident_id, "sources": source_ids, "severity": merged_severity},
        )
        for source_id in source_ids:
            from_status = incidents[source_id]["status"]
            to_status = main["status"] if source_id == main_incident_id else "merged"
            self.audit.record(
                source_id, actor, "merge", from_status, to_status, {"merge_id": merge_id},
            )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, merge_id)
        return self.repository.get_entity(merge_id)

    def cancel_merge(self, actor, merge_id, reason=None, expected_version=None):
        merge = self._find_active_merge(merge_id)
        # Only a coordinator may undo a merge; supervisors/operators are rejected.
        self.rules._ensure_role(actor, ("coordinator", "admin"))
        if not reason:
            raise ValidationError("cancel reason is required")

        data = dict(merge["data"])
        main_id = data["main_incident_id"]
        source_ids = list(data.get("source_incident_ids") or [])

        main = self._load_incident(main_id)
        main_before = data.get("main_before") or {}
        main_new_data = dict(main["data"])
        if "severity" in main_before:
            main_new_data["severity"] = main_before["severity"]
        if "priority_score" in main_before:
            main_new_data["priority_score"] = main_before["priority_score"]
        main_new_data.pop("merge_id", None)
        main_restore = (main_id, main["version"], main["status"], main_new_data)

        source_restores = []
        for source_id in source_ids:
            if source_id == main_id:
                continue
            entity = self._load_incident(source_id)
            restore_status = (data.get("source_status_before") or {}).get(source_id, "reported")
            new_data = dict(entity["data"])
            new_data.pop("merge_id", None)
            new_data.pop("merged_into", None)
            source_restores.append((source_id, entity["version"], restore_status, new_data))

        task_restores = []
        for moved in data.get("transferred_tasks") or []:
            task = self.repository.get_entity(moved["task_id"])
            if not task or task["kind"] != "task":
                continue
            new_data = dict(task["data"])
            new_data["incident_id"] = moved["from_incident_id"]
            task_restores.append((moved["task_id"], task["version"], new_data))

        merge_new_data = dict(data)
        merge_new_data["status"] = "cancelled"
        merge_new_data["cancelled_by"] = actor.user_id
        merge_new_data["cancel_reason"] = reason
        merge_new_data["cancelled_at"] = utcnow()
        expected = expected_version if expected_version is not None else merge["version"]
        merge_update = (merge_id, expected, "cancelled", merge_new_data)

        self.repository.apply_cancel(
            merge_update=merge_update,
            main_restore=main_restore,
            source_restores=source_restores,
            task_restores=task_restores,
        )
        self.audit.record(merge_id, actor, "cancel", "active", "cancelled", {"reason": reason})
        return self.repository.get_entity(merge_id)

    def incident_detail(self, actor, entity_id):
        """事件详情: 主事件、来源清单和未决任务数。"""
        entity = self._load_incident(entity_id)
        data = dict(entity["data"])
        merge = None
        main_incident = None
        sources = []
        merge_id = data.get("merge_id")
        if merge_id:
            merge = self._load_merge(merge_id)
            if merge["status"] == "active":
                merge_data = merge["data"]
                main_incident = self._load_incident(merge_data["main_incident_id"])
                for source_id in merge_data.get("source_incident_ids") or []:
                    source = self.repository.get_entity(source_id)
                    if source and source["kind"] == "incident":
                        sources.append(source)

        # For a merged incident the pending count covers the whole merge:
        # draft tasks moved onto the main plus dispatched tasks still on the
        # sources. A standalone incident counts only its own tasks.
        if merge and merge["status"] == "active":
            incident_scope = {source["id"] for source in sources}
        else:
            incident_scope = {entity_id}
        pending_task_count = 0
        for task in self.repository.list_entities(kind="task"):
            if task["data"].get("incident_id") in incident_scope and task["status"] in PENDING_TASK_STATUSES:
                pending_task_count += 1

        return {
            "incident": entity,
            "merge": merge,
            "main_incident": main_incident,
            "sources": sources,
            "pending_task_count": pending_task_count,
        }
