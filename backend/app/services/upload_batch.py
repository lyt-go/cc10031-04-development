"""批量上送领域服务：维护缓冲分片按范围批量上送的记录条数、压缩方式与批次校验值。
上送完成后将数据写入上送批次文件，自动触发单位标定和接入水位推进。"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class UploadBatchService(BaseModuleService):
    MODULE = "upload_batch"
    ENTITY = "上送批次"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["批次编号", "分片范围", "测点数量", "记录条数", "压缩方式", "批次校验值", "上送时长", "批次水位"]
    REQUIRED_FIELDS = ["批次编号", "分片范围", "测点数量"]
    STATUS_ORDER = ["草稿", "待上送", "上送中", "已完成", "已失败"]
    ACTION_RULES = {"启动上送": "上送中", "标记完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    # 数据导入操作：读取边缘采样文件启动批次上送写入采集数据校验集记录
    def submit_batch(self, batch_id: int) -> dict[str, Any]:
        """启动批次上送并模拟压缩传输，将上送结果写入批次交付数据文件。"""
        batch = store.find(self.MODULE, batch_id)
        if batch is None:
            return {"error": f"上送批次 {batch_id} 不存在"}
        entry, _ = self.run_action(batch_id, "启动上送")
        batch_key = entry["批次编号"]
        shard = entry.get("分片范围", batch_key)
        # 读取对应的边缘采样文件模拟上送
        samples_file = self._data_dir("edge_samples") / f"{shard}.jsonl"
        samples = self._read_jsonl(samples_file)
        upload_duration = len(samples) * 0.01
        batch["上送时长"] = f"{upload_duration:.2f}"
        batch["记录条数"] = str(len(samples))
        # 写入上送批次持久化记录文件，记录传输摘要
        self._write_jsonl(
            # 批次校验：将上送交付记录写入持久化日志数据文件
            self._data_dir("upload_batches") / f"{batch_key}.jsonl",
            [{"action": "start_upload", "batch": batch_key, "shard": shard,
              "record_count": len(samples), "upload_duration_s": round(upload_duration, 2),
              "compressed_size": len(samples) * 12, "at": time.time()}]
        )
        return {"batch_key": batch_key, "shard": shard, "records": len(samples),
                "duration": round(upload_duration, 2), "status": "上送中"}

    def submit_samples(
        self,
        batch_key: str,
        point: str,
        samples: list[dict[str, Any]],
        *,
        late_keys: set[str] | None = None,
        is_resend: bool = False,
    ) -> dict[str, Any]:
        """按批上送原始采样点：后端先保留原点，再按测点+采样时间的生效规则做标定。

        - 允许同一批次包含标定变更前后的样本，规则按各样本自己的采样时间选用；
        - 迟到记录照常按事件时间参与规则选用，并在原点与成果上保留 is_late 标记；
        - 重送整批时原点去重、原值核对，每个样本始终最多换算一次；
        - 同优先级规则交叠的点不换算，仅生成一份待现场确认处置记录。
        """
        late_keys = late_keys or set()
        # 上送批次登记（重送沿用同一批次编号，新增一条交付日志，不覆盖历史）
        rows = store.rows(self.MODULE)
        entry = next((r for r in rows if r.get("批次编号") == batch_key), None)
        if entry is None:
            entry = self._auto_create_seed(self.MODULE, {
                "批次编号": batch_key, "分片范围": point, "测点数量": "1",
                "记录条数": str(len(samples)), "压缩方式": "none",
                "批次校验值": self._compute_checksum(json.dumps(samples, sort_keys=True, default=str)),
                "上送时长": "0", "批次水位": str(len(samples)),
            })
        self._write_jsonl(
            self._data_dir("upload_batches") / f"{batch_key}.jsonl",
            [{"action": "resend" if is_resend else "submit_samples",
              "batch": batch_key, "point": point, "records": len(samples),
              "late": len(late_keys), "at": time.time()}]
        )
        cal_svc = self._safe_import(
            "app.services.unit_calibrate", "UnitCalibrateService")()
        calibration = cal_svc.ingest_batch(
            batch_key, point, samples, late_keys=late_keys, is_resend=is_resend)
        return {"batch_key": batch_key, "point": point, "is_resend": is_resend,
                "records": len(samples), "calibration": calibration}

    def complete_batch(self, batch_id: int) -> dict[str, Any]:
        """确认批次上送完成并写入上送记录到交付日志数据集，自动触发单位标定和接入水位推进。"""
        batch = store.find(self.MODULE, batch_id)
        if batch is None:
            return {"error": f"上送批次 {batch_id} 不存在"}
        entry, _ = self.run_action(batch_id, "标记完成")
        batch_key = entry["批次编号"]
        shard = entry.get("分片范围", batch_key)
        # 将上送记录写入持久化交付文件
        self._write_jsonl(
            self._data_dir("upload_batches") / f"{batch_key}.jsonl",
            [{"action": "complete", "batch": batch_key, "shard": shard,
              "total_records": entry.get("记录条数", "0"), "completed_at": time.time()}]
        )
        triggers = {}
        # 上送完成后自动触发单位标定
        try:
            cal_svc = self._safe_import("app.services.unit_calibrate", "UnitCalibrateService")()
            cal_entry = self._auto_create_seed("unit_calibrate", {
                "标定编号": f"UNIT-{batch_key}",
                "测点编号": shard,
                "原始单位": "mV",
                "目标单位": "V",
                "换算系数": "0.001",
                "零点偏移": "0",
                "标定方式": "线性换算",
                "标定时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            })
            if cal_entry:
                triggers["unit_calibrate"] = {"created": cal_entry["标定编号"], "status": "已生效"}
        except Exception as e:
            triggers["unit_calibrate"] = {"error": str(e)}
        # 上送完成后自动推进接入水位
        try:
            wm_svc = self._safe_import("app.services.ingest_watermark", "IngestWatermarkService")()
            wm_entry = self._auto_create_seed("ingest_watermark", {
                "水位编号": f"WATER-{batch_key}",
                "测点编号": shard,
                "事件时间水位": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "处理时间水位": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "滞后秒数": "0",
                "迟到点数": "0",
                "推进方式": "自动推进",
                "检查时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            })
            if wm_entry:
                triggers["ingest_watermark"] = {"created": wm_entry["水位编号"], "status": "正常"}
        except Exception as e:
            triggers["ingest_watermark"] = {"error": str(e)}
        return {
            "batch_entry": entry,
            "batch_key": batch_key,
            "completed_at": time.time(),
            "auto_triggers": triggers,
        }