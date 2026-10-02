from . import domain, rules
from .domain import DomainError

REGISTER_OPERATOR_ROLES = {"coordinator", "analyst"}


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

    def register_operator(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in REGISTER_OPERATOR_ROLES:
            raise DomainError("forbidden", "当前角色不能登记运营方用户", 403)
        operator_user_id = domain.require_text(payload, "operator_user_id")
        organization = domain.require_text(payload, "organization")
        return self.repository.register_operator(operator_user_id, organization, actor, role)

    def _authorize_opinion(self, item, payload, actor):
        """运营方只能代表自己名下物体对应的机构表态。"""
        operator = payload.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise DomainError("field_required", "operator 不能为空")
        operator = operator.strip()
        mapping = item["payload"].get("object_operators", {})
        object_names = {item["payload"].get("primary_object_id"), item["payload"].get("secondary_object_id")}
        owned_objects = [obj for obj in object_names if mapping.get(obj) == operator]
        if not owned_objects:
            self.repository.record_denial(
                item["id"],
                "operator_overreach",
                actor,
                "operator",
                {"action": "record_opinion", "claimed_operator": operator,
                 "owned_objects_in_event": []},
            )
            raise DomainError(
                "operator_overreach",
                "运营方 %s 在该接近事件中名下没有物体，不能代表相关运营方表态" % operator,
                403,
            )
        registered_org = self.repository.get_operator_org(actor)
        if registered_org is None:
            self.repository.record_denial(
                item["id"],
                "operator_overreach",
                actor,
                "operator",
                {"action": "record_opinion", "claimed_operator": operator,
                 "registered_organization": None, "owned_objects_in_event": owned_objects},
            )
            raise DomainError(
                "operator_overreach",
                "用户 %s 未登记为任何运营方，不能提交会签意见" % actor,
                403,
            )
        if registered_org != operator:
            self.repository.record_denial(
                item["id"],
                "operator_overreach",
                actor,
                "operator",
                {"action": "record_opinion", "claimed_operator": operator,
                 "registered_organization": registered_org, "owned_objects_in_event": owned_objects},
            )
            raise DomainError(
                "operator_overreach",
                "用户 %s 属于 %s，不能代表 %s 表态" % (actor, registered_org, operator),
                403,
            )
        payload["operator"] = operator

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
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        if action == "record_opinion":
            self._authorize_opinion(item, payload, actor)

        def mutator(latest_item, submitted_at):
            action_payload = dict(payload)
            if action == "record_opinion":
                action_payload["submitted_at"] = submitted_at
            return rules.apply_action(latest_item, action, action_payload, actor, role)

        self.repository.apply_action(item_id, action, actor, role, mutator, expected_version)
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["signoff"] = rules.signoff_summary(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
