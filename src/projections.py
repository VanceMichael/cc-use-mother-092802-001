"""从只追加台账重放出领域状态（R-LOG-4 / R-RESUME）。

投影只按事件顺序应用，不做业务裁决；业务校验在 ``service`` 层。
重放是确定性的：同一台账永远得到同一状态（R-RESUME-3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ledger import Event

# 额度单位的守恒状态：每一个算力单位在任一时刻处于四种状态之一。
# 预留 -> 领取 ->（核对用量 | 释放）；预留也可直接释放（含到期回收）。
UNIT_RESERVED = "reserved"
UNIT_CLAIMED = "claimed"
UNIT_USED = "used"
UNIT_RELEASED = "released"


@dataclass
class GrantState:
    grant_id: str
    app_key: str
    revision: int
    pool_id: str
    units: int = 0
    reserved: int = 0
    claimed: int = 0
    used: int = 0
    released: int = 0
    expires_at: str | None = None
    status: str = "active"  # active / expired
    claim_events: dict[str, str] = field(default_factory=dict)  # 幂等键 -> 事件类型


@dataclass
class PoolState:
    pool_id: str
    capacity: int = 0
    period_start: str | None = None
    period_end: str | None = None
    grants: dict[str, GrantState] = field(default_factory=dict)

    @property
    def occupied(self) -> int:
        """仍占用池容量的单位。

        活跃额度：预留 + 已领取 + 已核对用量；
        已到期额度：仅保留已核对用量（已永久消耗，不能再次分配），
        未用部分已在到期时退回池容量。
        """
        total = 0
        for grant in self.grants.values():
            total += grant.used
            if grant.status == "active":
                total += grant.reserved + grant.claimed
        return total

    @property
    def available(self) -> int:
        return self.capacity - self.occupied


@dataclass
class ApplicationRevision:
    revision: int
    submitted_ts: str
    content: dict[str, Any]
    fingerprint: str
    status: str = "submitted"  # submitted / decided / withdrawn
    decision: dict[str, Any] | None = None


@dataclass
class ApplicationState:
    app_key: str
    revisions: dict[int, ApplicationRevision] = field(default_factory=dict)

    @property
    def latest_revision(self) -> int:
        return max(self.revisions)

    def revision_with_fingerprint(self, fingerprint: str) -> int | None:
        for number, revision in self.revisions.items():
            if revision.fingerprint == fingerprint:
                return number
        return None


@dataclass
class AccessState:
    access_id: str
    dev_id: str
    resource_id: str
    valid_until: str
    status: str = "active"  # active / expired / revoked


@dataclass
class DomainState:
    orgs: dict[str, dict[str, Any]] = field(default_factory=dict)
    developers: dict[str, dict[str, Any]] = field(default_factory=dict)
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> {revisions:{}, versions:[]}
    rules: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # track -> 版本列表
    applications: dict[str, ApplicationState] = field(default_factory=dict)
    pools: dict[str, PoolState] = field(default_factory=dict)
    accesses: dict[str, AccessState] = field(default_factory=dict)
    # 命令幂等键 ->（事件类型, 事件序号），重复指令返回原结果（R-QUOTA-4 / R-RESUME-4）
    idem_index: dict[str, tuple[str, int]] = field(default_factory=dict)
    seq: int = 0

    def resource_revision(self, resource_id: str, version: str) -> dict[str, Any] | None:
        resource = self.resources.get(resource_id)
        if resource is None:
            return None
        return resource["revisions"].get(version)

    def rules_effective(self, track: str, ts: str) -> dict[str, Any] | None:
        """某时刻对某赛道生效的规则：生效时间不晚于 ts 的最高版本（R-RULE-4）。"""
        chosen = None
        for ruleset in self.rules.get(track, []):
            if ruleset["effective_ts"] <= ts and (chosen is None or ruleset["version"] > chosen["version"]):
                chosen = ruleset
        return chosen

    def pending_revisions(self) -> list[tuple[str, int]]:
        """已提交但尚未决定、未撤回的申请版本（停机后重新入队，R-RESUME-2）。"""
        pending = []
        for app_key, application in sorted(self.applications.items()):
            for number, revision in sorted(application.revisions.items()):
                if revision.status == "submitted":
                    pending.append((app_key, number))
        return pending

    def active_accesses(self) -> list[AccessState]:
        return [access for access in self.accesses.values() if access.status == "active"]

    def active_grants(self) -> list[GrantState]:
        return [
            grant
            for pool in self.pools.values()
            for grant in pool.grants.values()
            if grant.status == "active"
        ]


def apply_event(state: DomainState, event: Event) -> None:
    p = event.payload
    t = event.type

    if t == "organization_registered":
        state.orgs[p["org_id"]] = {"org_id": p["org_id"], "name": p["name"], "group": p["group"]}
    elif t == "developer_registered":
        state.developers[p["dev_id"]] = {
            "dev_id": p["dev_id"],
            "name": p["name"],
            "org_id": p["org_id"],
        }
    elif t == "project_registered":
        state.projects[p["project_id"]] = dict(p)
    elif t == "resource_published":
        resource = state.resources.setdefault(
            p["resource_id"], {"resource_id": p["resource_id"], "kind": p["kind"], "revisions": {}}
        )
        resource["revisions"][p["version"]] = dict(p)
    elif t == "rule_set_published":
        state.rules.setdefault(p["track"], []).append(
            {"version": p["version"], "effective_ts": p["effective_ts"], "config": p["config"]}
        )
    elif t == "quota_pool_opened":
        state.pools[p["pool_id"]] = PoolState(
            pool_id=p["pool_id"],
            capacity=p["capacity"],
            period_start=p["period_start"],
            period_end=p["period_end"],
        )
    elif t == "application_submitted":
        application = state.applications.setdefault(p["app_key"], ApplicationState(p["app_key"]))
        application.revisions[p["revision"]] = ApplicationRevision(
            revision=p["revision"],
            submitted_ts=event.ts,
            content={k: p[k] for k in ("project_id", "dev_id", "track", "resources", "compute_units", "purpose")},
            fingerprint=p["fingerprint"],
        )
    elif t == "application_withdrawn":
        state.applications[p["app_key"]].revisions[p["revision"]].status = "withdrawn"
    elif t == "review_decided":
        revision = state.applications[p["app_key"]].revisions[p["revision"]]
        revision.status = "decided"
        revision.decision = dict(p)
    elif t == "grant_reserved":
        pool = state.pools[p["pool_id"]]
        grant = GrantState(
            grant_id=p["grant_id"],
            app_key=p["app_key"],
            revision=p["revision"],
            pool_id=p["pool_id"],
            units=p["units"],
            reserved=p["units"],
            expires_at=p["expires_at"],
        )
        pool.grants[p["grant_id"]] = grant
    elif t in ("grant_claimed", "grant_released", "grant_extended", "usage_reported"):
        grant = state.pools[p["pool_id"]].grants[p["grant_id"]]
        _move_units(t, p, grant)
        grant.claim_events[p["idem_key"]] = t
        state.idem_index[p["idem_key"]] = (t, event.seq)
        if t == "grant_extended":
            grant.expires_at = p["new_expires_at"]
    elif t in ("grant_expired", "access_expired", "access_revoked"):
        if t == "grant_expired":
            grant = state.pools[p["pool_id"]].grants[p["grant_id"]]
            _move_units(t, p, grant)
            grant.status = "expired"
            grant.expires_at = event.ts
        else:
            access = state.accesses[p["access_id"]]
            access.status = "expired" if t == "access_expired" else "revoked"
    elif t == "access_granted":
        state.accesses[p["access_id"]] = AccessState(
            access_id=p["access_id"],
            dev_id=p["dev_id"],
            resource_id=p["resource_id"],
            valid_until=p["valid_until"],
        )
    else:  # pragma: no cover - 未知事件类型属于编程错误
        raise ValueError(f"未知事件类型：{t}")

    state.seq = event.seq


def _move_units(event_type: str, p: dict[str, Any], grant: GrantState) -> None:
    """按守恒状态机迁移单位；投影层只做迁移，合法性已在 service 层判定。"""
    if event_type == "grant_claimed":
        units = p["units"]
        grant.reserved -= units
        grant.claimed += units
    elif event_type == "usage_reported":
        units = p["units"]
        grant.claimed -= units
        grant.used += units
    elif event_type in ("grant_released", "grant_expired"):
        units = p["units"]
        # 释放/到期来源记录在 payload 中：先减已领取，再减预留。
        from_reserved = p.get("from_reserved", 0)
        from_claimed = units - from_reserved
        grant.reserved -= from_reserved
        grant.claimed -= from_claimed
        grant.released += units
    # grant_extended 不迁移任何单位（R-QUOTA-2 延期不新增算力）


def replay(events: list[Event]) -> DomainState:
    state = DomainState()
    for event in events:
        apply_event(state, event)
    return state
