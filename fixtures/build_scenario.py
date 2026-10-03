"""构造资源开放服务的演示台账（全部为合成数据，无真实身份信息）。

运行：python3 -m fixtures.build_scenario
生成：fixtures/scenario.ledger.json
场景覆盖：分赛道规则、规则升级不溯及既往、重复申请幂等、内容变更新版本、
回避与例外双签、配额守恒与反事实挤占、敏感资源存在性隐藏、到期停机接续。
"""

from __future__ import annotations

from pathlib import Path

from src.ledger import Ledger
from src.service import ResourceService

OUTPUT = Path("fixtures/scenario.ledger.json")


def build() -> Ledger:
    ledger = Ledger()
    svc = ResourceService(ledger)

    # 机构分属三个群体，用于公平开放度量。
    svc.register_org("org-factory", "示例重型机械厂", "工业一线", ts="2026-09-01T00:00:00+00:00")
    svc.register_org("org-grid", "示例电网运检中心", "工业一线", ts="2026-09-01T00:01:00+00:00")
    svc.register_org("org-steel", "示例钢铁集团", "工业一线", ts="2026-09-01T00:02:00+00:00")
    svc.register_org("org-lab", "示例人工智能研究院", "科研院所", ts="2026-09-01T00:03:00+00:00")
    svc.register_org("org-sme", "示例初创科技小队", "中小团队", ts="2026-09-01T00:04:00+00:00")

    # 开发者。
    svc.register_developer("dev-fang", "方某（合成）", "org-factory", ts="2026-09-01T01:00:00+00:00")
    svc.register_developer("dev-min", "闵某（合成）", "org-grid", ts="2026-09-01T01:01:00+00:00")
    svc.register_developer("dev-wei", "卫某（合成）", "org-steel", ts="2026-09-01T01:02:00+00:00")
    svc.register_developer("dev-an", "安某（合成）", "org-lab", ts="2026-09-01T01:03:00+00:00")
    svc.register_developer("dev-qiao", "乔某（合成）", "org-sme", ts="2026-09-01T01:04:00+00:00")

    # 各赛道规则 v1 于 9 月 1 日生效（R-RULE-1）。工业一线 v1 上限 60。
    svc.publish_rule_set(
        "industrial", 1,
        {"min_units": 1, "max_units": 60, "max_sensitivity": "normal",
         "require_purpose": True, "allow_zone": False},
        effective_ts="2026-09-01T00:00:00+00:00", ts="2026-09-01T02:00:00+00:00",
    )
    svc.publish_rule_set(
        "security", 1,
        {"min_units": 1, "max_units": 120, "max_sensitivity": "restricted",
         "require_purpose": True, "allow_zone": True},
        effective_ts="2026-09-01T00:00:00+00:00", ts="2026-09-01T02:01:00+00:00",
    )
    svc.publish_rule_set(
        "public_service", 1,
        {"min_units": 1, "max_units": 80, "max_sensitivity": "normal",
         "require_purpose": True, "allow_zone": False},
        effective_ts="2026-09-01T00:00:00+00:00", ts="2026-09-01T02:02:00+00:00",
    )

    # 共享算力池容量 100（工业+安全），刻意制造挤占；公共服务单列池容量 120。
    svc.open_quota_pool("pool-2026q3", 100, "2026-07-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00",
                        ts="2026-09-01T02:03:00+00:00")
    svc.open_quota_pool("pool-public-q3", 120, "2026-07-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00",
                        ts="2026-09-01T02:04:00+00:00")

    # 项目（三个赛道）。
    svc.register_project("proj-weld", "焊缝缺陷视觉质检", "industrial", ts="2026-09-02T00:00:00+00:00")
    svc.register_project("proj-grid", "输电线路无人机巡检", "industrial", ts="2026-09-02T00:01:00+00:00")
    svc.register_project("proj-furnace", "高炉炉温预测", "industrial", ts="2026-09-02T00:02:00+00:00")
    svc.register_project("proj-redteam", "模型对抗红队评测", "security", ts="2026-09-02T00:03:00+00:00")
    svc.register_project("proj-elder", "社区适老问答助手", "public_service", ts="2026-09-02T00:04:00+00:00")
    svc.register_project("proj-crane", "起重机吊钩安全监测", "industrial", ts="2026-09-02T00:05:00+00:00")

    # 资源版本：普通模型/数据集 + 安全专区受限数据集。
    svc.publish_resource(
        "model-vision", "v1.2", "model", "示例视觉基座模型", "商用许可：署名，同一条件共享",
        ts="2026-09-03T00:00:00+00:00",
    )
    svc.publish_resource(
        "ds-weld", "v1", "dataset", "示例焊缝图像数据集", "仅限非商业研究，二次发布需申请",
        description="合成焊缝图像", ts="2026-09-03T00:01:00+00:00",
    )
    svc.publish_resource(
        "ds-redteam", "v2", "dataset", "示例对抗样本专区数据集", "仅限安全专区授权项目，禁止外传",
        sensitivity="restricted", zone="security",
        description="安全专区对抗样本", ts="2026-09-03T00:02:00+00:00",
    )
    svc.publish_resource(
        "model-vision", "v1.3", "model", "示例视觉基座模型", "商用许可：署名，同一条件共享",
        description="修复小目标召回", ts="2026-09-04T00:00:00+00:00",
    )

    # 09-05 申请一：工业一线 50 单位，按 v1 批准并预留（占用 50，余量 50）。
    key1 = svc.application_key(
        "dev-fang", "proj-weld", ["model-vision@v1.2", "ds-weld@v1"], "2026Q3"
    )
    svc.submit_application(
        "dev-fang", "proj-weld",
        [{"resource_id": "model-vision", "version": "v1.2"},
         {"resource_id": "ds-weld", "version": "v1"}],
        50, "生产线焊缝缺陷自动判级", "2026Q3", "pool-2026q3",
        ts="2026-09-05T02:00:00+00:00",
    )
    # 同内容重复提交：幂等返回原结果，不产生新事件（R-APP-2）。
    duplicate = svc.submit_application(
        "dev-fang", "proj-weld",
        [{"resource_id": "model-vision", "version": "v1.2"},
         {"resource_id": "ds-weld", "version": "v1"}],
        50, "生产线焊缝缺陷自动判级", "2026Q3", "pool-2026q3",
        ts="2026-09-05T02:05:00+00:00",
    )
    assert duplicate["replayed"] is True and duplicate["revision"] == 1
    svc.decide(key1, 1, "rev-industrial-1", approve=True,
               grant_id="grant-fang-q3", expires_at="2026-09-25T00:00:00+00:00",
               ts="2026-09-05T09:00:00+00:00")
    svc.claim("grant-fang-q3", 50, "idem-claim-fang", ts="2026-09-06T01:00:00+00:00")

    # 09-07 申请二：另一工业项目申请 55 单位。池余量 50，容量条款不通过被拒；
    # 反事实测算：若申请一不占 50，余量 100 足以容纳 55 —— 申请一构成挤占。
    key2 = svc.application_key("dev-min", "proj-grid", ["model-vision@v1.3"], "2026Q3")
    svc.submit_application(
        "dev-min", "proj-grid",
        [{"resource_id": "model-vision", "version": "v1.3"}],
        55, "输电线路无人机巡检缺陷识别", "2026Q3", "pool-2026q3",
        ts="2026-09-07T02:00:00+00:00",
    )
    svc.decide(key2, 1, "rev-industrial-1", approve=False, ts="2026-09-07T09:00:00+00:00")

    # 09-08 申请三：安全研究申请安全专区受限数据集，安全审核人可见并批准；
    # 普通评审人在搜索和直取中都无法推断该数据集存在（R-PRIV-1）。
    key3 = svc.application_key("dev-an", "proj-redteam", ["ds-redteam@v2"], "2026Q3")
    svc.submit_application(
        "dev-an", "proj-redteam",
        [{"resource_id": "ds-redteam", "version": "v2"}],
        30, "模型对抗鲁棒性红队评测", "2026Q3", "pool-2026q3",
        ts="2026-09-08T02:00:00+00:00",
    )
    svc.grant_access("access-an-redteam", "dev-an", "ds-redteam", "2026-09-30T00:00:00+00:00",
                     ts="2026-09-08T03:00:00+00:00")
    svc.decide(key3, 1, "rev-security-1", approve=True,
               grant_id="grant-an-q3", expires_at="2026-09-28T00:00:00+00:00",
               ts="2026-09-08T09:00:00+00:00")
    svc.claim("grant-an-q3", 30, "idem-claim-an", ts="2026-09-09T01:00:00+00:00")

    # 09-12 申请四：公共服务项目申请 90 单位（超公共服务上限 80，属可例外条款；
    # 公共服务池余量 120，容量充足）。必须走例外并由第二名评审人复核（R-REV-4）。
    key4 = svc.application_key("dev-qiao", "proj-elder", ["model-vision@v1.3"], "2026Q3")
    svc.submit_application(
        "dev-qiao", "proj-elder",
        [{"resource_id": "model-vision", "version": "v1.3"}],
        90, "社区适老多模态问答助手（公益）", "2026Q3", "pool-public-q3",
        ts="2026-09-12T02:00:00+00:00",
    )
    try:  # 不走例外直接批准 → 被拒绝执行
        svc.decide(key4, 1, "rev-public-1", approve=True, ts="2026-09-12T08:00:00+00:00")
        raise AssertionError("超上限批准必须走例外")
    except ValueError:
        pass
    try:  # 走例外但缺第二复核人 → 同样拒绝执行
        svc.decide(key4, 1, "rev-public-1", approve=True, exception=True,
                   ts="2026-09-12T08:30:00+00:00")
        raise AssertionError("例外必须双签")
    except ValueError:
        pass
    svc.decide(key4, 1, "rev-public-1", approve=True, exception=True,
               second_reviewer_id="rev-public-2",
               grant_id="grant-qiao-q3", expires_at="2026-10-20T00:00:00+00:00",
               ts="2026-09-12T09:00:00+00:00")

    # 09-13 申请（补充工业一线 v1 样本）：用途说明缺失，C-PURPOSE 不可放宽，拒绝。
    key6 = svc.application_key("dev-wei", "proj-furnace", ["model-vision@v1.3"], "2026Q3")
    svc.submit_application(
        "dev-wei", "proj-furnace",
        [{"resource_id": "model-vision", "version": "v1.3"}],
        20, "", "2026Q3", "pool-2026q3",
        ts="2026-09-13T02:00:00+00:00",
    )
    svc.decide(key6, 1, "rev-industrial-1", approve=False, ts="2026-09-13T09:00:00+00:00")

    # 09-20 工业规则升级 v2 生效：上限由 60 放宽到 100。新版本只影响之后的决定，
    # 申请一保留 v1 依据（R-RULE-2/3）。
    svc.publish_rule_set(
        "industrial", 2,
        {"min_units": 1, "max_units": 100, "max_sensitivity": "normal",
         "require_purpose": True, "allow_zone": False},
        effective_ts="2026-09-20T00:00:00+00:00",
    )
    # 申请一核对用量 30（占用状态：claimed 20 / used 30，总占用不变）；
    # 09-21 释放 10（claimed 余 10），总占用由 80 降为 70。
    svc.report_usage("grant-fang-q3", 30, "idem-usage-fang", ts="2026-09-20T01:00:00+00:00")
    svc.release("grant-fang-q3", 10, "idem-release-fang", ts="2026-09-21T01:00:00+00:00")

    # 09-22 申请五：工业项目在 v2 下申请 90（v1 上限 60 会挡，v2 上限 100 已放行条款），
    # 但共享池余量仅 30，容量仍不足 → 拒绝。规则放宽不创造算力，也不回溯旧决定。
    key5 = svc.application_key("dev-min", "proj-crane", ["model-vision@v1.3"], "2026Q3")
    svc.submit_application(
        "dev-min", "proj-crane",
        [{"resource_id": "model-vision", "version": "v1.3"}],
        90, "起重机吊钩安全监测推理", "2026Q3", "pool-2026q3",
        ts="2026-09-22T02:00:00+00:00",
    )
    svc.decide(key5, 1, "rev-industrial-2", approve=False, ts="2026-09-22T09:00:00+00:00")

    # 09-24 申请三到期前延期至 10-12（延期不新增任何单位，R-QUOTA-2）。
    svc.extend("grant-an-q3", "2026-10-12T00:00:00+00:00", "idem-extend-an",
               ts="2026-09-24T01:00:00+00:00")

    # 09-29 申请四同键内容变化（90→20 单位、二期用途）→ 不可变新版本 revision 2，
    # 已提交未评审；停机重启后须作为在途评审重新入队（R-APP-3 / R-RESUME-2）。
    svc.submit_application(
        "dev-qiao", "proj-elder",
        [{"resource_id": "model-vision", "version": "v1.3"}],
        20, "社区适老问答助手二期（公益）", "2026Q3", "pool-public-q3",
        ts="2026-09-29T02:00:00+00:00",
    )

    return ledger


def main() -> None:
    ledger = build()
    # 补处理 10-03 重启前已过窗口的到期事件（申请一额度到期、安全授权到期）。
    # 申请一仍有 10 单位已领取未核对，到期回收入池；已核对 30 单位永久占用（R-RESUME-1）。
    svc = ResourceService(ledger)
    result = svc.resume("2026-10-03T00:00:00+00:00")
    ledger.validate()
    ledger.save(OUTPUT)
    print(f"已生成 {OUTPUT}，共 {len(ledger.events)} 条事件")
    print(f"停机接续：到期额度{result['expired_grants']}，到期授权{result['expired_accesses']}")
    print(f"重新入队评审：{result['pending_reviews']}")


if __name__ == "__main__":
    main()
