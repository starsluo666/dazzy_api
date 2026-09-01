# dazzy_api

DAZZY Django 5.2 API。

```bash
uv sync
cp .env.example .env
uv run --env-file .env python manage.py migrate
uv run --env-file .env python manage.py runserver
```

轻量任务中心使用 Celery Worker + Beat。开发环境另外启动两个进程：

```bash
uv run --env-file .env celery -A config worker --loglevel=info
uv run --env-file .env celery -A config beat --loglevel=info
```

Beat 每 10 秒派发一次到期任务处理，每 3 分钟分页补建一次遗漏任务；也可以用
`python manage.py process_scheduled_tasks` 手动补建并处理一批。兼容命令
`python manage.py expire_provider_orders` 只处理达人订单支付超时任务。

生产环境必须把 API、Worker、Beat 作为独立进程托管并配置自动重启。Beat 同一套
调度只能运行 1 个实例，Worker 可以运行多个实例；任务真实状态保存在 PostgreSQL，
Redis 只承担 Celery 消息传递。

生产监控至少需要覆盖 Worker/Beat 存活、失败任务数、逾期待执行任务数和最老任务
延迟；出现失败任务、连续 5 分钟存在逾期任务或进程失联时应触发告警。

存活检查：`GET /api/v1/health/`；就绪检查：`GET /api/v1/health/ready/`；OpenAPI：`GET /api/schema/`。
