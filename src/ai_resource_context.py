"""读取并校验央企人工智能资源普惠开放治理的领域上下文。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DOMAIN = "inclusive-ai-resource-hub"
REQUIRED_KEYS = frozenset(
    {
        "domain",
        "version",
        "actors",
        "facts",
        "constraints",
        "record_chain",
        "rule_tracks",
        "continuity",
    }
)
# v2 起生效的最低内容规模
MINIMUMS = {
    "actors": 3,
    "facts": 3,
    "constraints": 12,
}
# 约束必须覆盖的业务标签（与 docs/domain-rules.md 的边界一致）
CONSTRAINT_TAGS = (
    "资源与许可版本",
    "申请评审",
    "规则时点",
    "轨道分治",
    "评审回避",
    "最小知情",
    "存在性遮蔽",
    "申请幂等",
    "内容变更版本化",
    "配额守恒",
    "停机接续",
    "公平性解释",
)
# 三类规则轨道
RULE_TRACK_NAMES = ("工业一线", "安全研究", "公共服务")
# 停机后必须接续的对象
CONTINUITY_OBJECTS = ("即将到期的授权", "未完成的评审", "用量核对")
# 示例资料禁止出现的真实身份痕迹（粗粒度的形态检查，而非真实数据黑名单）
_FORBIDDEN_PATTERNS = ("@", "http://", "https://", "身份证", "手机号", "密码", "密钥", "token")


def _ensure_non_empty_strings(values: Any, label: str, minimum: int) -> list[str]:
    if not isinstance(values, list) or len(values) < minimum:
        raise ValueError(f"{label}内容不足")
    entries: list[str] = []
    for entry in values:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{label}包含空内容")
        entries.append(entry)
    return entries


def load_context(path: Path) -> dict[str, Any]:
    """读取领域资料，并拒绝缺字段、错领域、版本过期或结构不完整的资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != REQUIRED_KEYS:
        raise ValueError("领域资料字段不完整")
    if value["domain"] != DOMAIN:
        raise ValueError("领域标识不一致")
    if not isinstance(value["version"], int) or isinstance(value["version"], bool) or value["version"] < 2:
        raise ValueError("资料版本无效")
    for key, minimum in MINIMUMS.items():
        _ensure_non_empty_strings(value[key], key, minimum)

    constraints = value["constraints"]
    tags_present = {tag: any(f"【{tag}】" in entry for entry in constraints) for tag in CONSTRAINT_TAGS}
    missing = [tag for tag, present in tags_present.items() if not present]
    if missing:
        raise ValueError(f"约束缺少业务标签：{'、'.join(missing)}")

    _validate_record_chain(value["record_chain"])
    _validate_rule_tracks(value["rule_tracks"])
    _validate_continuity(value["continuity"])
    _ensure_no_real_identity(value)
    return value


def _validate_record_chain(chain: Any) -> None:
    if not isinstance(chain, list) or len(chain) < 8:
        raise ValueError("记录链路环节不足")
    seen_orders: list[int] = []
    for node in chain:
        if not isinstance(node, dict) or set(node) != {"order", "link", "records"}:
            raise ValueError("记录链路结构不完整")
        order = node["order"]
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ValueError("记录链路顺序无效")
        if not isinstance(node["link"], str) or not node["link"].strip():
            raise ValueError("记录链路环节名称为空")
        _ensure_non_empty_strings(node["records"], "记录链路台账", 1)
        seen_orders.append(order)
    if seen_orders != list(range(1, len(chain) + 1)):
        raise ValueError("记录链路必须从1开始连续编号")


def _validate_rule_tracks(tracks: Any) -> None:
    if not isinstance(tracks, list) or len(tracks) < 3:
        raise ValueError("规则轨道不足")
    names: list[str] = []
    for track in tracks:
        if not isinstance(track, dict) or set(track) != {"name", "applies_to", "rule_points"}:
            raise ValueError("规则轨道结构不完整")
        if not isinstance(track["name"], str) or not track["name"].strip():
            raise ValueError("规则轨道名称为空")
        if not isinstance(track["applies_to"], str) or not track["applies_to"].strip():
            raise ValueError("规则轨道适用范围为空")
        _ensure_non_empty_strings(track["rule_points"], "规则要点", 1)
        names.append(track["name"])
    for required in RULE_TRACK_NAMES:
        if required not in names:
            raise ValueError(f"缺少规则轨道：{required}")


def _validate_continuity(continuity: Any) -> None:
    if not isinstance(continuity, list) or len(continuity) < 3:
        raise ValueError("停机接续对象不足")
    objects: list[str] = []
    for item in continuity:
        if not isinstance(item, dict) or set(item) != {"object", "resume_action", "must_not_lose"}:
            raise ValueError("停机接续结构不完整")
        if not isinstance(item["object"], str) or not item["object"].strip():
            raise ValueError("停机接续对象为空")
        if not isinstance(item["resume_action"], str) or not item["resume_action"].strip():
            raise ValueError("停机接续动作不能为空")
        _ensure_non_empty_strings(item["must_not_lose"], "不可丢失信息", 1)
        objects.append(item["object"])
    for required in CONTINUITY_OBJECTS:
        if required not in objects:
            raise ValueError(f"缺少停机接续对象：{required}")


def _ensure_no_real_identity(value: dict[str, Any]) -> None:
    """示例资料只能包含合成内容：拒绝明显的真实身份信息形态。"""
    flattened = json.dumps(value, ensure_ascii=False)
    lowered = flattened.lower()
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.lower() in lowered:
            raise ValueError(f"示例资料疑似包含真实身份信息：{pattern}")


def context_fingerprint(value: dict[str, Any]) -> str:
    """生成与键顺序无关的资料摘要，便于识别版本内容。"""
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
