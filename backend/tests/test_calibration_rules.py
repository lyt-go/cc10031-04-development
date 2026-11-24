"""按生效范围选用的单位标定规则测试。

覆盖：独占区间换算、同优先级交叠待确认、高优先级交叠直接确定、
迟到样本、重送幂等与原值冲突、时间格读取、现场确认后补换算、溯源核对，
以及未获准/已失效规则不参与选用。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import calibration_rules as cr
from app.services.unit_calibrate import (
    UnitCalibrateService, sample_key_of, TABLE_RESULT, TABLE_DISPOSITION, TABLE_ORIGIN)
from app.services.upload_batch import UploadBatchService
from app.store import store

POINT = "SENS-RANGE"
OLD_RULE = "UNIT-OLD-001"
NEW_RULE = "UNIT-NEW-001"

# 旧规则独占：t < 10:00；交叠区：[10:00, 11:00)；新规则独占：t >= 11:00
T_OLD = "2026-11-24T09:30:00+00:00"
T_OVERLAP = "2026-11-24T10:15:00+00:00"
T_NEW = "2026-11-24T11:30:00+00:00"


@pytest.fixture
def svc_env():
    """每个用例使用独立 store 与临时数据目录。"""
    saved = dict(store._tables)
    store._tables = {name: [dict(r) for r in rows] for name, rows in saved.items()}
    with tempfile.TemporaryDirectory() as tmp:
        with patch("app.services.base.DATA_ROOT", Path(tmp)):
            import app.services.unit_calibrate as uc
            import app.services.upload_batch as ub
            uc.DATA_ROOT = Path(tmp)
            ub.DATA_ROOT = Path(tmp)
            yield UnitCalibrateService(), Path(tmp)
    store._tables = saved


def _register(svc_env, rule_id, start, end, scale, priority="0", status="已标定"):
    svc, _ = svc_env
    rule, missing = svc.register_calibration_rule({
        "标定编号": rule_id, "测点编号": POINT, "原始单位": "mV", "目标单位": "V",
        "换算系数": str(scale), "零点偏移": "0", "标定方式": "线性换算",
        "生效起点": start, "生效终点": end, "优先级": priority,
    })
    assert not missing, missing
    if status != "已标定":
        rule["status"] = status
    return rule


def _three_samples():
    return [
        {"timestamp": T_OLD, "raw_value": 100.0, "unit": "mV"},
        {"timestamp": T_OVERLAP, "raw_value": 100.0, "unit": "mV", "is_late": True},
        {"timestamp": T_NEW, "raw_value": 100.0, "unit": "mV"},
    ]


class TestRuleEngine:
    def test_exclusive_intervals_pick_single_rule(self, svc_env):
        _register(svc_env, OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00", 0.001)
        _register(svc_env, NEW_RULE, "2026-11-24T10:00:00+00:00", "", 0.002)
        svc, _ = svc_env
        d_old = svc.resolve_sample(POINT, T_OLD)
        d_new = svc.resolve_sample(POINT, T_NEW)
        assert d_old.decision == cr.DECISION_EXCLUSIVE and d_old.selected["规则编号"] == OLD_RULE
        assert d_new.decision == cr.DECISION_EXCLUSIVE and d_new.selected["规则编号"] == NEW_RULE

    def test_equal_priority_overlap_waits_for_confirmation(self, svc_env):
        _register(svc_env, OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00", 0.001)
        _register(svc_env, NEW_RULE, "2026-11-24T10:00:00+00:00", "", 0.002)
        svc, _ = svc_env
        d = svc.resolve_sample(POINT, T_OVERLAP)
        assert d.decision == cr.DECISION_OVERLAP_PENDING
        assert {r["规则编号"] for r in d.candidates} == {OLD_RULE, NEW_RULE}
        assert d.selected is None

    def test_higher_priority_overlap_resolves_deterministically(self, svc_env):
        _register(svc_env, OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00", 0.001, "0")
        _register(svc_env, NEW_RULE, "2026-11-24T10:00:00+00:00", "", 0.002, "10")
        svc, _ = svc_env
        d = svc.resolve_sample(POINT, T_OVERLAP)
        assert d.decision == cr.DECISION_OVERLAP_RESOLVED
        assert d.selected["规则编号"] == NEW_RULE

    def test_draft_and_expired_rules_are_ignored(self, svc_env):
        _register(svc_env, OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00",
                  0.001, status="草稿")
        svc, _ = svc_env
        d = svc.resolve_sample(POINT, T_OLD)
        assert d.decision == cr.DECISION_NO_RULE

    def test_interval_relation_reports_exclusive_and_overlap_parts(self, svc_env):
        _register(svc_env, OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00", 0.001)
        _register(svc_env, NEW_RULE, "2026-11-24T10:00:00+00:00", "", 0.002)
        svc, _ = svc_env
        rel = svc.rule_interval_relation(OLD_RULE, NEW_RULE)
        assert rel["overlap"]["start"].startswith("2026-11-24T10:00:00")
        assert rel["overlap"]["end"].startswith("2026-11-24T11:00:00")
        assert rel["exclusive_a"]["start"].startswith("2026-11-24T00:00:00")
        assert rel["exclusive_b"]["end"] is None  # 新规则长期有效


class TestBatchCalibration:
    def _setup_rules(self, svc_env):
        _register(svc_env, OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00", 0.001)
        _register(svc_env, NEW_RULE, "2026-11-24T10:00:00+00:00", "", 0.002)

    def test_exclusive_values_and_single_pending_disposition(self, svc_env):
        self._setup_rules(svc_env)
        svc, _ = svc_env
        summary = svc.ingest_batch("BATCH-1", POINT, _three_samples(),
                                   late_keys={sample_key_of(POINT, T_OVERLAP)})
        assert summary["converted"] == 2
        assert summary["pending_confirmation"] == 1
        assert summary["new_origin"] == 3
        # 独占区结果分别为 0.1 V 和 0.2 V
        old = svc._find_result(sample_key_of(POINT, T_OLD))
        new = svc._find_result(sample_key_of(POINT, T_NEW))
        assert old["calibrated_value"] == pytest.approx(0.1)
        assert new["calibrated_value"] == pytest.approx(0.2)
        assert old["rule_id"] == OLD_RULE and new["rule_id"] == NEW_RULE
        assert old["raw_value"] == 100.0 and old["raw_unit"] == "mV"  # 保留实际单位
        # 交叠点只有一份待确认处置，且没有成果
        skey = sample_key_of(POINT, T_OVERLAP)
        assert len([d for d in store.rows(TABLE_DISPOSITION) if d["sample_key"] == skey]) == 1
        assert svc._find_result(skey) is None
        # 迟到标记保留
        assert svc._find_origin(skey)["is_late"] is True
        # 批次核对通过
        assert svc.verify_batch("BATCH-1")["ok"] is True

    def test_late_record_uses_event_time_not_arrival_order(self, svc_env):
        self._setup_rules(svc_env)
        svc, _ = svc_env
        # 先到新独占区样本，再迟到交叠区样本：结论仍按事件时间判定
        summary = svc.ingest_batch("BATCH-LATE", POINT, [
            {"timestamp": T_NEW, "raw_value": 100.0},
            {"timestamp": T_OVERLAP, "raw_value": 100.0, "is_late": True},
        ])
        assert summary["converted"] == 1
        assert summary["pending_confirmation"] == 1

    def test_resend_is_idempotent_and_keeps_single_records(self, svc_env):
        self._setup_rules(svc_env)
        svc, _ = svc_env
        svc.ingest_batch("BATCH-1", POINT, _three_samples())
        again = svc.ingest_batch("BATCH-1", POINT, _three_samples(), is_resend=True)
        assert again["new_origin"] == 0
        assert again["converted"] == 0
        assert again["duplicate_unchanged"] == 3
        assert again["pending_confirmation"] == 0  # 处置记录已存在，不重复登记
        # 独占区始终各一条成果、交叠点仅一份处置
        for ts in (T_OLD, T_NEW):
            skey = sample_key_of(POINT, ts)
            assert len([r for r in store.rows(TABLE_RESULT) if r["sample_key"] == skey]) == 1
        skey = sample_key_of(POINT, T_OVERLAP)
        assert len([d for d in store.rows(TABLE_DISPOSITION) if d["sample_key"] == skey]) == 1
        assert svc.verify_batch("BATCH-1")["ok"] is True

    def test_resend_with_changed_raw_value_is_rejected_and_audited(self, svc_env):
        self._setup_rules(svc_env)
        svc, data_dir = svc_env
        svc.ingest_batch("BATCH-C", POINT, _three_samples())
        tampered = [dict(s, raw_value=999.0) for s in _three_samples()]
        out = svc.ingest_batch("BATCH-C", POINT, tampered, is_resend=True)
        assert out["value_conflict"] == 3
        # 原值与成果均未被改写
        old = svc._find_result(sample_key_of(POINT, T_OLD))
        assert old["raw_value"] == 100.0 and old["calibrated_value"] == pytest.approx(0.1)
        audit = data_dir / "calibration_audit" / "BATCH-C_audit.jsonl"
        assert audit.exists() and "value_conflict" in audit.read_text(encoding="utf-8")

    def test_confirm_overlap_then_cell_reads_value(self, svc_env):
        self._setup_rules(svc_env)
        svc, _ = svc_env
        svc.ingest_batch("BATCH-1", POINT, _three_samples())
        skey = sample_key_of(POINT, T_OVERLAP)
        # 确认前时间格读取：等待现场确认
        cell_before = svc.read_aligned_cell(POINT, T_OVERLAP, "1h")
        assert cell_before["status"] == "待现场确认"
        assert cell_before["readable_value"] is None
        assert set(cell_before["candidate_rule_ids"]) == {OLD_RULE, NEW_RULE}
        # 现场确认采用新规则 → 0.2 V
        confirmed = svc.confirm_overlap(skey, NEW_RULE, "工程师甲")
        assert confirmed["ok"] is True
        assert confirmed["result"]["calibrated_value"] == pytest.approx(0.2)
        # 确认后时间格读取：实际读到新规则的标定值
        cell_after = svc.read_aligned_cell(POINT, T_OVERLAP, "1h")
        assert cell_after["status"] == "已标定"
        assert cell_after["readable_value"] == pytest.approx(0.2)
        assert cell_after["rule_id"] == NEW_RULE
        # 仍只有一份处置记录、一条成果
        assert len([d for d in store.rows(TABLE_DISPOSITION) if d["sample_key"] == skey]) == 1
        assert len([r for r in store.rows(TABLE_RESULT) if r["sample_key"] == skey]) == 1
        # 不能重复确认导致二次换算
        again = svc.confirm_overlap(skey, OLD_RULE, "工程师乙")
        assert "error" in again

    def test_confirm_rejects_non_candidate_rule(self, svc_env):
        self._setup_rules(svc_env)
        svc, _ = svc_env
        svc.ingest_batch("BATCH-1", POINT, _three_samples())
        _register(svc_env, "UNIT-OTHER", "2026-11-24T00:00:00+00:00", "2026-11-24T09:00:00+00:00", 0.009)
        out = svc.confirm_overlap(sample_key_of(POINT, T_OVERLAP), "UNIT-OTHER", "工程师甲")
        assert "error" in out

    def test_trace_sample_follows_batch_origin_rule_and_rechecks(self, svc_env):
        self._setup_rules(svc_env)
        svc, _ = svc_env
        svc.ingest_batch("BATCH-1", POINT, _three_samples())
        trace = svc.trace_sample(sample_key_of(POINT, T_OLD))
        assert trace["origin"]["batch_key"] == "BATCH-1"
        assert trace["result"]["rule_id"] == OLD_RULE
        assert trace["rule"]["换算系数"] == "0.001"
        assert trace["recheck_ok"] is True
        # 规则失效后重算：保留的成果仍可追溯到当时使用的规则版本
        trace["rule"]["status"] = "已失效"
        assert svc.trace_sample(sample_key_of(POINT, T_OLD))["result"]["calibrated_value"] == pytest.approx(0.1)

    def test_upload_service_preserves_origin_and_returns_calibration(self, svc_env):
        self._setup_rules(svc_env)
        _, data_dir = svc_env
        up = UploadBatchService()
        out = up.submit_samples("BATCH-UP", POINT, _three_samples(),
                                late_keys={sample_key_of(POINT, T_OVERLAP)})
        assert out["calibration"]["converted"] == 2
        assert out["calibration"]["pending_confirmation"] == 1
        # 原点文件与标定序列文件均已落盘
        assert (data_dir / "raw_uploads" / "BATCH-UP.jsonl").exists()
        series = data_dir / "calibrated_series" / f"{POINT}_calibrated_series.jsonl"
        assert series.exists()
        # 处置文件每样本仅一份
        disp_file = data_dir / "calibration_dispositions" / f"{POINT}_dispositions.jsonl"
        lines = [ln for ln in disp_file.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 1
        # 重送不追加任何原点/成果
        again = up.submit_samples("BATCH-UP", POINT, _three_samples(), is_resend=True)
        assert again["calibration"]["new_origin"] == 0
        assert len(series.read_text(encoding="utf-8").splitlines()) == 2
        assert len(disp_file.read_text(encoding="utf-8").splitlines()) == 1

    def test_no_rule_sample_is_flagged_without_corrupting_others(self, svc_env):
        _register(svc_env, OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00", 0.001)
        svc, _ = svc_env
        out = svc.ingest_batch("BATCH-N", POINT, [
            {"timestamp": T_OLD, "raw_value": 100.0},
            {"timestamp": T_NEW, "raw_value": 100.0},
        ])
        assert out["converted"] == 1 and out["no_rule"] == 1
        assert len(store.rows(TABLE_ORIGIN)) == 2  # 原点仍然保留
