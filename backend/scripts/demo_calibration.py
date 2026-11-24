"""端到端场景演示：按生效范围选用的单位标定规则。

场景（同一测点 SENS-RANGE，三个原值均为 100 mV 的样本）：
  1. 旧规则 UNIT-OLD-001（系数 0.001）独占区样本 09:30 → 期望 0.1 V
  2. 两条获准规则交叠区样本 10:15（恰好迟到）→ 先待现场确认，确认采用新规则后为 0.2 V
  3. 新规则 UNIT-NEW-001（系数 0.002）独占区样本 11:30 → 期望 0.2 V
随后时间对齐读取交叠点所在格，再重送原批核对唯一性与溯源链路。

运行：backend/.venv/bin/python scripts/demo_calibration.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.base import DATA_ROOT  # noqa: E402
import app.services.unit_calibrate as uc_mod  # noqa: E402
import app.services.upload_batch as ub_mod  # noqa: E402
from app.services.unit_calibrate import UnitCalibrateService, sample_key_of  # noqa: E402
from app.services.upload_batch import UploadBatchService  # noqa: E402
from app.store import store  # noqa: E402

POINT = "SENS-RANGE"
OLD_RULE, NEW_RULE = "UNIT-OLD-001", "UNIT-NEW-001"
BATCH = "BATCH-DEMO-001"
T_OLD, T_OVERLAP, T_NEW = (
    "2026-11-24T09:30:00+00:00",
    "2026-11-24T10:15:00+00:00",
    "2026-11-24T11:30:00+00:00",
)


def main() -> None:
    saved = {name: [dict(r) for r in rows] for name, rows in store._tables.items()}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with patch("app.services.base.DATA_ROOT", tmp_path):
            uc_mod.DATA_ROOT = tmp_path
            ub_mod.DATA_ROOT = tmp_path
            cal, up = UnitCalibrateService(), UploadBatchService()

            print("== 1. 登记两条生效范围获准规则（交叠区 [10:00, 11:00)）==")
            for args in (
                (OLD_RULE, "2026-11-24T00:00:00+00:00", "2026-11-24T11:00:00+00:00", 0.001),
                (NEW_RULE, "2026-11-24T10:00:00+00:00", "", 0.002),
            ):
                rule, missing = cal.register_calibration_rule({
                    "标定编号": args[0], "测点编号": POINT, "原始单位": "mV", "目标单位": "V",
                    "换算系数": str(args[3]), "零点偏移": "0",
                    "生效起点": args[1], "生效终点": args[2], "优先级": "0",
                })
                assert not missing, missing
                print(f"  - {args[0]}: 系数 {args[3]}，生效 [{args[1]}, {args[2] or '∞'})")
            rel = cal.rule_interval_relation(OLD_RULE, NEW_RULE)
            print(f"  区间核对：交叠段 {rel['overlap']['start']} ~ {rel['overlap']['end']}")

            print("\n== 2. 批量上送（含标定变更前后样本；10:15 为迟到记录）==")
            samples = [
                {"timestamp": T_OLD, "raw_value": 100.0, "unit": "mV"},
                {"timestamp": T_OVERLAP, "raw_value": 100.0, "unit": "mV", "is_late": True},
                {"timestamp": T_NEW, "raw_value": 100.0, "unit": "mV"},
            ]
            out = up.submit_samples(
                BATCH, POINT, samples,
                late_keys={sample_key_of(POINT, T_OVERLAP)})
            s = out["calibration"]
            print(f"  新原点 {s['new_origin']} 条，已换算 {s['converted']} 条，"
                  f"待现场确认 {s['pending_confirmation']} 条，迟到 {sum(1 for x in s['outcomes'] if x['is_late'])} 条")

            old_r = cal._find_result(sample_key_of(POINT, T_OLD))
            new_r = cal._find_result(sample_key_of(POINT, T_NEW))
            print(f"  旧规则独占区 09:30 → {old_r['calibrated_value']} {old_r['target_unit']}"
                  f"（规则 {old_r['rule_id']}，原点保留 {old_r['raw_value']}{old_r['raw_unit']}）")
            print(f"  新规则独占区 11:30 → {new_r['calibrated_value']} {new_r['target_unit']}"
                  f"（规则 {new_r['rule_id']}，原点保留 {new_r['raw_value']}{new_r['raw_unit']}）")
            assert old_r["calibrated_value"] == 0.1 and new_r["calibrated_value"] == 0.2

            print("\n== 3. 时间对齐读取交叠点 10:15 所在的 1h 格 ==")
            cell = cal.read_aligned_cell(POINT, T_OVERLAP, "1h")
            print(f"  格 {cell['cell_id']}：{cell['status']}，可读值={cell['readable_value']}")
            print(f"  原因：{cell['reason']}；候选规则={cell['candidate_rule_ids']}")

            print("\n== 4. 现场确认交叠点采用新规则，补做唯一一次换算 ==")
            skey = sample_key_of(POINT, T_OVERLAP)
            confirmed = cal.confirm_overlap(skey, NEW_RULE, "现场工程师甲")
            print(f"  交叠点 → {confirmed['result']['calibrated_value']} V"
                  f"（规则 {confirmed['result']['rule_id']}，确认人 {confirmed['disposition']['operator']}）")
            cell2 = cal.read_aligned_cell(POINT, T_OVERLAP, "1h")
            print(f"  再次读格：{cell2['status']}，实际读到 {cell2['readable_value']} V（规则 {cell2['rule_id']}）")

            print("\n== 5. 重送原批：去重、不二次换算、原值可核对 ==")
            again = up.submit_samples(BATCH, POINT, samples, is_resend=True)["calibration"]
            print(f"  新原点 {again['new_origin']}，新换算 {again['converted']}，"
                  f"重复跳过 {again['duplicate_unchanged']}，待确认新增 {again['pending_confirmation']}")
            verify = cal.verify_batch(BATCH)
            print(f"  批次核对：ok={verify['ok']}，成果 {verify['converted']} 条，"
                  f"待确认处置 {verify['pending_confirmation']} 条，异常 {verify['anomalies']}")

            print("\n== 6. 从采用结果追溯原批、原点与规则并复算 ==")
            for ts, expect in ((T_OLD, 0.1), (T_OVERLAP, 0.2), (T_NEW, 0.2)):
                trace = cal.trace_sample(sample_key_of(POINT, ts))
                print(f"  {ts}: {' -> '.join(trace['trace_chain'])}")
                print(f"    按规则复算核对：{'通过' if trace['recheck_ok'] else '失败'}")
                assert trace["recheck_ok"] and trace["result"]["calibrated_value"] == expect

            print("\n== 数据产物文件 ==")
            for path in sorted(tmp_path.rglob("*.jsonl")):
                lines = path.read_text(encoding="utf-8").strip().splitlines()
                print(f"  {path.relative_to(tmp_path)}：{len(lines)} 行")
            print("\n全部断言通过。")

    store._tables = saved


if __name__ == "__main__":
    main()
