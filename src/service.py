from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action == "record_opinion":
            self._authorize_opinion(item, payload, actor, role)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def _authorize_opinion(self, item, payload, actor, role):
        """运营方只能代表自己名下的物体表态；越权一律拒绝并留审计。"""
        operator = payload.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            return  # 具体校验交给 rules.apply_action
        operator = operator.strip()
        member = self.repository.get_operator_member(actor)
        if member is None:
            reason = "actor_not_member"
            message = "该用户未隶属于任何运营方，不能代表 %s 表态" % operator
        elif member != operator:
            reason = "operator_mismatch"
            message = "该用户隶属于 %s，不能代表 %s 表态" % (member, operator)
        elif operator not in item["payload"].get("operating_organizations", []):
            reason = "operator_not_involved"
            message = "运营方 %s 未参与本次接近事件，不能表态" % operator
        else:
            return
        self.repository.append_audit_event(
            item["id"],
            "opinion_rejected",
            actor,
            role,
            {"operator": operator, "reason": reason, "message": message},
        )
        raise DomainError("operator_mismatch", message, 403)

    def register_operator_member(self, operator, actor, actor_id, role):
        if not actor_id or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role != "coordinator":
            raise DomainError("forbidden", "只有协调员可以登记运营方成员", 403)
        if not isinstance(operator, str) or not operator.strip():
            raise DomainError("field_required", "operator 不能为空")
        if not isinstance(actor, str) or not actor.strip():
            raise DomainError("field_required", "actor 不能为空")
        operator = operator.strip()
        actor = actor.strip()
        self.repository.add_operator_member(operator, actor)
        return {"operator": operator, "actor": actor}

    def list_operator_members(self):
        return self.repository.list_operator_members()

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
