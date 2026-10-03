"""运营读模型：决定解释（R-EXPLAIN）与公平开放度量（R-FAIR）。

两个读模型都由台账重放生成，不接受手工编辑（R-EXPLAIN-4）。
涉及敏感资源的明细按下发角色脱敏，小样本群体被抑制（R-PRIV-2 / R-FAIR-3）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .ledger import Ledger
from .projections import DomainState, replay
from .rules import TRACK_NAMES
from . import privacy

SMALL_SAMPLE_THRESHOLD = 3
RESTRICTED_BUCKET = "安全专区敏感资源（身份已隐藏）"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _org_group(state: DomainState, dev_id: str) -> str:
    developer = state.developers[dev_id]
    return state.orgs[developer["org_id"]]["group"]


def _pool_id_of_submission(ledger: Ledger, app_key: str, revision_no: int) -> str | None:
    for event in ledger.events:
        if (
            event.type == "application_submitted"
            and event.payload["app_key"] == app_key
            and event.payload["revision"] == revision_no
        ):
            return event.payload["pool_id"]
    return None


def explain_decision(ledger: Ledger, app_key: str, revision_no: int, role: str = "ops") -> dict[str, Any]:
    """给出某申请版本为何获批/被拒的完整解释（R-EXPLAIN-1..4）。"""
    final_state = replay(ledger.events)
    application = final_state.applications.get(app_key)
    if application is None or revision_no not in application.revisions:
        raise ValueError("申请版本不存在")
    revision = application.revisions[revision_no]
    if revision.decision is None:
        raise ValueError("该申请版本尚未作出决定，暂无可解释内容")
    decision = revision.decision
    content = revision.content

    # R-EXPLAIN-2：使用的资源版本与许可范围；敏感身份对无权角色隐藏（R-PRIV）。
    resources_view = []
    for item in content["resources"]:
        view = privacy.get_resource(final_state, role, content["track"], item["resource_id"], item["version"])
        if view is None:
            resources_view.append(
                {"resource_id": item["resource_id"], "version": item["version"], "name": RESTRICTED_BUCKET}
            )
        else:
            resources_view.append(
                {
                    "resource_id": view["resource_id"],
                    "version": view["version"],
                    "name": view["name"],
                    "license": view["license"],
                    "sensitivity": view["sensitivity"],
                }
            )

    # R-EXPLAIN-3：决定作出前一刻的池状态与在队申请，用前缀重放精确还原。
    decision_seq = max(
        event.seq for event in ledger.events
        if event.type == "review_decided"
        and event.payload["app_key"] == app_key
        and event.payload["revision"] == revision_no
    )
    before = replay(ledger.events[: decision_seq - 1])
    pool_id = decision["pool_id"]
    pool_before = before.pools[pool_id]
    queue_at_decision = []
    for queued_key, queued_rev in before.pending_revisions():
        if _pool_id_of_submission(ledger, queued_key, queued_rev) == pool_id:
            queued = before.applications[queued_key].revisions[queued_rev]
            queue_at_decision.append(
                {
                    "app_key": queued_key,
                    "revision": queued_rev,
                    "group": _org_group(before, queued.content["dev_id"]),
                    "compute_units": queued.content["compute_units"],
                }
            )

    # 同一池周期内因容量不足被拒的申请，逐个做反事实测算：
    # 若在被拒时刻没有本额度占用其单位，申请能否被容纳？能，则本额度构成挤占。
    own_grant_id = next(
        (
            event.payload["grant_id"]
            for event in ledger.events
            if event.type == "grant_reserved"
            and event.payload["app_key"] == app_key
            and event.payload["revision"] == revision_no
        ),
        None,
    )
    capacity_rejected = []
    for event in ledger.events:
        if event.type != "review_decided" or event.payload["pool_id"] != pool_id:
            continue
        if event.payload["approve"]:
            continue
        capacity_clause = next(
            (b for b in event.payload["basis"] if b["clause"] == "C-CAPACITY"), None
        )
        if capacity_clause is None or capacity_clause["passed"]:
            continue
        rejected_content = _content_at(final_state, event.payload["app_key"], event.payload["revision"])
        # 在该拒绝决定作出前一刻重放，取当时池余量与本额度占用量。
        at_reject = replay(ledger.events[: event.seq - 1])
        pool_at_reject = at_reject.pools[pool_id]
        own_occupation = 0
        if own_grant_id is not None and own_grant_id in pool_at_reject.grants:
            own = pool_at_reject.grants[own_grant_id]
            if own.status == "active":
                own_occupation = own.reserved + own.claimed + own.used
        requested = rejected_content["compute_units"]
        counterfactual_fit = pool_at_reject.available + own_occupation >= requested
        capacity_rejected.append(
            {
                "app_key": event.payload["app_key"],
                "revision": event.payload["revision"],
                "group": _org_group(final_state, rejected_content["dev_id"]),
                "compute_units": requested,
                "decided_ts": event.payload["decided_ts"],
                "pool_available_then": pool_at_reject.available,
                "this_grant_occupied_then": own_occupation,
                "counterfactual_fit_without_this_grant": counterfactual_fit,
                "detail": capacity_clause["detail"],
            }
        )

    pool_after = final_state.pools[pool_id]
    approved = decision["approve"]
    displaced = approved and any(
        item["counterfactual_fit_without_this_grant"] for item in capacity_rejected
    )
    conclusion = (
        f"于{decision['decided_ts']}按{TRACK_NAMES.get(decision['rules_track'], decision['rules_track'])}"
        f"规则v{decision['rules_version']}{'例外批准' if decision['exception'] else '批准'}，"
        f"批准时池余量{decision['pool_available_at_decision']}单位。"
        if approved
        else f"于{decision['decided_ts']}按规则v{decision['rules_version']}拒绝，"
        f"未通过：{'、'.join(b['clause'] for b in decision['basis'] if not b['passed'])}。"
    )
    if displaced:
        conclusion += " 反事实测算表明：同期有申请仅因本额度占用而无法容纳，本额度构成对他人的挤占，详见 contention。"
    elif approved:
        conclusion += " 同期容量拒绝即便扣除本额度仍无法容纳，本额度未构成决定性挤占。"

    return {
        "app_key": app_key,
        "revision": revision_no,
        "outcome": "exception_approved" if decision["exception"] else "approved" if approved else "rejected",
        "conclusion": conclusion,
        "decided_ts": decision["decided_ts"],
        "reviewer_id": decision["reviewer_id"],
        "second_reviewer_id": decision["second_reviewer_id"],
        "rules": {"track": decision["rules_track"], "version": decision["rules_version"]},
        "basis": decision["basis"],
        "resources": resources_view,
        "quota": {
            "pool_id": pool_id,
            "capacity": pool_after.capacity,
            "requested_units": content["compute_units"],
            "pool_available_at_decision": decision["pool_available_at_decision"],
            "pool_occupied_now": pool_after.occupied,
        },
        "contention": {
            "queue_at_decision": queue_at_decision,
            "capacity_rejected": capacity_rejected,
            "displaced_others": displaced,
        },
    }


def _content_at(state: DomainState, app_key: str, revision_no: int) -> dict[str, Any]:
    return state.applications[app_key].revisions[revision_no].content


def fairness_report(ledger: Ledger, role: str = "ops") -> dict[str, Any]:
    """按群体 × 决定时规则版本分段统计公平开放指标（R-FAIR-1/2）。

    小样本群体返回 suppressed=True 且不给出比率，避免反推个体（R-FAIR-3）。
    """
    state = replay(ledger.events)

    # 标记延期与到期的额度（R-FAIR-1 到期前可续期率）。
    extended_grants: set[tuple[str, str]] = set()
    expired_grants: set[tuple[str, str]] = set()
    for event in ledger.events:
        if event.type == "grant_extended":
            extended_grants.add((event.payload["pool_id"], event.payload["grant_id"]))
        if event.type == "grant_expired":
            expired_grants.add((event.payload["pool_id"], event.payload["grant_id"]))

    buckets: dict[tuple[str, int], dict[str, Any]] = defaultdict(
        lambda: {
            "applications": 0,
            "approved": 0,
            "rejected": 0,
            "wait_seconds": [],
            "requested_units": 0,
            "approved_units": 0,
            "grants_expired_unextended": 0,
            "grants_extended_before_expiry": 0,
            "applications_ref": [],
        }
    )

    for app_key, application in sorted(state.applications.items()):
        for revision_no, revision in sorted(application.revisions.items()):
            if revision.decision is None:
                continue  # 在途申请不计入已决定群体的比率
            content = revision.content
            group = _org_group(state, content["dev_id"])
            key = (group, revision.decision["rules_version"])
            bucket = buckets[key]
            bucket["applications"] += 1
            bucket["requested_units"] += content["compute_units"]
            wait = (_parse(revision.decision["decided_ts"]) - _parse(revision.submitted_ts)).total_seconds()
            bucket["wait_seconds"].append(max(0, wait))
            if revision.decision["approve"]:
                bucket["approved"] += 1
                bucket["approved_units"] += content["compute_units"]
            else:
                bucket["rejected"] += 1
            bucket["applications_ref"].append({"app_key": app_key, "revision": revision_no})

    # 续期率按额度归属群体汇总到对应规则版本：
    # 分母 = 已到期未延期 + 到期前完成延期；后者即"到期前可续期"的成功样本。
    grant_to_decision: dict[tuple[str, str], tuple[str, int]] = {}
    for event in ledger.events:
        if event.type == "grant_reserved":
            app_key = event.payload["app_key"]
            rev = event.payload["revision"]
            decision = state.applications[app_key].revisions[rev].decision
            grant_to_decision[(event.payload["pool_id"], event.payload["grant_id"])] = (
                _org_group(state, revision_content(state, app_key, rev)["dev_id"]),
                decision["rules_version"],
            )
    all_grants = set(grant_to_decision)
    for grant_ref in all_grants:
        group, rules_version = grant_to_decision[grant_ref]
        bucket = buckets[(group, rules_version)]
        if grant_ref in extended_grants:
            bucket["grants_extended_before_expiry"] += 1
        elif grant_ref in expired_grants:
            bucket["grants_expired_unextended"] += 1

    groups = []
    for (group, rules_version), bucket in sorted(buckets.items()):
        n = bucket["applications"]
        entry = {
            "group": group,
            "rules_version": rules_version,
            "applications": n,
            "drilldown_role": role,
        }
        if n < SMALL_SAMPLE_THRESHOLD:
            entry["suppressed"] = True
            entry["reason"] = f"样本少于{SMALL_SAMPLE_THRESHOLD}，按隐私规则抑制比率"
            groups.append(entry)
            continue
        waits = bucket["wait_seconds"]
        entry.update(
            {
                "suppressed": False,
                "approved": bucket["approved"],
                "rejected": bucket["rejected"],
                "approval_rate": round(bucket["approved"] / n, 4),
                "average_wait_seconds": round(sum(waits) / len(waits), 1),
                "fill_rate": (
                    round(bucket["approved_units"] / bucket["requested_units"], 4)
                    if bucket["requested_units"]
                    else None
                ),
                "renewal_before_expiry_rate": (
                    round(
                        bucket["grants_extended_before_expiry"]
                        / (bucket["grants_extended_before_expiry"] + bucket["grants_expired_unextended"]),
                        4,
                    )
                    if (bucket["grants_extended_before_expiry"] + bucket["grants_expired_unextended"])
                    else None
                ),
            }
        )
        if role == "ops":
            entry["applications_ref"] = bucket["applications_ref"]  # R-FAIR-4 可下钻
        groups.append(entry)

    return {"segmentation": "group × rules_version_at_decision", "groups": groups}


def revision_content(state: DomainState, app_key: str, revision_no: int) -> dict[str, Any]:
    return state.applications[app_key].revisions[revision_no].content
