"""资源开放服务领域规则的逐条验证。

测试与 docs/domain-rules.md 的规则编号一一对应：
R-LOG 连续记录 / R-RULE 规则版本 / R-APP 申请幂等与版本 /
R-REV 评审隔离 / R-PRIV 存在性隐藏 / R-QUOTA 配额守恒 /
R-RESUME 停机接续 / R-EXPLAIN 可解释 / R-FAIR 公平度量。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.ledger import Ledger
from src.projections import replay
from src.service import ResourceService, ServiceError
from src import readmodels
from src import privacy

FIXTURE_LEDGER = Path("fixtures/scenario.ledger.json")

INDUSTRIAL_V1 = {
    "min_units": 1, "max_units": 60, "max_sensitivity": "normal",
    "require_purpose": True, "allow_zone": False,
}
SECURITY_V1 = {
    "min_units": 1, "max_units": 120, "max_sensitivity": "restricted",
    "require_purpose": True, "allow_zone": True,
}
PUBLIC_V1 = {
    "min_units": 1, "max_units": 80, "max_sensitivity": "normal",
    "require_purpose": True, "allow_zone": False,
}


def bootstrap() -> ResourceService:
    """构造最小可用服务：机构/开发者/项目/资源/规则/配额池。"""
    svc = ResourceService(Ledger())
    svc.register_org("org-a", "甲厂（合成）", "工业一线", ts="2026-09-01T00:00:00+00:00")
    svc.register_org("org-b", "乙院（合成）", "科研院所", ts="2026-09-01T00:01:00+00:00")
    svc.register_developer("dev-a", "甲（合成）", "org-a", ts="2026-09-01T01:00:00+00:00")
    svc.register_developer("dev-b", "乙（合成）", "org-b", ts="2026-09-01T01:01:00+00:00")
    svc.register_project("proj-a", "工业项目", "industrial", ts="2026-09-02T00:00:00+00:00")
    svc.register_project("proj-b", "安全项目", "security", ts="2026-09-02T00:01:00+00:00")
    svc.publish_resource("model-x", "v1", "model", "普通模型", "普通许可",
                         ts="2026-09-03T00:00:00+00:00")
    svc.publish_resource("ds-secret", "v1", "dataset", "专区数据集", "专区授权",
                         sensitivity="restricted", zone="security",
                         ts="2026-09-03T00:01:00+00:00")
    svc.publish_rule_set("industrial", 1, INDUSTRIAL_V1,
                         effective_ts="2026-09-01T00:00:00+00:00",
                         ts="2026-09-03T01:00:00+00:00")
    svc.publish_rule_set("security", 1, SECURITY_V1,
                         effective_ts="2026-09-01T00:00:00+00:00",
                         ts="2026-09-03T01:01:00+00:00")
    svc.publish_rule_set("public_service", 1, PUBLIC_V1,
                         effective_ts="2026-09-01T00:00:00+00:00",
                         ts="2026-09-03T01:02:00+00:00")
    svc.open_quota_pool("pool", 100, "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00",
                        ts="2026-09-03T02:00:00+00:00")
    return svc


class LedgerChainTest(unittest.TestCase):
    def test_r_log_append_only_and_hash_chain(self) -> None:
        svc = bootstrap()
        before = len(svc.ledger.events)
        svc.register_org("org-c", "丙队（合成）", "中小团队", ts="2026-09-04T00:00:00+00:00")
        svc.ledger.validate()
        self.assertEqual(len(svc.ledger.events), before + 1)

    def test_r_log_2_tampering_is_detected(self) -> None:
        svc = bootstrap()
        events = svc.ledger.events
        raw = [e.to_dict() for e in events]
        # 篡改中间事件的载荷：该事件哈希立即失效。
        raw[3]["payload"]["name"] = "被篡改名称"
        tampered_events = [type(events[0]).from_dict(item) for item in raw]
        with self.assertRaisesRegex(Exception, "哈希"):
            Ledger(tampered_events).validate()

    def test_r_log_3_seq_and_ts_monotonic(self) -> None:
        svc = bootstrap()
        with self.assertRaises(Exception):
            svc.ledger.append("x", {}, ts="2020-01-01T00:00:00+00:00")

    def test_r_log_4_state_reconstructs_from_events(self) -> None:
        svc = bootstrap()
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            10, "用途", "2026Q3", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        rebuilt = replay(svc.ledger.events)
        self.assertEqual(rebuilt.orgs["org-a"]["group"], "工业一线")
        self.assertEqual(len(rebuilt.applications), 1)


class RuleVersionTest(unittest.TestCase):
    def test_r_rule_2_versions_only_increase(self) -> None:
        svc = bootstrap()
        with self.assertRaises(ValueError):
            svc.publish_rule_set("industrial", 1, INDUSTRIAL_V1,
                                 effective_ts="2026-10-01T00:00:00+00:00",
                                 ts="2026-09-10T00:00:00+00:00")
        svc.publish_rule_set(
            "industrial", 2, {**INDUSTRIAL_V1, "max_units": 100},
            effective_ts="2026-09-20T00:00:00+00:00", ts="2026-09-10T00:01:00+00:00",
        )

    def test_r_rule_3_existing_grant_keeps_old_basis_after_rules_change(self) -> None:
        svc = bootstrap()
        key = svc.application_key("dev-a", "proj-a", ["model-x@v1"], "2026Q3")
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            50, "工业用途", "2026Q3", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        svc.decide(key, 1, "rev-1", approve=True, grant_id="g1",
                   ts="2026-09-05T01:00:00+00:00")
        # 规则升级：上限改为 100。
        svc.publish_rule_set(
            "industrial", 2, {**INDUSTRIAL_V1, "max_units": 100},
            effective_ts="2026-09-20T00:00:00+00:00", ts="2026-09-10T00:00:00+00:00",
        )
        # 已决定版本返回原结果，依据永远是 v1；额度仍在。
        again = svc.decide(key, 1, "rev-1", approve=True, ts="2026-09-21T00:00:00+00:00")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["decision"]["rules_version"], 1)
        self.assertEqual(svc.state.pools["pool"].grants["g1"].units, 50)

    def test_r_rule_4_rules_not_effective_cannot_be_used(self) -> None:
        svc = bootstrap()
        svc.publish_rule_set(
            "industrial", 2, {**INDUSTRIAL_V1, "max_units": 100},
            effective_ts="2026-09-20T00:00:00+00:00", ts="2026-09-10T00:00:00+00:00",
        )
        key = svc.application_key("dev-a", "proj-a", ["model-x@v1"], "2026Q3b")
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            90, "工业用途", "2026Q3b", "pool", ts="2026-09-15T00:00:00+00:00",
        )
        result = svc.decide(key, 1, "rev-1", approve=False, ts="2026-09-15T01:00:00+00:00")
        # 生效前按 v1（上限 60）评审：90 单位超出 v1 区间，C-UNITS 不通过 → 拒绝，
        # 决定记录的依据版本仍是 v1（而不是尚未生效的 v2）。
        self.assertFalse(result["decision"]["approve"])
        self.assertEqual(result["decision"]["rules_version"], 1)
        failed = {b["clause"]: b["passed"] for b in result["decision"]["basis"]}
        self.assertFalse(failed["C-UNITS"])


class ApplicationTest(unittest.TestCase):
    def test_r_app_2_duplicate_returns_original_without_event(self) -> None:
        svc = bootstrap()
        kwargs = dict(
            dev_id="dev-a", project_id="proj-a",
            resources=[{"resource_id": "model-x", "version": "v1"}],
            compute_units=10, purpose="用途", period="2026Q3", pool_id="pool",
        )
        first = svc.submit_application(ts="2026-09-05T00:00:00+00:00", **kwargs)
        events_after_first = len(svc.ledger.events)
        second = svc.submit_application(ts="2026-09-05T00:05:00+00:00", **kwargs)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(len(svc.ledger.events), events_after_first)

    def test_r_app_3_content_change_creates_new_revision(self) -> None:
        svc = bootstrap()
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            10, "用途一", "2026Q3", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        second = svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            20, "用途二", "2026Q3", "pool", ts="2026-09-06T00:00:00+00:00",
        )
        self.assertFalse(second["replayed"])
        self.assertEqual(second["revision"], 2)
        key = svc.application_key("dev-a", "proj-a", ["model-x@v1"], "2026Q3")
        app = svc.state.applications[key]
        self.assertEqual(app.revisions[1].content["compute_units"], 10)
        self.assertEqual(app.revisions[2].content["compute_units"], 20)


class ReviewSegregationTest(unittest.TestCase):
    def _app(self, svc: ResourceService) -> str:
        key = svc.application_key("dev-a", "proj-a", ["model-x@v1"], "2026Q3")
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            10, "用途", "2026Q3", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        return key

    def test_r_rev_1_applicant_cannot_review_own(self) -> None:
        svc = bootstrap()
        key = self._app(svc)
        with self.assertRaisesRegex(ServiceError, "申请人不能审批本人申请"):
            svc.decide(key, 1, "dev-a", approve=True, ts="2026-09-05T01:00:00+00:00")
        with self.assertRaisesRegex(ServiceError, "申请人不能审批本人申请"):
            svc.decide(key, 1, "dev-a", approve=True, exception=True,
                       second_reviewer_id="dev-b", ts="2026-09-05T01:00:00+00:00")

    def test_r_rev_2_packet_is_need_to_know(self) -> None:
        svc = bootstrap()
        key = self._app(svc)
        packet = svc.review_packet(key, 1, "industry_reviewer")
        # 只含本申请所需字段，不含池余量、开发者其他项目等内部信息。
        self.assertEqual(set(packet), {
            "app_key", "revision", "track", "project", "applicant",
            "purpose", "compute_units", "resources",
        })
        self.assertNotIn("pool", packet)

    def test_r_rev_3_decision_records_basis(self) -> None:
        svc = bootstrap()
        key = self._app(svc)
        result = svc.decide(key, 1, "rev-1", approve=True, grant_id="g",
                            ts="2026-09-05T01:00:00+00:00")
        clauses = {b["clause"] for b in result["decision"]["basis"]}
        self.assertEqual(clauses, {"C-PURPOSE", "C-UNITS", "C-RESOURCE", "C-SENSITIVITY", "C-CAPACITY"})

    def test_r_rev_4_exception_requires_independent_second_reviewer(self) -> None:
        svc = bootstrap()  # 工业上限 60
        key = svc.application_key("dev-a", "proj-a", ["model-x@v1"], "2026Q3x")
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            80, "超限但有正当理由", "2026Q3x", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        with self.assertRaisesRegex(ServiceError, "例外"):
            svc.decide(key, 1, "rev-1", approve=True, ts="2026-09-05T01:00:00+00:00")
        with self.assertRaisesRegex(ServiceError, "复核人"):
            svc.decide(key, 1, "rev-1", approve=True, exception=True,
                       second_reviewer_id="rev-1", ts="2026-09-05T01:00:00+00:00")
        with self.assertRaisesRegex(ServiceError, "复核人"):
            svc.decide(key, 1, "rev-1", approve=True, exception=True,
                       second_reviewer_id="dev-a", ts="2026-09-05T01:00:00+00:00")
        result = svc.decide(key, 1, "rev-1", approve=True, exception=True,
                            second_reviewer_id="rev-2", grant_id="g-exc",
                            ts="2026-09-05T01:00:00+00:00")
        self.assertTrue(result["decision"]["exception"])

    def test_capacity_clause_cannot_be_waived(self) -> None:
        svc = bootstrap()
        key = svc.application_key("dev-a", "proj-a", ["model-x@v1"], "2026Q3y")
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            150, "超出容量", "2026Q3y", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        with self.assertRaisesRegex(ServiceError, "不可放宽"):
            svc.decide(key, 1, "rev-1", approve=True, exception=True,
                       second_reviewer_id="rev-2", ts="2026-09-05T01:00:00+00:00")


class PrivacyTest(unittest.TestCase):
    def test_r_priv_1_existence_undiscoverable(self) -> None:
        svc = bootstrap()
        # 工业评审人：搜索、列表、直取都无法感知专区数据集。
        self.assertEqual(svc.search_resources("industry_reviewer", "industrial", "专区"), [])
        self.assertIsNone(svc.get_resource("industry_reviewer", "industrial", "ds-secret", "v1"))
        # 与一个真正不存在的资源返回不可区分的结果。
        self.assertIsNone(svc.get_resource("industry_reviewer", "industrial", "does-not-exist", "v9"))
        # 安全评审人在安全赛道可见。
        self.assertIsNotNone(svc.get_resource("security_reviewer", "security", "ds-secret", "v1"))
        # 安全评审人在非安全语境同样不可见。
        self.assertIsNone(svc.get_resource("security_reviewer", "industrial", "ds-secret", "v1"))

    def test_r_priv_3_access_expiry_restores_invisibility(self) -> None:
        svc = bootstrap()
        svc.grant_access("acc-1", "dev-a", "ds-secret", "2026-09-30T00:00:00+00:00",
                         ts="2026-09-04T00:00:00+00:00")
        viewer = "developer:dev-a"
        self.assertIsNotNone(svc.get_resource(viewer, "industrial", "ds-secret", "v1"))
        svc.resume("2026-10-03T00:00:00+00:00")  # 授权到期
        self.assertIsNone(svc.get_resource(viewer, "industrial", "ds-secret", "v1"))
        # 普通资源始终可见，证明返回 None 不是接口故障。
        self.assertIsNotNone(svc.get_resource(viewer, "industrial", "model-x", "v1"))


class QuotaConservationTest(unittest.TestCase):
    def _approved(self, svc: ResourceService, units: int, grant_id: str,
                  dev: str = "dev-b", proj: str = "proj-b") -> str:
        key = svc.application_key(dev, proj, ["model-x@v1"], f"P-{grant_id}")
        svc.submit_application(
            dev, proj, [{"resource_id": "model-x", "version": "v1"}],
            units, "安全用途", f"P-{grant_id}", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        svc.decide(key, 1, "rev-sec", approve=True, grant_id=grant_id,
                   ts="2026-09-05T01:00:00+00:00")
        return key

    def test_r_quota_1_over_capacity_rejected_and_rejection_costs_nothing(self) -> None:
        svc = bootstrap()
        self._approved(svc, 60, "g1", "dev-a", "proj-a")
        pool = svc.state.pools["pool"]
        self.assertEqual(pool.available, 40)
        key = svc.application_key("dev-b", "proj-b", ["model-x@v1"], "P-2")
        # 安全项目在安全规则下上限 120，但池容量只有 100：申请 50 超余量。
        svc.submit_application(
            "dev-b", "proj-b", [{"resource_id": "model-x", "version": "v1"}],
            50, "安全用途", "P-2", "pool", ts="2026-09-06T00:00:00+00:00",
        )
        # 容量为不可放宽条款：尝试批准会被直接拒绝执行；改为记录拒绝决定。
        with self.assertRaisesRegex(ServiceError, "不可放宽"):
            svc.decide(key, 1, "rev-sec", approve=True, exception=True,
                       second_reviewer_id="rev-2", ts="2026-09-06T01:00:00+00:00")
        result = svc.decide(key, 1, "rev-sec", approve=False, ts="2026-09-06T02:00:00+00:00")
        self.assertFalse(result["decision"]["approve"])
        self.assertEqual(svc.state.pools["pool"].occupied, 60)  # 拒绝不占配额

    def test_r_quota_2_claim_usage_release_extend_conserve(self) -> None:
        svc = bootstrap()
        self._approved(svc, 100, "g1")  # 安全赛道上限 120，可合法批 100
        self.assertEqual(svc.state.pools["pool"].occupied, 100)
        svc.claim("g1", 60, "k-claim", ts="2026-09-06T00:00:00+00:00")
        self.assertEqual(svc.state.pools["pool"].occupied, 100)  # 领取只在状态间迁移
        with self.assertRaises(ServiceError):
            svc.report_usage("g1", 61, "k-bad", ts="2026-09-07T00:00:00+00:00")
        svc.report_usage("g1", 40, "k-use", ts="2026-09-07T00:00:00+00:00")
        self.assertEqual(svc.state.pools["pool"].occupied, 100)  # 核对用量仍占用
        svc.release("g1", 30, "k-rel", ts="2026-09-08T00:00:00+00:00")
        pool = svc.state.pools["pool"]
        grant = pool.grants["g1"]
        self.assertEqual(pool.occupied, 70)  # 释放回池
        self.assertEqual(pool.available, 30)
        before = (grant.reserved, grant.claimed, grant.used)
        svc.extend("g1", "2026-12-01T00:00:00+00:00", "k-ext", ts="2026-09-09T00:00:00+00:00")
        grant = svc.state.pools["pool"].grants["g1"]
        self.assertEqual((grant.reserved, grant.claimed, grant.used), before)  # 延期零增减
        self.assertEqual(grant.expires_at, "2026-12-01T00:00:00+00:00")
        # 单位守恒：四个状态之和恒等于批准总量。
        self.assertEqual(grant.reserved + grant.claimed + grant.used + grant.released, 100)

    def test_r_quota_4_idempotent_command_keys_dedup(self) -> None:
        svc = bootstrap()
        self._approved(svc, 50, "g1", "dev-a", "proj-a")
        svc.claim("g1", 20, "same-key", ts="2026-09-06T00:00:00+00:00")
        duplicate = svc.claim("g1", 20, "same-key", ts="2026-09-06T00:05:00+00:00")
        self.assertTrue(duplicate["replayed"])
        grant = svc.state.pools["pool"].grants["g1"]
        self.assertEqual(grant.claimed, 20)  # 未重复扣减
        self.assertEqual(grant.reserved, 30)


class ResumeTest(unittest.TestCase):
    def test_r_resume_reconstructs_pending_and_due_after_restart(self) -> None:
        svc = bootstrap()
        # 批准一个 09-25 到期、预留 40 的额度；另有一份提交未评审的申请。
        key = svc.application_key("dev-a", "proj-a", ["model-x@v1"], "P-1")
        svc.submit_application(
            "dev-a", "proj-a", [{"resource_id": "model-x", "version": "v1"}],
            40, "用途", "P-1", "pool", ts="2026-09-05T00:00:00+00:00",
        )
        svc.decide(key, 1, "rev-1", approve=True, grant_id="g1",
                   expires_at="2026-09-25T00:00:00+00:00", ts="2026-09-05T01:00:00+00:00")
        svc.claim("g1", 40, "k", ts="2026-09-06T00:00:00+00:00")
        svc.report_usage("g1", 10, "u", ts="2026-09-07T00:00:00+00:00")
        pending_key = svc.application_key("dev-b", "proj-b", ["model-x@v1"], "P-2")
        svc.submit_application(
            "dev-b", "proj-b", [{"resource_id": "model-x", "version": "v1"}],
            10, "安全用途", "P-2", "pool", ts="2026-09-28T00:00:00+00:00",
        )

        # 模拟停机：只保留台账文件，重新加载后接续。
        path = Path("fixtures/.tmp-resume.json")
        svc.ledger.save(path)
        try:
            restarted = ResourceService(Ledger.load(path))
            due = restarted.due_expirations("2026-10-03T00:00:00+00:00")
            self.assertEqual(due["grants"], [("pool", "g1")])
            result = restarted.resume("2026-10-03T00:00:00+00:00")
            self.assertIn("g1", result["expired_grants"])
            self.assertIn((pending_key, 1), result["pending_reviews"])
            # 到期只回收未用的 30，已核对 10 永久占用。
            pool = restarted.state.pools["pool"]
            self.assertEqual(pool.occupied, 10)
            self.assertEqual(pool.available, 90)
            # 再次接续是幂等的：不会重复到期、重复入队。
            again = restarted.resume("2026-10-03T00:00:00+00:00")
            self.assertEqual(again["expired_grants"], [])
        finally:
            path.unlink(missing_ok=True)


class ExplainAndFairnessTest(unittest.TestCase):
    def test_demo_fixture_is_consistent(self) -> None:
        ledger = Ledger.load(FIXTURE_LEDGER)
        ledger.validate()
        state = replay(ledger.events)
        for pool in state.pools.values():
            self.assertLessEqual(pool.occupied, pool.capacity)
            for grant in pool.grants.values():
                self.assertEqual(
                    grant.reserved + grant.claimed + grant.used + grant.released,
                    grant.units,
                )

    def test_r_explain_approved_project(self) -> None:
        ledger = Ledger.load(FIXTURE_LEDGER)
        key = ResourceService.application_key(
            "dev-fang", "proj-weld", ["model-vision@v1.2", "ds-weld@v1"], "2026Q3"
        )
        explanation = readmodels.explain_decision(ledger, key, 1)
        self.assertEqual(explanation["outcome"], "approved")
        self.assertEqual(explanation["rules"]["version"], 1)
        # R-EXPLAIN-2：资源版本与许可范围。
        self.assertEqual({r["version"] for r in explanation["resources"]}, {"v1.2", "v1"})
        # R-EXPLAIN-3：反事实挤占成立（若无该 50 单位，55 单位申请即可容纳）。
        self.assertTrue(explanation["contention"]["displaced_others"])
        victim = explanation["contention"]["capacity_rejected"][0]
        self.assertTrue(victim["counterfactual_fit_without_this_grant"])
        self.assertEqual(victim["compute_units"], 55)

    def test_r_explain_rejected_project_points_to_clause_and_resource_version(self) -> None:
        ledger = Ledger.load(FIXTURE_LEDGER)
        key = ResourceService.application_key("dev-min", "proj-grid", ["model-vision@v1.3"], "2026Q3")
        explanation = readmodels.explain_decision(ledger, key, 1)
        self.assertEqual(explanation["outcome"], "rejected")
        failed = [b["clause"] for b in explanation["basis"] if not b["passed"]]
        self.assertEqual(failed, ["C-CAPACITY"])
        self.assertEqual(explanation["resources"][0]["version"], "v1.3")

    def test_r_fair_segmented_by_group_and_rules_version(self) -> None:
        ledger = Ledger.load(FIXTURE_LEDGER)
        report = readmodels.fairness_report(ledger)
        indexed = {(g["group"], g["rules_version"]): g for g in report["groups"]}
        # 工业一线在 v1 下有 3 个决定样本：1 批 2 拒。
        industrial_v1 = indexed[("工业一线", 1)]
        self.assertFalse(industrial_v1["suppressed"])
        self.assertEqual(industrial_v1["approval_rate"], round(1 / 3, 4))
        # 规则 v2 分段单独统计，不与 v1 混并（R-FAIR-2）。
        self.assertIn(("工业一线", 2), indexed)
        # 小样本群体被抑制，不暴露比率（R-FAIR-3）。
        for group in report["groups"]:
            if group["applications"] < 3:
                self.assertTrue(group["suppressed"])
                self.assertNotIn("approval_rate", group)
        # R-FAIR-4：运营角色可下钻到申请版本集合。
        self.assertIn("applications_ref", industrial_v1)


if __name__ == "__main__":
    unittest.main()
