"""校验央企人工智能资源普惠开放治理的领域资料。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.ai_resource_context import (
    CONSTRAINT_TAGS,
    CONTINUITY_OBJECTS,
    DOMAIN,
    RULE_TRACK_NAMES,
    context_fingerprint,
    load_context,
)

FIXTURE = Path("fixtures/context.json")


class ContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.value = load_context(FIXTURE)

    def test_example_matches_domain_and_is_v2(self) -> None:
        self.assertEqual(self.value["domain"], DOMAIN)
        self.assertGreaterEqual(self.value["version"], 2)

    def test_fingerprint_is_stable_and_order_independent(self) -> None:
        self.assertEqual(len(context_fingerprint(self.value)), 64)
        reordered = dict(self.value)
        reordered["actors"] = list(reversed(self.value["actors"]))
        self.assertNotEqual(context_fingerprint(reordered), context_fingerprint(self.value))

    def test_all_twelve_constraint_tags_present(self) -> None:
        for tag in CONSTRAINT_TAGS:
            self.assertTrue(
                any(f"【{tag}】" in entry for entry in self.value["constraints"]),
                f"缺少约束标签：{tag}",
            )
        self.assertGreaterEqual(len(self.value["constraints"]), 12)

    def test_record_chain_has_eight_continuous_links(self) -> None:
        chain = self.value["record_chain"]
        self.assertGreaterEqual(len(chain), 8)
        self.assertEqual([node["order"] for node in chain], list(range(1, len(chain) + 1)))
        links = [node["link"] for node in chain]
        for required in (
            "机构",
            "开发者",
            "项目目标",
            "模型与数据集版本",
            "许可范围",
            "算力需求",
            "评审依据",
            "实际用量",
        ):
            self.assertIn(required, links)
        for node in chain:
            self.assertTrue(all(record.strip() for record in node["records"]))

    def test_three_rule_tracks_present(self) -> None:
        names = [track["name"] for track in self.value["rule_tracks"]]
        for track_name in RULE_TRACK_NAMES:
            self.assertIn(track_name, names)

    def test_continuity_covers_all_resume_objects(self) -> None:
        objects = [item["object"] for item in self.value["continuity"]]
        for required in CONTINUITY_OBJECTS:
            self.assertIn(required, objects)
        for item in self.value["continuity"]:
            self.assertTrue(item["resume_action"].strip())
            self.assertTrue(item["must_not_lose"])


class RejectionTest(unittest.TestCase):
    """对资料做局部破坏后，校验器必须明确拒绝。"""

    def setUp(self) -> None:
        self.valid = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.tmp = Path("fixtures/.invalid-context.json")

    def tearDown(self) -> None:
        self.tmp.unlink(missing_ok=True)

    def _expect_error(self, mutated: dict, pattern: str) -> None:
        self.tmp.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, pattern):
            load_context(self.tmp)

    def test_wrong_domain_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        mutated["domain"] = "other-domain"
        self._expect_error(mutated, "领域标识不一致")

    def test_old_version_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        mutated["version"] = 1
        self._expect_error(mutated, "资料版本无效")

    def test_missing_constraint_tag_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        # 保留 12 条的数量，仅抹掉标签，确保触发的是标签覆盖校验而非数量校验
        mutated["constraints"] = [
            entry.replace("【配额守恒】", "【其他事项】") if "【配额守恒】" in entry else entry
            for entry in mutated["constraints"]
        ]
        self._expect_error(mutated, "约束缺少业务标签")

    def test_broken_chain_numbering_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        mutated["record_chain"][-1]["order"] = 99
        self._expect_error(mutated, "记录链路")

    def test_missing_track_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        mutated["rule_tracks"] = [
            track for track in mutated["rule_tracks"] if track["name"] != "安全研究"
        ]
        self._expect_error(mutated, "规则轨道")

    def test_missing_continuity_object_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        mutated["continuity"] = [
            item for item in mutated["continuity"] if item["object"] != "用量核对"
        ]
        self._expect_error(mutated, "停机接续对象")

    def test_extra_field_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        mutated["unknown"] = "不应出现"
        self._expect_error(mutated, "字段不完整")

    def test_real_identity_marker_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.valid))
        mutated["actors"].append("张三 zhangsan@example.test")
        self._expect_error(mutated, "真实身份信息")


if __name__ == "__main__":
    unittest.main()
