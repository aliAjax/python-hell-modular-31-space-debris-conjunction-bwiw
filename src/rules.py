from .domain import DomainError

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst"}
SOURCE_ROLES = {"analyst", "operator"}
ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "prepare_command": {"operator"},
    "execute": {"operator"},
    "resolve": {"coordinator"},
    "cancel": {"coordinator"},
    "report_revision": {"analyst"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"approve", "execute", "resolve", "cancel"}

OPINION_APPROVE = "approve"
OPINION_REJECT = "reject"
OPINION_REVIEW = "request_review"
VALID_OPINIONS = {OPINION_APPROVE, OPINION_REJECT, OPINION_REVIEW}
BLOCKING_OPINIONS = {OPINION_REJECT, OPINION_REVIEW}

# 批准后运营方改意见（且与本人上一条不同）时的状态回退
REVIEW_STATUS = "review"


def assess(payload):
    ratio = float(payload.get("miss_distance_m", 0)) / max(float(payload.get("covariance_m", 1)), 1.0)
    tca_hours = float(payload.get("hours_to_tca", 24))
    severity = max(0.0, 100.0 - min(95.0, ratio * 20.0))
    urgency = max(0.0, min(20.0, (24.0 - tca_hours) * 0.8))
    score = round(min(100.0, severity + urgency), 2)
    if score >= 80:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "distance_to_covariance_ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _require_number(payload, name, minimum=None):
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError):
        raise DomainError("field_required", "%s 不能为空" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def _require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def signatories(current):
    """联合会签方：事件涉及的每家运营方（按机构名去重排序）。"""
    mapping = current.get("object_operators") or {}
    names = set(mapping.values()) | set(current.get("operating_organizations", []))
    return sorted(names)


def latest_opinions(current, exclude_seq=None):
    """每家运营方当前生效（序号最大）的一条意见。"""
    result = {}
    for entry in current.get("opinions", []):
        if exclude_seq is not None and entry.get("seq") == exclude_seq:
            continue
        result[entry["operator"]] = entry
    return result


def signoff_summary(current):
    """会签视图：每家签批方的最新表态、缺口和卡点。"""
    latest = latest_opinions(current)
    rows = []
    missing = []
    blockers = []
    for name in signatories(current):
        entry = latest.get(name)
        if entry is None:
            missing.append(name)
            rows.append({"operator": name, "opinion": None, "seq": None})
        else:
            rows.append({"operator": name, "opinion": entry["opinion"], "seq": entry["seq"]})
            if entry["opinion"] in BLOCKING_OPINIONS:
                blockers.append("%s:%s" % (name, entry["opinion"]))
    return {"signatories": rows, "missing": missing, "blockers": blockers}


def _void_current_countersign(current, reason, changed_entry):
    """让当前会签批准连带失效；已配置未发出的指令退回重议。"""
    current["conflict"] = True
    signoff = current.get("countersign")
    history = current.setdefault("countersign_history", [])
    if signoff:
        voided = dict(signoff)
        voided["voided_by"] = changed_entry
        voided["voided_reason"] = reason
        history.append(voided)
    pending = current.get("pending_command")
    returned = current.setdefault("returned_commands", [])
    if pending:
        pending["returned_reason"] = reason
        pending["returned_by"] = changed_entry
        returned.append(pending)
        current["pending_command"] = None
    current["countersign"] = None


def apply_action(item, action, payload, actor, role):
    """纯规则：基于事务内最新 item 计算 (new_status, new_payload, event_payload)。"""
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed", "review"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = assess(current)
        current["assessment"] = result
        return "assessed", current, {"assessment": result, "actor": actor}

    if action == "report_revision":
        _need_status(item, {"pending", "assessed", "coordinating", "review", "executing"})
        revision = {
            "observed_at": _require_text(payload, "observed_at"),
            "miss_distance_m": _require_number(payload, "miss_distance_m", 0),
            "covariance_m": _require_number(payload, "covariance_m", 0.001),
            "source": _require_text(payload, "source"),
        }
        if revision["covariance_m"] <= 0:
            raise DomainError("invalid_covariance", "协方差必须大于零")
        current.setdefault("revisions", []).append(revision)
        current["miss_distance_m"] = revision["miss_distance_m"]
        current["covariance_m"] = revision["covariance_m"]
        current["assessment"] = assess(current)
        return status, current, {"revision": revision}

    if action == "record_opinion":
        if status in {"executing", "resolved"}:
            raise DomainError(
                "record_locked",
                "规避指令已发出，会签记录锁定；改动意见不再受理，原会签记录保留",
                409,
            )
        _need_status(item, {"assessed", "coordinating", "review"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in VALID_OPINIONS:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        if operator not in signatories(current):
            # 正常情况下 service 层已经拦截；这里兜底，防止绕过身份校验
            raise DomainError("not_a_party", "%s 不是该事件的相关运营方" % operator, 403)
        entries = current.setdefault("opinions", [])
        seq = (max([entry.get("seq", 0) for entry in entries], default=0) + 1)
        entry = {
            "seq": seq,
            "operator": operator,
            "opinion": opinion,
            "reason": payload.get("reason", ""),
            "submitted_by": actor,
            "submitted_at": payload.get("submitted_at"),
        }
        entries.append(entry)

        previous = latest_opinions(current, exclude_seq=seq).get(operator)
        # 同一机构重复提交相同意见：视为重申，不改变任何结论
        changed = previous is None or previous["opinion"] != opinion

        if status == "coordinating":
            if not changed:
                return status, current, {"opinion": entry, "effect": "restated"}
            # 会签批准之后改动意见：原批准连带失效，未发指令退回重议
            _void_current_countersign(current, "operator_opinion_changed", entry)
            new_status = REVIEW_STATUS if opinion in BLOCKING_OPINIONS else "assessed"
            return new_status, current, {
                "opinion": entry,
                "effect": "countersign_voided",
                "returned_to": new_status,
            }

        # assessed / review 阶段：意见只决定会签视图，不自动批准
        summary = signoff_summary(current)
        current["conflict"] = bool(summary["blockers"])
        return status, current, {"opinion": entry, "effect": "recorded", "signoff": summary}

    if action == "approve":
        _need_status(item, {"assessed", "review"})
        summary = signoff_summary(current)
        if summary["missing"]:
            raise DomainError(
                "signoff_incomplete",
                "联合会签未完成，缺少运营方表态: %s" % "、".join(summary["missing"]),
                409,
            )
        if summary["blockers"]:
            raise DomainError(
                "signoff_blocked",
                "联合会签未通过，卡住的运营方: %s" % "、".join(summary["blockers"]),
                409,
            )
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        budget = float(current.get("fuel_budget_m_s", 0))
        if fuel > budget:
            raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
        window = _require_text(payload, "maneuver_window")
        history = current.setdefault("countersign_history", [])
        prior_rounds = [int(signoff["round"]) for signoff in history if signoff.get("round")]
        if current.get("countersign"):
            prior_rounds.append(int(current["countersign"]["round"]))
        round_no = (max(prior_rounds) + 1) if prior_rounds else 1
        countersign = {
            "round": round_no,
            "fuel_cost_m_s": fuel,
            "maneuver_window": window,
            "approved_by": actor,
            "opinions": [dict(row) for row in summary["signatories"]],
        }
        if current.get("countersign"):
            history.append(current["countersign"])
        current["countersign"] = countersign
        current["conflict"] = False
        return "coordinating", current, {"countersign": countersign}

    if action == "prepare_command":
        _need_status(item, {"coordinating"})
        if not current.get("countersign"):
            raise DomainError("countersign_voided", "会签批准已失效，指令不能配置", 409)
        if current.get("pending_command"):
            raise DomainError("command_already_prepared", "已有一条配置好待发出的指令", 409)
        command_ref = _require_text(payload, "command_ref")
        command = {"command_ref": command_ref, "prepared_by": actor, "round": current["countersign"]["round"]}
        current["pending_command"] = command
        return "coordinating", current, {"pending_command": command}

    if action == "execute":
        _need_status(item, {"coordinating"})
        signoff = current.get("countersign")
        if not signoff:
            raise DomainError(
                "countersign_voided",
                "会签批准已失效，指令退回重议，不能发出",
                409,
            )
        pending = current.get("pending_command")
        if pending:
            command_ref = pending["command_ref"]
        else:
            command_ref = _require_text(payload, "command_ref")
        current["pending_command"] = None
        current["command_ref"] = command_ref
        current["issued_round"] = signoff["round"]
        return "executing", current, {"command_ref": command_ref, "round": signoff["round"]}

    if action == "resolve":
        _need_status(item, {"executing"})
        report_ref = _require_text(payload, "report_ref")
        current["resolution"] = {"report_ref": report_ref, "resolved_by": actor}
        return "resolved", current, {"report_ref": report_ref}

    if action == "cancel":
        _need_status(item, {"pending", "assessed", "review"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
