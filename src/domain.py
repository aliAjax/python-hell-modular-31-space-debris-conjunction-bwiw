from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def positive_integer(payload, name):
    value = payload.get(name, 0)
    if isinstance(value, bool):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    if value < 0:
        raise DomainError("invalid_integer", "%s 不能为负数" % name)
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    primary = require_text(payload, "primary_object_id")
    secondary = require_text(payload, "secondary_object_id")
    if primary == secondary:
        raise DomainError("same_object", "接近事件的两个物体不能相同")
    tca = parse_timestamp(payload, "tca")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    fuel_budget = number(payload, "fuel_budget_m_s", 0)
    track_age = number(payload, "track_age_hours", 0)
    operators = payload.get("operating_organizations", [])
    if not isinstance(operators, list) or any(not isinstance(item, str) or not item.strip() for item in operators):
        raise DomainError("invalid_operators", "运营方必须是字符串列表")
    operators = [item.strip() for item in operators]
    object_operators = payload.get("object_operators")
    if object_operators is None:
        if len(set(operators)) == 1:
            object_operators = {primary: operators[0], secondary: operators[0]}
        elif len(operators) == 2:
            object_operators = {primary: operators[0], secondary: operators[1]}
        else:
            raise DomainError(
                "invalid_object_operators",
                "涉及两家以上运营方时必须提供 object_operators 物体归属表",
            )
    if not isinstance(object_operators, dict) or any(
        not isinstance(key, str) or not key.strip()
        or not isinstance(value, str) or not value.strip()
        for key, value in object_operators.items()
    ):
        raise DomainError("invalid_object_operators", "object_operators 必须是 物体ID->运营方 的字符串映射")
    object_operators = {key.strip(): value.strip() for key, value in object_operators.items()}
    if set(object_operators.keys()) != {primary, secondary}:
        raise DomainError("invalid_object_operators", "object_operators 必须且只能声明接近事件的两个物体")
    if set(object_operators.values()) != set(operators):
        raise DomainError("invalid_object_operators", "物体归属的运营方与 operating_organizations 名单不一致")
    stable_key = "%s|%s|%s" % tuple(sorted([primary, secondary]) + [tca])
    return {
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "fuel_budget_m_s": fuel_budget,
        "track_age_hours": track_age,
        "operating_organizations": operators,
        "object_operators": object_operators,
        "revisions": [],
        "opinions": [],
        "countersign": None,
        "countersign_history": [],
        "pending_command": None,
        "returned_commands": [],
        "conflict": False,
        "_stable_key": stable_key,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "region": region,
        "operator": payload.get("operator"),
    }
