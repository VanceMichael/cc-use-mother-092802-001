"""资源开放服务命令层。

把台账、投影、规则引擎与隐私视图串成连续业务流程：
申请（幂等/版本化）→ 评审（回避、最小知情、例外双签）→
额度（领取/释放/延期/核对，全程守恒）→ 停机接续。
所有写操作都落到只追加台账；读状态来自重放投影。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .ledger import Ledger, canonical_json
from .projections import DomainState, GrantState, PoolState, replay
from .rules import evaluate, publish_rules as _validate_rules
from . import privacy


class ServiceError(ValueError):
    """业务规则被违反。"""


class ResourceService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.state: DomainState = replay(ledger.events)

    # ---- 基础登记 -------------------------------------------------------

    def register_org(self, org_id: str, name: str, group: str, *, ts: str | None = None) -> None:
        if org_id in self.state.orgs:
            raise ServiceError("机构已登记")
        self.ledger.append("organization_registered", {"org_id": org_id, "name": name, "group": group}, ts)
        self.state = replay(self.ledger.events)

    def register_developer(self, dev_id: str, name: str, org_id: str, *, ts: str | None = None) -> None:
        if dev_id in self.state.developers:
            raise ServiceError("开发者已登记")
        if org_id not in self.state.orgs:
            raise ServiceError("所属机构不存在")
        self.ledger.append(
            "developer_registered", {"dev_id": dev_id, "name": name, "org_id": org_id}, ts
        )
        self.state = replay(self.ledger.events)

    def register_project(self, project_id: str, name: str, track: str, *, ts: str | None = None) -> None:
        if project_id in self.state.projects:
            raise ServiceError("项目已登记")
        self.ledger.append("project_registered", {"project_id": project_id, "name": name, "track": track}, ts)
        self.state = replay(self.ledger.events)

    def publish_resource(
        self,
        resource_id: str,
        version: str,
        kind: str,
        name: str,
        license_scope: str,
        sensitivity: str = "normal",
        zone: str | None = None,
        description: str = "",
        *,
        ts: str | None = None,
    ) -> None:
        existing = self.state.resources.get(resource_id)
        if existing and version in existing["revisions"]:
            raise ServiceError("资源版本已存在；内容变化必须发布新版本")
        self.ledger.append(
            "resource_published",
            {
                "resource_id": resource_id,
                "version": version,
                "kind": kind,
                "name": name,
                "description": description,
                "license": license_scope,
                "sensitivity": sensitivity,
                "zone": zone,
            },
            ts,
        )
        self.state = replay(self.ledger.events)

    def publish_rule_set(
        self, track: str, version: int, config: dict[str, Any], *, effective_ts: str, ts: str | None = None
    ) -> None:
        _validate_rules(self.state, track, version, effective_ts, config)
        self.ledger.append(
            "rule_set_published",
            {"track": track, "version": version, "effective_ts": effective_ts, "config": config},
            ts or effective_ts,
        )
        self.state = replay(self.ledger.events)

    def open_quota_pool(
        self, pool_id: str, capacity: int, period_start: str, period_end: str, *, ts: str | None = None
    ) -> None:
        if pool_id in self.state.pools:
            raise ServiceError("配额池已开立")
        if capacity < 0:
            raise ServiceError("池容量不能为负")
        self.ledger.append(
            "quota_pool_opened",
            {
                "pool_id": pool_id,
                "capacity": capacity,
                "period_start": period_start,
                "period_end": period_end,
            },
            ts,
        )
        self.state = replay(self.ledger.events)

    def grant_access(
        self, access_id: str, dev_id: str, resource_id: str, valid_until: str, *, ts: str | None = None
    ) -> None:
        if access_id in self.state.accesses:
            raise ServiceError("授权标识已存在")
        self.ledger.append(
            "access_granted",
            {
                "access_id": access_id,
                "dev_id": dev_id,
                "resource_id": resource_id,
                "valid_until": valid_until,
            },
            ts,
        )
        self.state = replay(self.ledger.events)

    def revoke_access(self, access_id: str, *, ts: str | None = None) -> None:
        access = self.state.accesses.get(access_id)
        if access is None or access.status != "active":
            raise ServiceError("授权不存在或已失效")
        self.ledger.append("access_revoked", {"access_id": access_id}, ts)
        self.state = replay(self.ledger.events)

    # ---- 申请：幂等与版本化（R-APP）------------------------------------

    @staticmethod
    def application_key(dev_id: str, project_id: str, resource_ids: list[str], period: str) -> str:
        basis = canonical_json([dev_id, project_id, sorted(resource_ids), period])
        return "app-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def content_fingerprint(content: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()

    def submit_application(
        self,
        dev_id: str,
        project_id: str,
        resources: list[dict[str, str]],
        compute_units: int,
        purpose: str,
        period: str,
        pool_id: str,
        *,
        ts: str | None = None,
    ) -> dict[str, Any]:
        """提交申请。

        同申请键 + 同内容指纹：返回原申请版本（R-APP-2，幂等，不产生事件）。
        同申请键 + 内容变化：产生新不可变版本（R-APP-3）。
        """
        if dev_id not in self.state.developers:
            raise ServiceError("开发者不存在")
        project = self.state.projects.get(project_id)
        if project is None:
            raise ServiceError("项目不存在")
        if pool_id not in self.state.pools:
            raise ServiceError("配额池不存在")
        if compute_units < 0:
            raise ServiceError("算力需求不能为负")

        resource_ids = [f"{item['resource_id']}@{item['version']}" for item in resources]
        app_key = self.application_key(dev_id, project_id, resource_ids, period)
        content = {
            "project_id": project_id,
            "dev_id": dev_id,
            "track": project["track"],
            "resources": [dict(item) for item in resources],
            "compute_units": compute_units,
            "purpose": purpose,
        }
        fingerprint = self.content_fingerprint(content)

        existing = self.state.applications.get(app_key)
        if existing is not None:
            same = existing.revision_with_fingerprint(fingerprint)
            if same is not None:
                # R-APP-2：重复申请返回原结果（若已评审，含原决定）。
                revision = existing.revisions[same]
                return {
                    "app_key": app_key,
                    "revision": same,
                    "replayed": True,
                    "status": revision.status,
                    "decision": revision.decision,
                }
            revision_no = existing.latest_revision + 1
        else:
            revision_no = 1

        self.ledger.append(
            "application_submitted",
            {
                "app_key": app_key,
                "revision": revision_no,
                "fingerprint": fingerprint,
                "pool_id": pool_id,
                **content,
            },
            ts,
        )
        self.state = replay(self.ledger.events)
        return {"app_key": app_key, "revision": revision_no, "replayed": False, "status": "submitted"}

    def withdraw(self, app_key: str, revision_no: int, *, ts: str | None = None) -> None:
        """撤回尚未决定的申请版本；撤回后同键再提交即为新版本（R-APP-4）。"""
        application = self.state.applications.get(app_key)
        if application is None or revision_no not in application.revisions:
            raise ServiceError("申请版本不存在")
        if application.revisions[revision_no].status != "submitted":
            raise ServiceError("只能撤回尚未决定的申请版本")
        self.ledger.append(
            "application_withdrawn", {"app_key": app_key, "revision": revision_no}, ts
        )
        self.state = replay(self.ledger.events)

    # ---- 评审：回避、规则版本、例外双签（R-REV / R-RULE-3）--------------

    def review_packet(self, app_key: str, revision_no: int, role: str) -> dict[str, Any]:
        return privacy.review_packet(self.state, app_key, revision_no, role)

    def decide(
        self,
        app_key: str,
        revision_no: int,
        reviewer_id: str,
        *,
        approve: bool,
        exception: bool = False,
        second_reviewer_id: str | None = None,
        grant_id: str | None = None,
        expires_at: str | None = None,
        ts: str,
    ) -> dict[str, Any]:
        application = self.state.applications.get(app_key)
        if application is None or revision_no not in application.revisions:
            raise ServiceError("申请版本不存在")
        revision = application.revisions[revision_no]
        if revision.status == "decided":
            # R-APP-4 / 幂等：已作决定的版本不重判，返回原决定。
            return {"replayed": True, "decision": revision.decision}
        if revision.status != "submitted":
            raise ServiceError(f"申请版本状态为{revision.status}，不能评审")

        content = revision.content
        if reviewer_id == content["dev_id"]:
            raise ServiceError("申请人不能审批本人申请（含例外）")  # R-REV-1

        ruleset = self.state.rules_effective(content["track"], ts)
        if ruleset is None:
            raise ServiceError("该时刻没有生效的评审规则，不能作出决定")  # R-RULE-4

        pool_id = self._pool_of(app_key, revision_no)
        pool = self.state.pools[pool_id]
        evaluation = evaluate(self.state, content, ruleset, pool.available)

        hard_failures = [r for r in evaluation.failed if not r.waivable]
        soft_failures = evaluation.waivable_failures
        granted = False
        exception_used = False

        if approve:
            if hard_failures:
                raise ServiceError(
                    "存在不可放宽的不合格条款，不能批准："
                    + "、".join(f"{r.clause}（{r.detail}）" for r in hard_failures)
                )
            if soft_failures:
                if not exception:
                    raise ServiceError(
                        "存在可例外条款但未走例外："
                        + "、".join(r.clause for r in soft_failures)
                    )
                if second_reviewer_id is None:
                    raise ServiceError("例外批准必须由第二名评审人复核")
                if second_reviewer_id in (reviewer_id, content["dev_id"]):
                    raise ServiceError("例外复核人必须独立于评审人与申请人")  # R-REV-4
                exception_used = True
            granted = True
        # 拒绝不附例外，也不占用任何配额（R-QUOTA-3）。

        decision_payload = {
            "app_key": app_key,
            "revision": revision_no,
            "approve": granted,
            "exception": exception_used,
            "reviewer_id": reviewer_id,
            "second_reviewer_id": second_reviewer_id if exception_used else None,
            "rules_track": content["track"],
            "rules_version": ruleset["version"],  # decided-under，永久保留（R-RULE-3）
            "decided_ts": ts,
            "pool_id": pool_id,
            "pool_available_at_decision": pool.available,
            "basis": evaluation.to_basis(),
        }
        self.ledger.append("review_decided", decision_payload, ts)

        if granted and content["compute_units"] > 0:
            gid = grant_id or f"grant-{app_key}-r{revision_no}"
            if gid in {g.grant_id for p in self.state.pools.values() for g in p.grants.values()}:
                raise ServiceError("额度标识已存在")
            self.ledger.append(
                "grant_reserved",
                {
                    "grant_id": gid,
                    "app_key": app_key,
                    "revision": revision_no,
                    "pool_id": pool_id,
                    "units": content["compute_units"],
                    "expires_at": expires_at,
                },
                ts,
            )
        self.state = replay(self.ledger.events)
        return {"replayed": False, "decision": decision_payload}

    def _pool_of(self, app_key: str, revision_no: int) -> str:
        """从提交事件载荷取回该申请版本指定的配额池。"""
        for event in self.ledger.events:
            if (
                event.type == "application_submitted"
                and event.payload["app_key"] == app_key
                and event.payload["revision"] == revision_no
            ):
                return event.payload["pool_id"]
        raise ServiceError("找不到申请对应的配额池")

    # ---- 额度：领取 / 核对 / 释放 / 延期，全程守恒（R-QUOTA-2）----------

    def _grant(self, grant_id: str) -> tuple[PoolState, GrantState]:
        for pool in self.state.pools.values():
            if grant_id in pool.grants:
                return pool, pool.grants[grant_id]
        raise ServiceError("额度不存在")

    def _dedupe(self, idem_key: str) -> dict[str, Any] | None:
        if idem_key in self.state.idem_index:
            event_type, seq = self.state.idem_index[idem_key]
            return {"replayed": True, "idem_key": idem_key, "event_type": event_type, "seq": seq}
        return None

    def claim(self, grant_id: str, units: int, idem_key: str, *, ts: str | None = None) -> dict[str, Any]:
        dup = self._dedupe(idem_key)
        if dup:
            return dup
        pool, grant = self._grant(grant_id)
        self._require_active(grant)
        if units <= 0 or units > grant.reserved:
            raise ServiceError(f"领取数量非法：可领取预留{grant.reserved}单位")
        self.ledger.append(
            "grant_claimed",
            {"grant_id": grant_id, "pool_id": pool.pool_id, "units": units, "idem_key": idem_key},
            ts,
        )
        self.state = replay(self.ledger.events)
        return self._result(idem_key)

    def report_usage(self, grant_id: str, units: int, idem_key: str, *, ts: str | None = None) -> dict[str, Any]:
        dup = self._dedupe(idem_key)
        if dup:
            return dup
        pool, grant = self._grant(grant_id)
        self._require_active(grant)
        if units <= 0 or units > grant.claimed:
            raise ServiceError(f"核对用量非法：仅{grant.claimed}已领取单位可供核对")
        self.ledger.append(
            "usage_reported",
            {"grant_id": grant_id, "pool_id": pool.pool_id, "units": units, "idem_key": idem_key},
            ts,
        )
        self.state = replay(self.ledger.events)
        return self._result(idem_key)

    def release(
        self,
        grant_id: str,
        units: int,
        idem_key: str,
        *,
        from_reserved: int | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        dup = self._dedupe(idem_key)
        if dup:
            return dup
        pool, grant = self._grant(grant_id)
        self._require_active(grant)
        if units <= 0 or units > grant.reserved + grant.claimed:
            raise ServiceError("释放数量超过未用额度")
        # 默认先释放已领取部分；调用方可显式指定来源构成，守恒由投影状态机保证。
        from_reserved = max(0, units - grant.claimed) if from_reserved is None else from_reserved
        from_claimed = units - from_reserved
        if from_reserved > grant.reserved or from_claimed < 0 or from_claimed > grant.claimed:
            raise ServiceError("释放来源与额度状态不符")
        self.ledger.append(
            "grant_released",
            {
                "grant_id": grant_id,
                "pool_id": pool.pool_id,
                "units": units,
                "from_reserved": from_reserved,
                "idem_key": idem_key,
            },
            ts,
        )
        self.state = replay(self.ledger.events)
        return self._result(idem_key)

    def extend(self, grant_id: str, new_expires_at: str, idem_key: str, *, ts: str | None = None) -> dict[str, Any]:
        dup = self._dedupe(idem_key)
        if dup:
            return dup
        pool, grant = self._grant(grant_id)
        self._require_active(grant)
        if grant.expires_at is not None and new_expires_at <= grant.expires_at:
            raise ServiceError("延期只能把到期时间推后")
        # R-QUOTA-2：延期不迁移、不新增任何算力单位。
        self.ledger.append(
            "grant_extended",
            {
                "grant_id": grant_id,
                "pool_id": pool.pool_id,
                "units": 0,
                "new_expires_at": new_expires_at,
                "idem_key": idem_key,
            },
            ts,
        )
        self.state = replay(self.ledger.events)
        return self._result(idem_key)

    @staticmethod
    def _require_active(grant: GrantState) -> None:
        if grant.status != "active":
            raise ServiceError("额度已失效，不能继续操作")

    def _result(self, idem_key: str) -> dict[str, Any]:
        event_type, seq = self.state.idem_index[idem_key]
        return {"replayed": False, "idem_key": idem_key, "event_type": event_type, "seq": seq}

    # ---- 停机接续（R-RESUME）-------------------------------------------

    def due_expirations(self, as_of_ts: str) -> dict[str, list[str]]:
        """返回截至某时刻应处理的到期额度与授权，不修改台账。"""
        grants = [
            (pool.pool_id, grant.grant_id)
            for pool in self.state.pools.values()
            for grant in pool.grants.values()
            if grant.status == "active"
            and grant.expires_at is not None
            and grant.expires_at <= as_of_ts
        ]
        accesses = [
            access.access_id
            for access in self.state.active_accesses()
            if access.valid_until <= as_of_ts
        ]
        return {"grants": grants, "accesses": accesses}

    def resume(self, as_of_ts: str) -> dict[str, Any]:
        """重启后接续：到期失效并释放预留/领取占用；未决评审重新入队。

        重放确定性：先重放旧台账，再按业务时间补处理，结果与连续在线等价。
        到期额度仅退回未用的预留/已领取单位，已核对用量保持占用（R-QUOTA）。
        """
        expired_grants: list[str] = []
        expired_accesses: list[str] = []
        for pool_id, grant_id in self.due_expirations(as_of_ts)["grants"]:
            grant = self.state.pools[pool_id].grants[grant_id]
            outstanding = grant.reserved + grant.claimed
            self.ledger.append(
                "grant_expired",
                {
                    "grant_id": grant_id,
                    "pool_id": pool_id,
                    "units": outstanding,
                    "from_reserved": grant.reserved,
                },
                as_of_ts,
            )
            expired_grants.append(grant_id)
        for access_id in self.due_expirations(as_of_ts)["accesses"]:
            self.ledger.append("access_expired", {"access_id": access_id}, as_of_ts)
            expired_accesses.append(access_id)
        self.state = replay(self.ledger.events)
        return {
            "expired_grants": expired_grants,
            "expired_accesses": expired_accesses,
            "pending_reviews": self.state.pending_revisions(),
        }

    # ---- 隐私检索委托 ---------------------------------------------------

    def search_resources(self, role: str, track: str, keyword: str) -> list[dict[str, Any]]:
        return privacy.search_resources(self.state, role, track, keyword)

    def get_resource(self, role: str, track: str, resource_id: str, version: str) -> dict[str, Any] | None:
        return privacy.get_resource(self.state, role, track, resource_id, version)
