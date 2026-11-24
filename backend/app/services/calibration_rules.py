"""标定换算规则引擎：按测点与采样时间在规则生效范围内选用换算规则。

规则区间采用半开区间 [生效起点, 生效终点)，生效终点为空表示长期有效。
两条相邻规则可以存在获准的生效交叠区：
- 交叠区之外各自仍是独占区间，命中唯一获准规则即可直接换算；
- 交叠区内若优先级不同，高优先级规则确定胜出；
- 交叠区内若优先级相同，引擎不擅自换算，保留唯一一条处置记录等待现场确认。

本模块只做纯逻辑判定，不依赖 store 与文件 IO，方便单测与跨服务复用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# 已获准（可参与换算）的规则状态。
APPROVED_STATUSES = ("已标定", "已生效")
# 交叠区同优先级时的两种结论
DECISION_EXCLUSIVE = "独占"
DECISION_OVERLAP_RESOLVED = "交叠-高优先级确定"
DECISION_OVERLAP_PENDING = "交叠-待现场确认"
DECISION_NO_RULE = "无可用规则"


def parse_ts(value: str | datetime) -> datetime:
    """解析 ISO 8601 时间字符串；无时区信息按 UTC 处理，保证可比较。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def rule_covers(rule: dict[str, Any], ts: datetime) -> bool:
    """判断采样时刻是否落在规则的生效范围内（半开区间）。"""
    start = parse_ts(rule["生效起点"])
    if ts < start:
        return False
    end_text = rule.get("生效终点")
    if end_text in (None, ""):
        return True
    return ts < parse_ts(end_text)


def is_approved(rule: dict[str, Any]) -> bool:
    return rule.get("status") in APPROVED_STATUSES


@dataclass
class RuleDecision:
    """单次规则选用结论。"""
    decision: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected: dict[str, Any] | None = None
    reason: str = ""

    @property
    def convertible(self) -> bool:
        return self.selected is not None and self.decision in (
            DECISION_EXCLUSIVE, DECISION_OVERLAP_RESOLVED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "candidate_rule_ids": [r["规则编号"] for r in self.candidates],
            "selected_rule_id": self.selected["规则编号"] if self.selected else None,
            "reason": self.reason,
        }


def candidate_rules(
    rules: Iterable[dict[str, Any]], point: str, ts: datetime
) -> list[dict[str, Any]]:
    """筛出同一测点、已获准且生效范围覆盖采样时刻的规则，按优先级降序排列。"""
    matched = [
        rule for rule in rules
        if rule.get("测点编号") == point
        and is_approved(rule)
        and rule_covers(rule, ts)
    ]
    return sorted(matched, key=lambda r: (-int(r.get("优先级", 0)), parse_ts(r["生效起点"])))


def select_rule(rules: Iterable[dict[str, Any]], point: str, ts: str | datetime) -> RuleDecision:
    """按测点和采样时间选用换算规则，给出独占/交叠/无规则结论。"""
    moment = parse_ts(ts)
    candidates = candidate_rules(rules, point, moment)
    if not candidates:
        return RuleDecision(
            DECISION_NO_RULE, candidates=[],
            reason=f"测点 {point} 在 {moment.isoformat()} 没有已获准且生效的标定规则")
    if len(candidates) == 1:
        return RuleDecision(
            DECISION_EXCLUSIVE, candidates=candidates, selected=candidates[0],
            reason="仅一条获准规则覆盖该时刻，属于该规则的独占区间")
    top_priority = int(candidates[0].get("优先级", 0))
    tied = [r for r in candidates if int(r.get("优先级", 0)) == top_priority]
    if len(tied) == 1:
        winner = tied[0]
        return RuleDecision(
            DECISION_OVERLAP_RESOLVED, candidates=candidates, selected=winner,
            reason=f"{len(candidates)} 条规则生效交叠，规则 {winner['规则编号']} 优先级最高")
    return RuleDecision(
        DECISION_OVERLAP_PENDING, candidates=candidates,
        reason=(f"{len(tied)} 条同优先级获准规则在该时刻交叠（"
                + "、".join(r["规则编号"] for r in tied) + "），等待现场确认后才能换算"))


def convert(rule: dict[str, Any], raw_value: float) -> float:
    """线性换算：标定值 = 原始值 × 换算系数 + 零点偏移。"""
    scale = float(rule.get("换算系数", 1.0))
    offset = float(rule.get("零点偏移", 0.0) or 0.0)
    return round(float(raw_value) * scale + offset, 6)


def interval_relation(
    rule_a: dict[str, Any], rule_b: dict[str, Any]
) -> dict[str, Any]:
    """计算两条规则生效区间的独占段与交叠段，用于相邻规则冲突核对。"""
    a_start = parse_ts(rule_a["生效起点"])
    b_start = parse_ts(rule_b["生效起点"])
    a_end = parse_ts(rule_a["生效终点"]) if rule_a.get("生效终点") else None
    b_end = parse_ts(rule_b["生效终点"]) if rule_b.get("生效终点") else None

    overlap_start = max(a_start, b_start)
    # 交叠段在最早的有限终点处结束；两条都开放时交叠段才开放。
    finite_ends = [e for e in (a_end, b_end) if e is not None]
    overlap_end = min(finite_ends) if finite_ends else None
    if overlap_end is not None and overlap_start >= overlap_end:
        overlap = None
    else:
        overlap = {"start": overlap_start, "end": overlap_end}

    def _part(start: datetime, end: datetime | None) -> dict[str, Any] | None:
        if end is not None and start >= end:
            return None
        return {"start": start, "end": end}

    if overlap:
        # 每条规则的独占段 = 自身区间减去交叠段（前缀或后缀，取存在的一侧）
        exclusive_a = _part(a_start, overlap_start) or _part(overlap_end, a_end)
        exclusive_b = _part(b_start, overlap_start) or _part(overlap_end, b_end)
    else:
        exclusive_a = _part(a_start, a_end)
        exclusive_b = _part(b_start, b_end)
    return {
        "rule_a": rule_a["规则编号"],
        "rule_b": rule_b["规则编号"],
        "overlap": overlap,
        "exclusive_a": exclusive_a,
        "exclusive_b": exclusive_b,
    }
