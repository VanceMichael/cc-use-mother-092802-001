"""最小知情与敏感资源存在性隐藏（R-REV-2 / R-PRIV）。

无权人员既看不到敏感资源内容，也不能通过搜索、直取或错误提示推断其存在：
对无权方，命中受限资源与"根本不存在"返回不可区分的结果（R-PRIV-1）。
"""

from __future__ import annotations

from typing import Any

# 评审角色到可见敏感等级的映射；仅在评审安全研究申请时安全审核人可见专区资源。
ROLE_SENSITIVITY_CEILING = {
    "industry_reviewer": "normal",
    "public_service_reviewer": "normal",
    "security_reviewer": "restricted",
    "ops": "secret",  # 运营解释职责，可见全部元数据
}


def can_see_sensitivity(role: str, sensitivity: str, zone: str | None, track: str) -> bool:
    """该评审角色在该赛道评审语境下能否看到某敏感等级/专区资源的存在。"""
    from .rules import SENSITIVITY_ORDER

    ceiling = ROLE_SENSITIVITY_CEILING.get(role, "normal")
    if SENSITIVITY_ORDER[sensitivity] > SENSITIVITY_ORDER[ceiling]:
        return False
    if zone == "security" and role != "security_reviewer" and role != "ops":
        return False
    if zone == "security" and role == "security_reviewer" and track != "security":
        return False
    return True


def _developer_can_see(state: Any, dev_id: str, revision: dict[str, Any]) -> bool:
    """开发者只能看到普通资源，以及持有**有效**访问授权的敏感资源（R-PRIV-3）。

    授权到期或撤销后，敏感资源对该开发者立即恢复为不可见。
    """
    if revision["sensitivity"] == "normal" and not revision.get("zone"):
        return True
    return any(
        access.status == "active"
        and access.dev_id == dev_id
        and access.resource_id == revision["resource_id"]
        for access in state.accesses.values()
    )


def _viewer_can_see(state: Any, role: str, track: str, revision: dict[str, Any]) -> bool:
    if role.startswith("developer:"):
        return _developer_can_see(state, role.split(":", 1)[1], revision)
    return can_see_sensitivity(role, revision["sensitivity"], revision.get("zone"), track)


def visible_resource_revisions(
    state: Any, role: str, track: str
) -> list[dict[str, Any]]:
    """按角色返回可见资源版本；受限版本对无权方直接缺席，如同不存在。"""
    visible = []
    for resource in state.resources.values():
        for revision in resource["revisions"].values():
            if _viewer_can_see(state, role, track, revision):
                visible.append(_public_view(revision))
    return visible


def search_resources(state: Any, role: str, track: str, keyword: str) -> list[dict[str, Any]]:
    """关键词搜索。

    受限命中在过滤阶段被移除，且不返回任何"存在但无权"的提示——
    搜索结果与该资源不存在时完全一致（R-PRIV-1）。
    """
    keyword = keyword.strip().lower()
    hits = []
    for revision in visible_resource_revisions(state, role, track):
        blob = f"{revision['resource_id']} {revision['name']} {revision.get('description', '')}".lower()
        if keyword in blob:
            hits.append(revision)
    return hits


def get_resource(state: Any, role: str, track: str, resource_id: str, version: str) -> dict[str, Any] | None:
    """按标识直取。无权时返回 None，与资源不存在不可区分（不抛权限错误）。"""
    revision = state.resource_revision(resource_id, version)
    if revision is None:
        return None
    if not _viewer_can_see(state, role, track, revision):
        return None
    return _public_view(revision)


def _public_view(revision: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_id": revision["resource_id"],
        "version": revision["version"],
        "kind": revision["kind"],
        "name": revision["name"],
        "description": revision.get("description", ""),
        "sensitivity": revision["sensitivity"],
        "zone": revision.get("zone"),
        "license": revision["license"],
    }


def review_packet(
    state: Any,
    app_key: str,
    revision_no: int,
    role: str,
) -> dict[str, Any]:
    """为评审人生成履职所需资料包（need-to-know，R-REV-2）。

    - 只含被评审的这一个申请版本，不含开发者其他项目；
    - 不含池内余量等内部运营字段（评审只判断资格，容量由 C-CAPACITY 单独处理）；
    - 申请人无权看到的敏感资源，在资料包中以"资源版本不存在"呈现，
      从而评审人无法借资料包推断其存在。
    """
    application = state.applications.get(app_key)
    if application is None or revision_no not in application.revisions:
        raise ValueError("申请版本不存在")
    revision = application.revisions[revision_no]
    content = revision.content

    resources_view = []
    for item in content["resources"]:
        view = get_resource(state, role, content["track"], item["resource_id"], item["version"])
        resources_view.append(
            view if view is not None
            else {"resource_id": item["resource_id"], "version": item["version"], "status": "not_found"}
        )

    developer = state.developers[content["dev_id"]]
    org = state.orgs[developer["org_id"]]
    return {
        "app_key": app_key,
        "revision": revision_no,
        "track": content["track"],
        "project": {
            "project_id": content["project_id"],
            "name": state.projects[content["project_id"]]["name"],
        },
        "applicant": {"dev_id": developer["dev_id"], "org_group": org["group"]},
        "purpose": content["purpose"],
        "compute_units": content["compute_units"],
        "resources": resources_view,
    }
