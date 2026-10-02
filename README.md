# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`GET/POST /api/operators`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 联合会签

- 创建事件时用 `operating_organizations` 列出相关运营方，并用 `object_operators`（物体ID→运营方）声明两个物体的归属；涉及的每家运营方都是会签方。
- 运营方用户先经 `POST /api/operators`（coordinator/analyst）登记 `{operator_user_id, organization}`。`record_opinion` 只允许代表本人所属机构，且该机构名下必须有本事件物体；越权返回 403 `operator_overreach` 并追加 `action_denied` 审计，不改变业务状态。
- 每家运营方各自 `record_opinion`（approve / reject / request_review），意见追加留痕、带序号与提交时间，序号最大者为该机构当前意见。coordinator `approve` 时必须**全部签批方最新意见为 approve**：缺表态返回 409 `signoff_incomplete`，有反对或要求复核返回 409 `signoff_blocked`，错误消息写明卡住的机构。
- 状态：`pending → assessed → coordinating（已会签批准）→ executing（指令已发）→ resolved`；批准后任何一家改动意见（与本人上一条不同）会让当前会签失效并进入 `review`，`prepare_command` 配置但尚未发出的指令移入 `returned_commands`，重新会签批准后轮次（round）递增；重复提交相同意见只算重申，不翻结论。
- 指令一旦经 `execute` 发出，会签记录锁定（`record_locked`），后到的反对不再受理，原会签与指令记录保留。
- 并发意见在单个数据库事务内串行落库，按提交先后分配序号；晚到的意见决定最终卡点。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、联合会签门槛与卡点提示、意见改动导致批准失效与指令退回、指令发出后记录锁定、并发意见顺序、越权拒绝审计、权限、版本冲突、过期轨道。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
