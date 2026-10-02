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

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `POST /api/operators/members`（协调员登记运营方成员）。身份使用 `X-User-Id`、`X-Role` 请求头。

规避批准采用联合会签：接近事件涉及的每家运营方各自表态（`approve`/`reject`/`request_review`），全部同意才批准；出现反对或要求复核即退回，并写明是哪家运营方卡住。运营方只能代表自己名下的物体表态，越权表态会被拒绝并写入审计。批准做出后若有运营方改动意见，先前批准连带失效：尚未发出的指令退回重议，指令已发出则保留原记录。意见并发提交时，以提交时间靠后的一条为准（乐观版本冲突后重试）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道和运营方意见冲突。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
