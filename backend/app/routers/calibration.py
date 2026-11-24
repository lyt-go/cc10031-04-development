"""单位标定专用接口：生效范围规则、批量上送标定、交叠处置、时间格读取与溯源核对。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.unit_calibrate import UnitCalibrateService
from app.services.upload_batch import UploadBatchService

router = APIRouter(prefix="/api/calibration", tags=["单位标定"])


class RulePayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class Sample(BaseModel):
    timestamp: str
    raw_value: float
    unit: str = "mV"
    is_late: bool = False


class BatchPayload(BaseModel):
    batch_key: str
    point: str
    samples: list[Sample]
    late_keys: list[str] = Field(default_factory=list)
    is_resend: bool = False


class ConfirmPayload(BaseModel):
    sample_key: str
    rule_id: str
    operator: str = "现场工程师"


@router.post("/rules")
def register_rule(payload: RulePayload) -> dict[str, Any]:
    """登记一条按生效范围选用的换算规则（获准状态即可参与换算）。"""
    svc = UnitCalibrateService()
    rule, missing = svc.register_calibration_rule(payload.values)
    if missing:
        return {"ok": False, "message": "；".join(missing)}
    return {"ok": True, "message": "标定规则已登记并获准", "rule": rule}


@router.post("/batches")
def submit_batch(payload: BatchPayload) -> dict[str, Any]:
    """批量上送原始点：保留原点并按采样时间选用生效规则完成标定，支持迟到与重送。"""
    svc = UploadBatchService()
    return svc.submit_samples(
        payload.batch_key, payload.point,
        [s.model_dump() for s in payload.samples],
        late_keys=set(payload.late_keys), is_resend=payload.is_resend)


@router.post("/overlap/confirm")
def confirm_overlap(payload: ConfirmPayload) -> dict[str, Any]:
    """现场确认交叠点采用的获准规则并补做唯一一次换算。"""
    svc = UnitCalibrateService()
    return svc.confirm_overlap(payload.sample_key, payload.rule_id, payload.operator)


@router.get("/cell/{point}")
def read_cell(point: str, timestamp: str, grid: str = "1h") -> dict[str, Any]:
    """时间对齐读取样本所在格，返回实际读到的标定值或待现场确认原因。"""
    return UnitCalibrateService().read_aligned_cell(point, timestamp, grid)


@router.get("/trace/{sample_key:path}")
def trace_sample(sample_key: str) -> dict[str, Any]:
    """从标定结果追溯原批、原点与采用规则，并按规则复算核对。"""
    return UnitCalibrateService().trace_sample(sample_key)


@router.get("/batches/{batch_key}/verify")
def verify_batch(batch_key: str) -> dict[str, Any]:
    """重送核对：独占区每样本恰一条成果、交叠点恰一份处置、成果可溯源。"""
    return UnitCalibrateService().verify_batch(batch_key)


@router.get("/rules/{rule_a}/relation/{rule_b}")
def rule_relation(rule_a: str, rule_b: str) -> dict[str, Any]:
    """核对两条相邻规则的独占段与交叠段。"""
    return UnitCalibrateService().rule_interval_relation(rule_a, rule_b)
