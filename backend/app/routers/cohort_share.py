"""共享分群交付接口：共享提取批次、授权提取数据包、地区权限撤回与分群报表。

同一批分群名单复用给多个负责团队时，交付与报表读取都以仍有效的
权限范围为限；已撤回成员不计入汇总，也不在当前交付中再次出现。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.cohort_share import CohortShareService

router = APIRouter(prefix="/api/cohort-share", tags=["共享分群交付"])

service = CohortShareService()


class ExtractionPayload(BaseModel):
    """登记一次共享提取：来源信息照 data_extract 的口径登记。"""

    任务编号: str | None = None
    来源数据集: str | None = None
    筛选条件: str | None = None
    rule: dict[str, Any] = Field(default_factory=dict)


class TeamRefPayload(BaseModel):
    team_id: str


class RevokePayload(BaseModel):
    地区: str
    原因: str | None = None


@router.get("/teams")
def list_teams() -> dict[str, object]:
    """查看各负责团队的业务域、仍有效的地区权限与撤回记录。"""
    return {"items": service.list_teams()}


@router.post("/teams/{team_id}/revocations", response_model=ActionResult)
def revoke_region(team_id: str, payload: RevokePayload) -> ActionResult:
    """撤回团队一段地区权限；撤回即刻生效，后续交付与报表按新范围执行。"""
    team, message = service.revoke_region(team_id, payload.地区, payload.原因 or "")
    if team is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=team)


@router.post("/extractions", response_model=ActionResult)
def run_extraction(payload: ExtractionPayload) -> ActionResult:
    """跑一次共享提取：登记来源，产出供多团队复用的批次结果。"""
    batch = service.run_extraction(
        rule=payload.rule,
        task_code=payload.任务编号,
        source=payload.来源数据集,
        filter_desc=payload.筛选条件,
    )
    return ActionResult(
        ok=True,
        message=f"共享提取批次 {batch['batch_id']} 已生成，登记来源 {batch['来源数据集']}，共 {batch['批次总人数']} 人",
        entry=batch,
    )


@router.get("/extractions")
def list_extractions() -> dict[str, object]:
    """共享提取批次列表（含登记来源与批次总人数）。"""
    items = service.list_batches()
    return {"items": items, "total": len(items)}


@router.get("/extractions/{batch_id}")
def get_extraction(batch_id: str) -> dict:
    """查看单个共享批次的登记信息与原始名单。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"共享提取批次 {batch_id} 不存在")
    return batch


@router.post("/extractions/{batch_id}/deliveries", response_model=ActionResult)
def deliver(batch_id: str, payload: TeamRefPayload) -> ActionResult:
    """把共享批次交付给指定团队：校验数据归属，产出授权提取数据包。"""
    package, message = service.deliver(batch_id, payload.team_id)
    if package is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=package)


@router.get("/extractions/{batch_id}/packages/{team_id}")
def current_package(batch_id: str, team_id: str) -> dict:
    """当前交付视图：每次读取都重新校验归属，已撤回明细不会再次出现。"""
    package, message = service.current_package(batch_id, team_id)
    if package is None:
        raise HTTPException(status_code=404, detail=message)
    return package


@router.post("/extractions/{batch_id}/reports", response_model=ActionResult)
def generate_report(batch_id: str, payload: TeamRefPayload) -> ActionResult:
    """生成分群报表：按仍有效的授权范围统计本次汇总，记录运行批次与实际覆盖。"""
    report, message = service.generate_report(batch_id, payload.team_id)
    if report is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=report)


@router.get("/reports")
def list_reports(
    team_id: str | None = Query(default=None, description="按团队过滤报表历史"),
) -> dict[str, object]:
    """报表历史：旧报表保留运行批次与当时权限范围，用于追溯。"""
    items = service.list_reports(team_id)
    return {"items": items, "total": len(items)}
