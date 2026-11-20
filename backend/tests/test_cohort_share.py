"""共享分群交付测试：同批结果复用时的归属校验、授权交付与限域报表。

场景主线：同一批分群名单共享给华东、华南两个负责团队；华东团队
在报表取数前撤回一段地区权限（迟到变更），检查同一提取结果在两处
形成的名单与报表汇总。
"""
from __future__ import annotations

from app.services.cohort_share import CohortShareService

EAST = "TEAM-EAST"   # 华东负责团队：授权地区 [华东, 华北]
SOUTH = "TEAM-SOUTH"  # 华南负责团队：授权地区 [华南, 西南]

# fixtures COHORT_USERS 的已知口径：
# 华东 4 人 LTV 5060；华北 3 人 LTV 3880；华南 2 人 LTV 800；西南 1 人 LTV 1500
EAST_REGIONS = {"华东", "华北"}
SOUTH_REGIONS = {"华南", "西南"}


def _service() -> CohortShareService:
    return CohortShareService()


def _extract(svc: CohortShareService, **kwargs) -> dict:
    return svc.run_extraction(**kwargs)


class TestSharedExtraction:
    """共享提取批次：登记来源，一次提取供多团队复用。"""

    def test_extraction_registers_source(self):
        svc = _service()
        batch = _extract(svc)
        assert batch["batch_id"] == "EXB-0001"
        assert batch["任务编号"] == "DATA-SHARE-COHORT"
        assert batch["来源数据集"] == "cohort_users"
        assert batch["批次总人数"] == 10
        assert len(batch["members"]) == 10

    def test_extraction_with_rule(self):
        svc = _service()
        batch = _extract(svc, rule={"min_ltv": 1000.0})
        assert batch["批次总人数"] == 4
        for m in batch["members"]:
            assert m["ltv"] >= 1000.0

    def test_batches_listed(self):
        svc = _service()
        _extract(svc)
        _extract(svc, rule={"tag": "高价值"})
        batches = svc.list_batches()
        assert len(batches) == 2
        assert batches[1]["batch_id"] == "EXB-0002"


class TestAuthorizedDelivery:
    """授权提取数据包：复用同批结果时按团队业务域校验数据归属。"""

    def test_same_batch_delivers_different_lists_per_team(self):
        svc = _service()
        batch = _extract(svc)
        east_pkg, _ = svc.deliver(batch["batch_id"], EAST)
        south_pkg, _ = svc.deliver(batch["batch_id"], SOUTH)
        east_ids = {m["user_id"] for m in east_pkg["members"]}
        south_ids = {m["user_id"] for m in south_pkg["members"]}
        # 华东团队 7 人（华东4+华北3），华南团队 3 人（华南2+西南1）
        assert east_pkg["交付人数"] == 7
        assert south_pkg["交付人数"] == 3
        assert east_ids.isdisjoint(south_ids)

    def test_no_cross_team_mixing(self):
        svc = _service()
        batch = _extract(svc)
        east_pkg, _ = svc.deliver(batch["batch_id"], EAST)
        south_pkg, _ = svc.deliver(batch["batch_id"], SOUTH)
        # 每个团队只能拿到自己业务域内的成员，互不混用
        assert {m["region"] for m in east_pkg["members"]} <= EAST_REGIONS
        assert {m["region"] for m in south_pkg["members"]} <= SOUTH_REGIONS
        assert all(m["region"] not in SOUTH_REGIONS for m in east_pkg["members"])
        assert all(m["region"] not in EAST_REGIONS for m in south_pkg["members"])

    def test_package_records_scope_and_coverage(self):
        svc = _service()
        batch = _extract(svc)
        pkg, _ = svc.deliver(batch["batch_id"], EAST)
        assert pkg["权限范围"] == ["华东", "华北"]
        assert pkg["实际覆盖"] == {"批次总人数": 10, "覆盖人数": 7, "覆盖率": 0.7}
        # 排除统计只给计数不给明细：3 人都是另一团队业务域的域外成员
        assert pkg["排除统计"]["域外成员"] == 3
        assert pkg["排除统计"]["已撤回成员"] == 0

    def test_deliver_rejects_unknown_team_or_batch(self):
        svc = _service()
        batch = _extract(svc)
        pkg, msg = svc.deliver(batch["batch_id"], "TEAM-GHOST")
        assert pkg is None and "不存在" in msg
        pkg, msg = svc.deliver("EXB-9999", EAST)
        assert pkg is None and "不存在" in msg


class TestLateRevocation:
    """迟到的归属变更：报表取数前撤回一段地区权限。"""

    def test_revoke_region_updates_scope(self):
        svc = _service()
        team, msg = svc.revoke_region(EAST, "华北", "归属变更迟到，取数前撤回")
        assert team is not None
        assert team["授权地区"] == ["华东"]
        assert team["权限版本"] == 2
        assert team["撤回记录"][0]["地区"] == "华北"

    def test_revoke_rejects_region_out_of_scope(self):
        svc = _service()
        team, msg = svc.revoke_region(EAST, "华南")
        assert team is None and "不在" in msg
        # 华南是另一团队业务域，华东团队无权撤回，也不受影响
        team, msg = svc.revoke_region(SOUTH, "华南")
        assert team is not None

    def test_current_package_drops_revoked_members(self):
        svc = _service()
        batch = _extract(svc)
        svc.deliver(batch["batch_id"], EAST)  # 撤回前已交付过一次
        svc.revoke_region(EAST, "华北")
        pkg, _ = svc.current_package(batch["batch_id"], EAST)
        # 当前交付不再提供已撤回明细，归属未变的成员仍可访问
        assert {m["region"] for m in pkg["members"]} == {"华东"}
        assert pkg["交付人数"] == 4
        revoked_ids = {"U10002", "U10006", "U10010"}
        assert revoked_ids.isdisjoint({m["user_id"] for m in pkg["members"]})
        assert pkg["排除统计"]["已撤回成员"] == 3

    def test_revocation_does_not_touch_other_team(self):
        svc = _service()
        batch = _extract(svc)
        svc.revoke_region(EAST, "华北")
        pkg, _ = svc.current_package(batch["batch_id"], SOUTH)
        assert pkg["交付人数"] == 3
        assert {m["region"] for m in pkg["members"]} <= SOUTH_REGIONS


class TestScopeLimitedReports:
    """分群报表：按仍有效的授权范围统计，记录运行批次与实际覆盖。"""

    def test_report_computed_within_scope(self):
        svc = _service()
        batch = _extract(svc)
        report, _ = svc.generate_report(batch["batch_id"], EAST)
        assert report["运行批次"] == batch["batch_id"]
        assert report["权限范围"] == ["华东", "华北"]
        summary = report["汇总"]
        assert summary["总人数"] == 7
        assert summary["LTV合计"] == 8940.0
        assert summary["平均LTV"] == round(8940.0 / 7, 2)
        assert summary["地区分布"] == {"华东": 4, "华北": 3}
        assert report["实际覆盖"]["覆盖人数"] == 7

    def test_report_after_revoke_excludes_revoked_members(self):
        svc = _service()
        batch = _extract(svc)
        svc.revoke_region(EAST, "华北")  # 报表取数前才撤回
        report, _ = svc.generate_report(batch["batch_id"], EAST)
        summary = report["汇总"]
        # 已撤回成员不计入总人数、LTV 与地区分布
        assert summary["总人数"] == 4
        assert summary["LTV合计"] == 5060.0
        assert summary["地区分布"] == {"华东": 4}
        assert "华北" not in summary["地区分布"]
        assert report["权限范围"] == ["华东"]
        assert report["实际覆盖"] == {"批次总人数": 10, "覆盖人数": 4, "覆盖率": 0.4}
        assert "缩小" in report["覆盖说明"]

    def test_old_report_keeps_batch_and_scope_snapshot(self):
        svc = _service()
        batch = _extract(svc)
        old_report, _ = svc.generate_report(batch["batch_id"], EAST)
        svc.revoke_region(EAST, "华北")
        new_report, _ = svc.generate_report(batch["batch_id"], EAST)
        reports = svc.list_reports(team_id=EAST)
        assert len(reports) == 2
        # 旧报表保留运行批次与当时的权限范围用于追溯
        assert old_report["运行批次"] == batch["batch_id"]
        assert old_report["权限范围"] == ["华东", "华北"]
        assert old_report["汇总"]["总人数"] == 7
        # 新报表按仍有效范围计算，两期覆盖差异可追溯
        assert new_report["权限范围"] == ["华东"]
        assert new_report["汇总"]["总人数"] == 4

    def test_same_batch_reports_differ_between_teams(self):
        svc = _service()
        batch = _extract(svc)
        svc.revoke_region(EAST, "华北")
        east_report, _ = svc.generate_report(batch["batch_id"], EAST)
        south_report, _ = svc.generate_report(batch["batch_id"], SOUTH)
        # 同一提取结果在两处形成不同的报表汇总
        assert east_report["汇总"]["总人数"] == 4
        assert south_report["汇总"]["总人数"] == 3
        assert south_report["汇总"]["LTV合计"] == 2300.0
        assert south_report["汇总"]["地区分布"] == {"华南": 2, "西南": 1}
        # 两团队的报表成员互不混用：地区分布没有交集
        east_regions = set(east_report["汇总"]["地区分布"])
        south_regions = set(south_report["汇总"]["地区分布"])
        assert east_regions.isdisjoint(south_regions)

    def test_report_rejects_unknown_team_or_batch(self):
        svc = _service()
        batch = _extract(svc)
        report, msg = svc.generate_report(batch["batch_id"], "TEAM-GHOST")
        assert report is None and "不存在" in msg
        report, msg = svc.generate_report("EXB-9999", EAST)
        assert report is None and "不存在" in msg
