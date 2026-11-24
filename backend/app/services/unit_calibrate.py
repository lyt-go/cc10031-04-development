"""单位标定领域服务：维护传感器原始值到目标计量单位的换算系数与零点偏移标定。
对标定记录进行单位转换计算并写入标定结果数据文件。

在原有按标定记录整体换算的基础上，新增按测点与采样时间选用生效规则的能力：
- 标定规则带半开生效区间 [生效起点, 生效终点) 与优先级，支持相邻规则交叠；
- 批量上送保留原始点，标定结果关联使用的规则、时间段与上送批次，每个样本只换算一次；
- 同优先级规则交叠时不擅自换算，仅保留一份待现场确认处置记录，确认后再补换算；
- 支持时间对齐格读取、原批溯源核对与幂等重送。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services import calibration_rules as cr
from app.store import store

# 内存结果表（首次使用时在 store 中惰性创建，不影响既有 18 个模块清单）
TABLE_RESULT = "calibrated_results"
TABLE_DISPOSITION = "calibration_dispositions"
TABLE_ORIGIN = "calibration_origin"


def sample_key_of(point: str, timestamp: str) -> str:
    return f"{point}|{timestamp}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate_to_grid(ts: datetime, grid: str) -> datetime:
    """把采样时刻截断到对齐格（支持 1s/1m/1h/1d）。"""
    if grid.endswith("d"):
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if grid.endswith("h"):
        return ts.replace(minute=0, second=0, microsecond=0)
    if grid.endswith("m"):
        return ts.replace(second=0, microsecond=0)
    return ts.replace(microsecond=0)


class UnitCalibrateService(BaseModuleService):
    MODULE = "unit_calibrate"
    ENTITY = "标定记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["标定编号", "测点编号", "原始单位", "目标单位", "换算系数", "零点偏移",
                   "标定方式", "标定时间", "生效起点", "生效终点", "优先级"]
    REQUIRED_FIELDS = ["标定编号", "测点编号", "原始单位"]
    STATUS_ORDER = ["草稿", "待标定", "已标定", "已失效"]
    ACTION_RULES = {"提交标定": "待标定", "确认标定": "已标定", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = ["标记失效"]

    # -- 生效范围标定规则 --

    def register_calibration_rule(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记一条按生效范围选用的换算规则；状态为已标定即视为获准参与换算。"""
        missing = [f for f in ("标定编号", "测点编号", "原始单位", "目标单位", "生效起点")
                   if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        # 编号唯一性与区间合法性校验
        if any(r.get("标定编号") == values["标定编号"] for r in store.rows(self.MODULE)):
            return None, [f"标定编号 {values['标定编号']} 已存在"]
        start = cr.parse_ts(values["生效起点"])
        end_text = values.get("生效终点")
        if end_text:
            end = cr.parse_ts(end_text)
            if end <= start:
                return None, ["生效终点必须晚于生效起点（半开区间 [起点, 终点)）"]
        rows = store.rows(self.MODULE)
        rule = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        for field in self.LIST_FIELDS:
            rule[field] = values.get(field, "")
        rule.setdefault("换算系数", "1")
        rule.setdefault("零点偏移", "0")
        rule.setdefault("优先级", "0")
        rule.setdefault("生效终点", "")
        rule.setdefault("标定方式", "线性换算")
        rule["规则编号"] = rule["标定编号"]
        rule["status"] = "已标定"
        rule["pending"] = False
        rule["abnormal"] = False
        rows.append(rule)
        return rule, []

    def _rules_for_point(self, point: str) -> list[dict[str, Any]]:
        return [r for r in store.rows(self.MODULE) if r.get("测点编号") == point]

    def resolve_sample(self, point: str, timestamp: str) -> cr.RuleDecision:
        """对外暴露的规则选用判定，便于相邻规则冲突时核对单次换算依据。"""
        return cr.select_rule(self._rules_for_point(point), point, timestamp)

    def rule_interval_relation(self, rule_id_a: str, rule_id_b: str) -> dict[str, Any]:
        """核对两条相邻规则的独占段与交叠段。"""
        ra = next((r for r in store.rows(self.MODULE) if r.get("规则编号") == rule_id_a), None)
        rb = next((r for r in store.rows(self.MODULE) if r.get("规则编号") == rule_id_b), None)
        if ra is None or rb is None:
            return {"error": f"规则 {rule_id_a} 或 {rule_id_b} 不存在"}
        relation = cr.interval_relation(ra, rb)
        for key in ("overlap", "exclusive_a", "exclusive_b"):
            part = relation[key]
            if part:
                relation[key] = {"start": part["start"].isoformat(),
                                 "end": part["end"].isoformat() if part["end"] else None}
        return relation

    # -- 批量上送：保留原点 + 每样本只换算一次 --

    def _origin_rows(self) -> list[dict[str, Any]]:
        return store.rows(TABLE_ORIGIN)

    def _result_rows(self) -> list[dict[str, Any]]:
        return store.rows(TABLE_RESULT)

    def _disposition_rows(self) -> list[dict[str, Any]]:
        return store.rows(TABLE_DISPOSITION)

    def _find_origin(self, skey: str) -> dict[str, Any] | None:
        return next((r for r in self._origin_rows() if r["sample_key"] == skey), None)

    def _find_result(self, skey: str) -> dict[str, Any] | None:
        return next((r for r in self._result_rows() if r["sample_key"] == skey), None)

    def _find_disposition(self, skey: str) -> dict[str, Any] | None:
        return next((r for r in self._disposition_rows() if r["sample_key"] == skey), None)

    def ingest_batch(
        self,
        batch_key: str,
        point: str,
        samples: list[dict[str, Any]],
        *,
        late_keys: set[str] | None = None,
        is_resend: bool = False,
    ) -> dict[str, Any]:
        """接收一个上送批次的原始点并完成规则选用与标定。

        原点只登记一次：重送时按样本键去重并核对原值，原值不一致只记审计不改写。
        每个样本最多产生一条标定成果；同优先级交叠只保留一份待确认处置记录。
        迟到样本按事件时间参与规则选用，与到达顺序无关。
        """
        late_keys = late_keys or set()
        summary = {"batch_key": batch_key, "point": point, "received": len(samples),
                   "new_origin": 0, "converted": 0, "pending_confirmation": 0,
                   "duplicate_unchanged": 0, "value_conflict": 0, "no_rule": 0,
                   "is_resend": is_resend, "outcomes": []}
        origin_file = self._data_dir("raw_uploads") / f"{batch_key}.jsonl"
        series_file = self._data_dir("calibrated_series") / f"{point}_calibrated_series.jsonl"
        disposition_file = self._data_dir("calibration_dispositions") / f"{point}_dispositions.jsonl"
        audit_file = self._data_dir("calibration_audit") / f"{batch_key}_audit.jsonl"

        new_origins: list[dict[str, Any]] = []
        new_results: list[dict[str, Any]] = []
        disposition_dirty = False

        for sample in samples:
            timestamp = str(sample["timestamp"])
            raw_value = float(sample["raw_value"])
            raw_unit = str(sample.get("unit", "mV"))
            is_late = sample.get("is_late", sample_key_of(point, timestamp) in late_keys)
            skey = sample_key_of(point, timestamp)
            outcome: dict[str, Any] = {"sample_key": skey, "timestamp": timestamp,
                                       "raw_value": raw_value, "is_late": bool(is_late)}

            # 1) 原点登记：重送去重 + 原值核对，绝不覆盖
            existed = self._find_origin(skey)
            if existed is not None:
                if float(existed["raw_value"]) != raw_value or existed.get("raw_unit") != raw_unit:
                    summary["value_conflict"] += 1
                    outcome["status"] = "原值冲突"
                    outcome["detail"] = (f"重送原值 {raw_value} {raw_unit} 与已保留原值 "
                                         f"{existed['raw_value']} {existed.get('raw_unit')} 不一致，拒绝改写")
                    self._write_jsonl(audit_file, [{"at": _now_iso(), "batch_key": batch_key,
                                                    "sample_key": skey, "event": "value_conflict",
                                                    "claimed_raw": raw_value,
                                                    "stored_raw": existed["raw_value"]}])
                    summary["outcomes"].append(outcome)
                    continue
                summary["duplicate_unchanged"] += 1
                outcome["status"] = "重复跳过"
                outcome["detail"] = "原点与成果均已存在，重送不重复换算"
                self._write_jsonl(audit_file, [{"at": _now_iso(), "batch_key": batch_key,
                                                "sample_key": skey, "event": "resend_duplicate"}])
                summary["outcomes"].append(outcome)
                continue

            origin = {"sample_key": skey, "batch_key": batch_key, "point": point,
                      "timestamp": timestamp, "raw_value": raw_value, "raw_unit": raw_unit,
                      "is_late": bool(is_late), "received_at": _now_iso()}
            self._origin_rows().append(origin)
            new_origins.append(origin)
            summary["new_origin"] += 1

            # 2) 按测点 + 采样时间选用生效规则
            decision = cr.select_rule(self._rules_for_point(point), point, timestamp)
            outcome["decision"] = decision.to_dict()

            if decision.decision == cr.DECISION_NO_RULE:
                summary["no_rule"] += 1
                outcome["status"] = "无可用规则"
                self._write_jsonl(audit_file, [{"at": _now_iso(), "batch_key": batch_key,
                                                "sample_key": skey, "event": "no_rule",
                                                "reason": decision.reason}])
                summary["outcomes"].append(outcome)
                continue

            grid_cell = self._grid_cell_id(point, cr.parse_ts(timestamp), "1h")
            if decision.convertible:
                # 独占区或高优先级确定区：每样本只换算一次
                if self._find_result(skey) is not None:
                    outcome["status"] = "成果已存在"
                else:
                    rule = decision.selected
                    calibrated = cr.convert(rule, raw_value)
                    result = {
                        "sample_key": skey, "batch_key": batch_key, "point": point,
                        "timestamp": timestamp, "raw_value": raw_value, "raw_unit": raw_unit,
                        "calibrated_value": calibrated, "target_unit": rule["目标单位"],
                        "rule_id": rule["规则编号"], "scale": float(rule["换算系数"]),
                        "offset": float(rule.get("零点偏移", 0) or 0),
                        "decision": decision.decision, "decision_reason": decision.reason,
                        "grid_cell": grid_cell, "is_late": bool(is_late),
                        "converted_at": _now_iso(),
                    }
                    self._result_rows().append(result)
                    new_results.append(result)
                    summary["converted"] += 1
                    outcome["status"] = "已换算"
                    outcome["calibrated_value"] = calibrated
                    outcome["rule_id"] = rule["规则编号"]
            else:
                # 同优先级交叠：只保留一份处置记录，等待现场确认
                if self._find_disposition(skey) is None:
                    self._disposition_rows().append({
                        "sample_key": skey, "batch_key": batch_key, "point": point,
                        "timestamp": timestamp, "raw_value": raw_value, "raw_unit": raw_unit,
                        "candidate_rule_ids": decision.to_dict()["candidate_rule_ids"],
                        "selected_rule_id": None, "status": "待现场确认",
                        "reason": decision.reason, "grid_cell": grid_cell,
                        "is_late": bool(is_late), "operator": None,
                        "created_at": _now_iso(), "updated_at": _now_iso(),
                    })
                    disposition_dirty = True
                    summary["pending_confirmation"] += 1
                    outcome["status"] = "待现场确认"
                else:
                    outcome["status"] = "处置记录已存在"
            summary["outcomes"].append(outcome)

        # 3) 持久化数据产物（原点/成果追加写；处置记录整文件重写保证每样本仅一份）
        if new_origins:
            self._write_jsonl(origin_file, new_origins)
        if new_results:
            self._write_jsonl(series_file, new_results)
        if disposition_dirty:
            self._rewrite_jsonl(disposition_file, self._disposition_rows())
        return summary

    def confirm_overlap(self, sample_key: str, rule_id: str, operator: str) -> dict[str, Any]:
        """现场确认交叠点采用哪条获准规则，随后补做唯一一次换算。"""
        disp = self._find_disposition(sample_key)
        if disp is None:
            return {"error": f"样本 {sample_key} 没有待确认的交叠处置记录"}
        if disp["status"] != "待现场确认":
            return {"error": f"处置记录已为 {disp['status']}，不能重复确认"}
        if rule_id not in disp["candidate_rule_ids"]:
            return {"error": f"规则 {rule_id} 不在交叠候选 {disp['candidate_rule_ids']} 内"}
        rule = next((r for r in store.rows(self.MODULE)
                     if r.get("规则编号") == rule_id and cr.is_approved(r)), None)
        if rule is None:
            return {"error": f"规则 {rule_id} 未获准或不存在"}
        if self._find_result(sample_key) is not None:
            return {"error": "该样本已有标定成果，禁止二次换算"}
        origin = self._find_origin(sample_key)
        raw_value = float(origin["raw_value"] if origin else disp["raw_value"])
        raw_unit = origin["raw_unit"] if origin else disp["raw_unit"]
        result = {
            "sample_key": sample_key, "batch_key": disp["batch_key"], "point": disp["point"],
            "timestamp": disp["timestamp"], "raw_value": raw_value, "raw_unit": raw_unit,
            "calibrated_value": cr.convert(rule, raw_value), "target_unit": rule["目标单位"],
            "rule_id": rule_id, "scale": float(rule["换算系数"]),
            "offset": float(rule.get("零点偏移", 0) or 0),
            "decision": "交叠-现场确认", "decision_reason": f"现场确认采用规则 {rule_id}",
            "grid_cell": disp["grid_cell"], "is_late": disp["is_late"],
            "confirmed_by": operator, "converted_at": _now_iso(),
        }
        self._result_rows().append(result)
        disp["status"] = "已确认"
        disp["selected_rule_id"] = rule_id
        disp["operator"] = operator
        disp["updated_at"] = _now_iso()
        series_file = self._data_dir("calibrated_series") / f"{disp['point']}_calibrated_series.jsonl"
        disposition_file = self._data_dir("calibration_dispositions") / f"{disp['point']}_dispositions.jsonl"
        self._write_jsonl(series_file, [result])
        self._rewrite_jsonl(disposition_file, self._disposition_rows())
        return {"ok": True, "sample_key": sample_key, "result": result, "disposition": disp}

    # -- 时间对齐格读取 --

    def _grid_cell_id(self, point: str, ts: datetime | str, grid: str) -> str:
        moment = cr.parse_ts(ts)
        cell_start = truncate_to_grid(moment, grid)
        return f"{point}|{grid}|{cell_start.isoformat()}"

    def read_aligned_cell(self, point: str, timestamp: str, grid: str = "1h") -> dict[str, Any]:
        """读取交叠点（或任一样本）所在对齐格，返回实际可读标定值或等待确认的原因。"""
        moment = cr.parse_ts(timestamp)
        cell_start = truncate_to_grid(moment, grid)
        cell_id = f"{point}|{grid}|{cell_start.isoformat()}"
        results = [r for r in self._result_rows()
                   if r["point"] == point and r.get("grid_cell") == cell_id]
        pendings = [d for d in self._disposition_rows()
                    if d["point"] == point and d.get("grid_cell") == cell_id]
        response: dict[str, Any] = {
            "cell_id": cell_id, "point": point, "grid": grid,
            "cell_start": cell_start.isoformat(), "cell_end": (cell_start + self._grid_delta(grid)).isoformat(),
            "calibrated": results, "pending": pendings,
        }
        if results:
            response["status"] = "已标定"
            response["readable_value"] = results[0]["calibrated_value"]
            response["rule_id"] = results[0]["rule_id"]
        elif pendings:
            response["status"] = "待现场确认"
            response["readable_value"] = None
            response["reason"] = pendings[0]["reason"]
            response["candidate_rule_ids"] = pendings[0]["candidate_rule_ids"]
        else:
            response["status"] = "格内无数据"
            response["readable_value"] = None
        return response

    @staticmethod
    def _grid_delta(grid: str) -> timedelta:
        return {"1s": timedelta(seconds=1), "1m": timedelta(minutes=1),
                "1h": timedelta(hours=1), "1d": timedelta(days=1)}.get(grid, timedelta(hours=1))

    # -- 溯源与重送核对 --

    def trace_sample(self, sample_key: str) -> dict[str, Any]:
        """从采用的标定结果追到原批、原点与规则，并按规则重算核对原值。"""
        origin = self._find_origin(sample_key)
        result = self._find_result(sample_key)
        disp = self._find_disposition(sample_key)
        rule = None
        recheck_ok = None
        if result:
            rule = next((r for r in store.rows(self.MODULE)
                         if r.get("规则编号") == result["rule_id"]), None)
            if rule is not None:
                recheck_ok = cr.convert(rule, float(result["raw_value"])) == result["calibrated_value"]
        return {
            "sample_key": sample_key, "origin": origin, "result": result,
            "disposition": disp, "rule": rule,
            "trace_chain": [x for x in (
                f"批次 {origin['batch_key']}" if origin else None,
                f"原点 {origin['raw_value']}{origin['raw_unit']} @ {origin['timestamp']}" if origin else None,
                f"规则 {result['rule_id']}(系数 {result['scale']})" if result else None,
                f"成果 {result['calibrated_value']}{result['target_unit']}" if result else None,
                f"处置 {disp['status']}" if disp else None,
            ) if x],
            "recheck_ok": recheck_ok,
        }

    def verify_batch(self, batch_key: str) -> dict[str, Any]:
        """核对批次：独占区每样本恰一条成果、交叠点恰一份处置记录，且成果可溯源。"""
        origins = [o for o in self._origin_rows() if o["batch_key"] == batch_key]
        anomalies: list[str] = []
        converted = pending = 0
        for origin in origins:
            skey = origin["sample_key"]
            results = [r for r in self._result_rows() if r["sample_key"] == skey]
            disps = [d for d in self._disposition_rows() if d["sample_key"] == skey]
            if len(results) > 1:
                anomalies.append(f"{skey} 存在 {len(results)} 条标定成果，违反每样本只换算一次")
            if len(disps) > 1:
                anomalies.append(f"{skey} 存在 {len(disps)} 份处置记录，违反交叠点仅一份处置")
            if results and disps and disps[0]["status"] == "待现场确认":
                anomalies.append(f"{skey} 已有成果但处置仍处于待确认")
            if results:
                converted += 1
                trace = self.trace_sample(skey)
                if trace["recheck_ok"] is not True:
                    anomalies.append(f"{skey} 成果无法按规则与原值复算核对")
                if results[0]["batch_key"] != batch_key:
                    anomalies.append(f"{skey} 成果关联批次 {results[0]['batch_key']} 与原批 {batch_key} 不一致")
            elif disps:
                pending += 1
            else:
                anomalies.append(f"{skey} 既无标定成果也无处置记录")
        return {"batch_key": batch_key, "samples": len(origins),
                "converted": converted, "pending_confirmation": pending,
                "anomalies": anomalies, "ok": not anomalies}

    def _rewrite_jsonl(self, filepath, records: list[dict[str, Any]]) -> None:
        """整文件重写 JSONL，用于处置记录这类必须保持每样本唯一的数据集。"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    # -- 既有按标定记录整体换算能力保持不变 --

    def calibrate_values(self, calibrate_id: int) -> dict[str, Any]:
        """对标定记录的原始数据进行单位换算计算并写入标定结果归档文件。"""
        cal = store.find(self.MODULE, calibrate_id)
        if cal is None:
            return {"error": f"标定记录 {calibrate_id} 不存在"}
        entry, _ = self.run_action(calibrate_id, "确认标定")
        point_key = entry["测点编号"]
        scale = float(entry.get("换算系数", "1"))
        offset = float(entry.get("零点偏移", "0"))
        # 读取对应测点的边缘采样数据
        samples_file = self._data_dir("edge_samples") / f"{point_key}.jsonl"
        samples = self._read_jsonl(samples_file)
        # 执行单位标定计算：原始值 × 换算系数 + 零点偏移
        calibrated = []
        for s in samples:
            raw = float(s.get("raw_value", 0))
            cal_val = round(raw * scale + offset, 4)
            calibrated.append({
                "point": s["point"],
                "timestamp": s["timestamp"],
                "raw_value_mV": raw,
                "calibrated_value_V": cal_val,
                "scale": scale,
                "offset": offset,
                # 数据转换输出：将标定计算结果写入持久化标定数据JSONL文件存储
                "calibrate_id": entry["标定编号"],
            })
        # 将标定结果写入持久化标定数据文件
        output_file = self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"
        written = self._write_jsonl(output_file, calibrated)
        entry["标定时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        return {"calibrate_id": entry["标定编号"], "point": point_key,
                "records_calibrated": written, "scale": scale, "offset": offset}

    def batch_calibrate(self, point_keys: list[str]) -> dict[str, Any]:
        """批量执行多个测点的单位标定转换，返回标定汇总数据集。"""
        results = []
        for pk in point_keys:
            cals = [r for r in store.rows(self.MODULE)
                    if r.get("测点编号") == pk and r.get("status") in ("待标定", "已标定")]
            for cal in cals:
                r = self.calibrate_values(cal["id"])
                results.append(r)
        # 写入批量标定汇总日志文件
        self._write_jsonl(
            self._data_dir("calibrated") / "batch_calibrate_summary.jsonl",
            [{"action": "batch_calibrate", "points": len(point_keys),
              "executed": len(results), "at": time.time()}]
        )
        return {"total_points": len(point_keys), "executed": len(results), "results": results}
