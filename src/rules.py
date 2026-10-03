"""分赛道、按版本发布的评审规则引擎（R-RULE）。

工业一线 / 安全研究 / 公共服务使用不同规则集；规则只能以更高版本追加发布。
引擎对一份申请版本逐条评估条款，输出可追溯的评估依据；它只做判定，不写台账。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 敏感等级：数值越大越敏感。normal 可公开搜索；restricted 限专区；secret 严格授权。
SENSITIVITY_ORDER = {"normal": 0, "restricted": 1, "secret": 2}

TRACKS = ("industrial", "security", "public_service")
TRACK_NAMES = {
    "industrial": "工业一线",
    "security": "安全研究",
    "public_service": "公共服务",
}


@dataclass(frozen=True)
class ClauseResult:
    clause: str
    title: str
    passed: bool
    detail: str
    waivable: bool = False


@dataclass
class Evaluation:
    track: str
    rules_version: int
    results: list[ClauseResult] = field(default_factory=list)

    @property
    def failed(self) -> list[ClauseResult]:
        return [r for r in self.results if not r.passed]

    @property
    def waivable_failures(self) -> list[ClauseResult]:
        return [r for r in self.failed if r.waivable]

    def to_basis(self) -> list[dict[str, Any]]:
        return [
            {
                "clause": r.clause,
                "title": r.title,
                "passed": r.passed,
                "detail": r.detail,
                "waivable": r.waivable,
            }
            for r in self.results
        ]


def publish_rules(state: Any, track: str, version: int, effective_ts: str, config: dict[str, Any]) -> None:
    """命令侧校验：赛道合法、版本只能递增（R-RULE-1/2）。投影由事件完成。"""
    if track not in TRACKS:
        raise ValueError(f"未知行业赛道：{track}")
    existing = state.rules.get(track, [])
    if any(ruleset["version"] == version for ruleset in existing):
        raise ValueError(f"{track}规则版本{version}已存在，规则只能以新版本发布")
    if existing and version <= max(ruleset["version"] for ruleset in existing):
        raise ValueError("规则版本号必须严格递增")
    required = {"min_units", "max_units", "max_sensitivity", "require_purpose", "allow_zone"}
    missing = required - set(config)
    if missing:
        raise ValueError(f"规则配置缺少条款：{sorted(missing)}")


def evaluate(
    state: Any,
    content: dict[str, Any],
    ruleset: dict[str, Any],
    available_units: int,
) -> Evaluation:
    """按给定规则版本评估申请内容。

    ``ruleset`` 必须是决定作出时刻生效的版本（由调用方经
    ``DomainState.rules_effective`` 选取），保证规则变化不溯及既往（R-RULE-3）。
    """
    config = ruleset["config"]
    evaluation = Evaluation(track=ruleset.get("track", content["track"]), rules_version=ruleset["version"])
    results = evaluation.results

    # C-PURPOSE：用途说明必填（公共服务与安全研究通常要求）。
    purpose = (content.get("purpose") or "").strip()
    need_purpose = config["require_purpose"]
    results.append(
        ClauseResult(
            "C-PURPOSE",
            "项目用途说明",
            passed=not need_purpose or bool(purpose),
            detail="已说明用途" if purpose else "缺少项目用途说明",
        )
    )

    # C-UNITS：算力需求在该赛道允许区间内。区间条款可经例外放宽（waivable）。
    units = content["compute_units"]
    in_range = config["min_units"] <= units <= config["max_units"]
    results.append(
        ClauseResult(
            "C-UNITS",
            "算力额度区间",
            passed=in_range,
            detail=(
                f"申请{units}单位，区间{config['min_units']}–{config['max_units']}"
                if in_range
                else f"申请{units}单位超出区间{config['min_units']}–{config['max_units']}"
            ),
            waivable=True,
        )
    )

    # C-RESOURCE：申请的每个资源版本都存在。
    missing = [
        f"{item['resource_id']}@{item['version']}"
        for item in content["resources"]
        if state.resource_revision(item["resource_id"], item["version"]) is None
    ]
    results.append(
        ClauseResult(
            "C-RESOURCE",
            "资源版本存在",
            passed=not missing,
            detail="全部资源版本存在" if not missing else f"资源版本不存在：{', '.join(missing)}",
        )
    )

    # C-SENSITIVITY：资源敏感等级与专区属性在该赛道规则允许范围内。
    allowed_level = SENSITIVITY_ORDER[config["max_sensitivity"]]
    allow_zone = config["allow_zone"]
    over = []
    for item in content["resources"]:
        revision = state.resource_revision(item["resource_id"], item["version"])
        if revision is None:
            continue
        level = SENSITIVITY_ORDER[revision["sensitivity"]]
        if level > allowed_level or (revision.get("zone") and not allow_zone):
            over.append(f"{item['resource_id']}@{item['version']}")
    results.append(
        ClauseResult(
            "C-SENSITIVITY",
            "敏感资源准入",
            passed=not over,
            detail="资源敏感等级与专区均在允许范围" if not over else f"超出本赛道准入：{', '.join(over)}",
        )
    )

    # C-CAPACITY：配额池余量充足。容量不可经例外突破（不可放宽，见 R-QUOTA-1）。
    enough = units <= available_units
    results.append(
        ClauseResult(
            "C-CAPACITY",
            "配额池容量",
            passed=enough,
            detail=f"池余量{available_units}单位，申请{units}单位" if enough
            else f"池余量仅{available_units}单位，无法满足{units}单位",
        )
    )
    return evaluation
