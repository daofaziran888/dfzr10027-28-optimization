"""共享分群提取交付：同批结果复用时的数据归属校验与授权交付。

背景：分群提取（cohort_rule -> data_extract -> report_gen）把匹配成员交给报表汇总，
提取任务能登记来源，但一份结果共享给其他团队时，覆盖范围仍同原分群。

本模块把共享交付改成「授权提取数据包」模式：

- 提取只跑一次，产出共享批次（登记来源数据集、任务编号与筛选条件）；
- 批次复用给各负责团队时，按团队仍有效的地区权限校验数据归属，
  只把明确属于本次业务域的成员交付出去，两个团队的成员互不混用；
- 地区权限可被撤回（包括报表取数前才到达的迟到变更），交付与报表读取
  都以仍有效的权限范围为限，已撤回成员不计入总人数、LTV 与地区分布；
- 分群报表按授权范围内的统计数据计算本次汇总，记录运行批次、当时权限
  范围与实际覆盖；旧报表保留快照用于追溯，当前交付不再提供已撤回明细。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.fixtures import COHORT_USERS, SHARE_TEAMS
from app import store as store_module

# store 命名空间（下划线前缀，与 _pipeline_results 一致，不进业务模块看板）
NS_TEAMS = "_share_teams"
NS_BATCHES = "_share_batches"
NS_DELIVERIES = "_share_deliveries"
NS_REPORTS = "_share_reports"

DEFAULT_TASK_CODE = "DATA-SHARE-COHORT"
DEFAULT_SOURCE = "cohort_users"
DEFAULT_FILTER = "全量分群成员"


def _db():
    """运行时解析 store 单例：本模块状态可流转（权限撤回），需要让
    reset_store() 的测试隔离真正生效，而不是绑死导入时的旧实例。"""
    return store_module.store


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 内部状态：团队权限档案（从 fixtures 惰性初始化，撤回会改变它）
# ---------------------------------------------------------------------------

def _teams() -> list[dict[str, Any]]:
    """团队权限档案：首次访问时从 fixtures 克隆，之后撤回记录都写在这里。"""
    rows = _db().rows(NS_TEAMS)
    if not rows:
        for team in SHARE_TEAMS:
            rows.append({
                "team_id": team["team_id"],
                "团队名称": team["团队名称"],
                "业务域": team["业务域"],
                "授权地区": list(team["授权地区"]),
                "撤回记录": [],
                "权限版本": 1,
            })
    return rows


def _find_team(team_id: str) -> dict[str, Any] | None:
    for team in _teams():
        if team["team_id"] == team_id:
            return team
    return None


def _find_batch(batch_id: str) -> dict[str, Any] | None:
    for batch in _db().rows(NS_BATCHES):
        if batch["batch_id"] == batch_id:
            return batch
    return None


def _next_id(rows: list[dict[str, Any]], key: str, prefix: str) -> str:
    return f"{prefix}-{len(rows) + 1:04d}"


# ---------------------------------------------------------------------------
# 归属校验与统计口径
# ---------------------------------------------------------------------------

def _match_rule(user: dict[str, Any], rule: dict[str, Any]) -> bool:
    """与分群提取管道一致的筛选口径：region / channel / tag / min_ltv。"""
    if "region" in rule and user.get("region") != rule["region"]:
        return False
    if "channel" in rule and user.get("channel") != rule["channel"]:
        return False
    if "tag" in rule and user.get("tag") != rule["tag"]:
        return False
    if "min_ltv" in rule and _to_float(user.get("ltv")) < _to_float(rule["min_ltv"]):
        return False
    return True


def _authorize(team: dict[str, Any], members: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """数据归属校验：只放行归属地区仍在团队有效权限范围内的成员。

    返回 (授权成员, 排除成员)。排除含两种情况——归属不在本团队业务域
    （如另一团队的地区，绝不混用）与归属地区权限已被撤回。
    """
    valid = set(team["授权地区"])
    granted = [m for m in members if m.get("region") in valid]
    excluded = [m for m in members if m.get("region") not in valid]
    return granted, excluded


def _revoked_regions(team: dict[str, Any]) -> set[str]:
    return {record["地区"] for record in team["撤回记录"]}


def _coverage(granted_count: int, batch_total: int) -> dict[str, Any]:
    rate = round(granted_count / batch_total, 4) if batch_total else 0.0
    return {"批次总人数": batch_total, "覆盖人数": granted_count, "覆盖率": rate}


def _summarize(members: list[dict[str, Any]]) -> dict[str, Any]:
    """分群报表汇总口径：总人数、LTV 合计/均值、地区分布，只统计授权成员。"""
    total_ltv = round(sum(_to_float(m.get("ltv")) for m in members), 2)
    region_dist: dict[str, int] = {}
    for m in members:
        region = str(m.get("region", ""))
        region_dist[region] = region_dist.get(region, 0) + 1
    return {
        "总人数": len(members),
        "LTV合计": total_ltv,
        "平均LTV": round(total_ltv / len(members), 2) if members else 0.0,
        "地区分布": region_dist,
    }


def _excluded_stat(team: dict[str, Any], excluded: list[dict[str, Any]]) -> dict[str, Any]:
    """排除成员的聚合说明（只给计数，不给明细），区分归属域外与已撤回。"""
    revoked = _revoked_regions(team)
    out_of_domain = 0
    revoked_count = 0
    by_region: dict[str, int] = {}
    for m in excluded:
        region = str(m.get("region", ""))
        by_region[region] = by_region.get(region, 0) + 1
        if region in revoked:
            revoked_count += 1
        else:
            out_of_domain += 1
    return {"域外成员": out_of_domain, "已撤回成员": revoked_count, "按地区计数": by_region}


def _package_view(team: dict[str, Any], batch: dict[str, Any], package_id: str, delivered_at: str) -> dict[str, Any]:
    """授权提取数据包：交付时实时校验归属，只装仍有效权限范围内的成员。"""
    granted, excluded = _authorize(team, batch["members"])
    return {
        "package_id": package_id,
        "batch_id": batch["batch_id"],
        "运行批次": batch["batch_id"],
        "team_id": team["team_id"],
        "团队名称": team["团队名称"],
        "业务域": team["业务域"],
        "权限范围": list(team["授权地区"]),
        "权限版本": team["权限版本"],
        "members": [dict(m) for m in granted],
        "交付人数": len(granted),
        "实际覆盖": _coverage(len(granted), batch["批次总人数"]),
        "排除统计": _excluded_stat(team, excluded),
        "delivered_at": delivered_at,
    }


# ---------------------------------------------------------------------------
# 业务规则
# ---------------------------------------------------------------------------

class CohortShareService:
    """共享分群交付的状态流转与校验口径，路由层不做业务判断。"""

    # -- 团队权限 ----------------------------------------------------------

    def list_teams(self) -> list[dict[str, Any]]:
        return [dict(team) for team in _teams()]

    def revoke_region(self, team_id: str, region: str, reason: str = "") -> tuple[dict[str, Any] | None, str]:
        """撤回团队一段地区权限；撤回即刻生效，后续交付与报表都按新范围执行。"""
        team = _find_team(team_id)
        if team is None:
            return None, f"团队 {team_id} 不存在"
        region = region.strip()
        if not region:
            return None, "缺少必填字段：地区"
        if region not in team["授权地区"]:
            return None, f"地区「{region}」不在团队 {team['团队名称']} 的有效权限范围内，或已撤回"
        team["授权地区"].remove(region)
        team["撤回记录"].append({"地区": region, "撤回时间": _now(), "原因": reason or "数据归属变更"})
        team["权限版本"] += 1
        return dict(team), f"团队{team['团队名称']}已撤回地区「{region}」权限，后续交付与报表以仍有效范围为准"

    # -- 共享提取批次 ------------------------------------------------------

    def run_extraction(
        self,
        *,
        rule: dict[str, Any] | None = None,
        task_code: str | None = None,
        source: str | None = None,
        filter_desc: str | None = None,
    ) -> dict[str, Any]:
        """跑一次共享提取：登记来源，产出供多团队复用的批次结果。"""
        rule = rule or {}
        members = [dict(u) for u in COHORT_USERS if _match_rule(u, rule)]
        batches = _db().rows(NS_BATCHES)
        batch = {
            "batch_id": _next_id(batches, "batch_id", "EXB"),
            "任务编号": (task_code or DEFAULT_TASK_CODE).strip() or DEFAULT_TASK_CODE,
            "来源数据集": (source or DEFAULT_SOURCE).strip() or DEFAULT_SOURCE,
            "筛选条件": (filter_desc or DEFAULT_FILTER).strip() or DEFAULT_FILTER,
            "rule": dict(rule),
            "members": members,
            "批次总人数": len(members),
            "created_at": _now(),
        }
        batches.append(batch)
        return dict(batch)

    def list_batches(self) -> list[dict[str, Any]]:
        return [dict(batch) for batch in _db().rows(NS_BATCHES)]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        batch = _find_batch(batch_id)
        return dict(batch) if batch else None

    # -- 授权交付 ----------------------------------------------------------

    def deliver(self, batch_id: str, team_id: str) -> tuple[dict[str, Any] | None, str]:
        """把共享批次交付给指定团队：校验数据归属，产出授权提取数据包。"""
        batch = _find_batch(batch_id)
        if batch is None:
            return None, f"共享提取批次 {batch_id} 不存在"
        team = _find_team(team_id)
        if team is None:
            return None, f"团队 {team_id} 不存在"
        deliveries = _db().rows(NS_DELIVERIES)
        package = _package_view(team, batch, _next_id(deliveries, "package_id", "PKG"), _now())
        # 交付台账只留批次、范围与计数，不留成员明细；明细以当前交付视图为准
        deliveries.append({
            "package_id": package["package_id"],
            "batch_id": batch_id,
            "team_id": team_id,
            "权限范围": package["权限范围"],
            "权限版本": package["权限版本"],
            "交付人数": package["交付人数"],
            "delivered_at": package["delivered_at"],
        })
        return package, f"批次 {batch_id} 已按{team['团队名称']}当前权限范围交付 {package['交付人数']} 人"

    def current_package(self, batch_id: str, team_id: str) -> tuple[dict[str, Any] | None, str]:
        """当前交付视图：每次读取都重新校验归属，已撤回明细不会再次出现。"""
        batch = _find_batch(batch_id)
        if batch is None:
            return None, f"共享提取批次 {batch_id} 不存在"
        team = _find_team(team_id)
        if team is None:
            return None, f"团队 {team_id} 不存在"
        return _package_view(team, batch, "CURRENT", _now()), "当前交付视图已按仍有效权限范围校验"

    # -- 分群报表 ----------------------------------------------------------

    def generate_report(self, batch_id: str, team_id: str) -> tuple[dict[str, Any] | None, str]:
        """生成分群报表：按仍有效的授权范围统计本次汇总，记录运行批次与实际覆盖。"""
        batch = _find_batch(batch_id)
        if batch is None:
            return None, f"共享提取批次 {batch_id} 不存在"
        team = _find_team(team_id)
        if team is None:
            return None, f"团队 {team_id} 不存在"
        granted, excluded = _authorize(team, batch["members"])
        reports = _db().rows(NS_REPORTS)
        revoked_hit = _revoked_regions(team) & {str(m.get("region", "")) for m in excluded}
        report = {
            "report_id": _next_id(reports, "report_id", "REPT"),
            "运行批次": batch["batch_id"],
            "team_id": team["team_id"],
            "团队名称": team["团队名称"],
            "权限范围": list(team["授权地区"]),
            "权限版本": team["权限版本"],
            "汇总": _summarize(granted),
            "实际覆盖": _coverage(len(granted), batch["批次总人数"]),
            "覆盖说明": (
                f"地区权限撤回涉及 {len(revoked_hit)} 个地区，本期覆盖已缩小，待重新核对"
                if revoked_hit else "本期覆盖与权限范围一致"
            ),
            "generated_at": _now(),
        }
        reports.append(report)
        return dict(report), f"分群报表已生成：运行批次 {batch_id}，覆盖 {len(granted)} 人"

    def list_reports(self, team_id: str | None = None) -> list[dict[str, Any]]:
        """报表历史：旧报表保留运行批次与当时权限范围，用于追溯。"""
        reports = _db().rows(NS_REPORTS)
        if team_id:
            reports = [r for r in reports if r.get("team_id") == team_id]
        return [dict(r) for r in reports]
