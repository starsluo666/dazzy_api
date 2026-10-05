# dazzy_api

DAZZY Django 5.2 API。

生产环境使用 Docker Compose：参见 [Docker 生产部署说明](docs/docker-production.md)。
项目根目录的 `start-dazzy-api.sh` 仍用于本地开发；生产服务由 `deploy-docker.sh` 管理。

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

Beat 每 10 秒派发一次到期任务处理，每 3 分钟分页补建一次遗漏任务；达人订单的
支付、接单、确认和结算任务，以及活动的报名支付、成局、开始、结束和结算任务均
进入同一任务中心。也可以用 `python manage.py process_scheduled_tasks` 手动补建并处理
一批。兼容命令 `python manage.py expire_provider_orders` 只处理达人订单支付超时任务，
`python manage.py process_activity_timeouts` 保留为活动任务的应急直接扫描命令，生产
环境不再单独周期调度这两个兼容命令。

生产环境必须把 API、Worker、Beat 作为独立进程托管并配置自动重启。Beat 同一套
调度只能运行 1 个实例，Worker 可以运行多个实例；任务真实状态保存在 PostgreSQL，
Redis 只承担 Celery 消息传递。

生产监控至少需要覆盖 Worker/Beat 存活、失败任务数、逾期待执行任务数和最老任务
延迟；出现失败任务、连续 5 分钟存在逾期任务或进程失联时应触发告警。

存活检查：`GET /api/v1/health/`；就绪检查：`GET /api/v1/health/ready/`；OpenAPI：`GET /api/schema/`。

## 达人身份证照片水印

`POST /api/v1/media/provider-identities/` 接收 `file` 和可选 `kind`：
`identity_front_photo` / `identity_back_photo` 会在服务端添加“仅用于达人验证”浅色斜向水印；
`identity_face_photo` 保持原样。旧版客户端不传 `kind` 时，默认加水印（包括旧版上传的核验照）。
仅影响新上传文件，历史照片不回写；生活照、头像和履约照片不受影响。

身份证图片在上传私有 COS 前处理，只存带水印的 JPEG，不额外保存无水印原件；
保留图片尺寸并校正手机 EXIF 旋转，移除 EXIF/GPS 等元数据。审核预览使用同一私有文件，
不改变既有审核权限或私有签名 URL 有效期。水印不替代权限控制，也不能保证无法移除。

本次部署需重新构建 API Docker 镜像，其中已加入 `fonts-wqy-microhei` 中文字体；
非 Docker 部署可安装该字体，或将 `PROVIDER_IDENTITY_WATERMARK_FONT_PATH` 指向支持中文的
TTF/TTC/OTF 字体。Windows 开发环境可使用系统微软雅黑，不分发该字体。字体缺失时接口返回
503 并拒绝存储原图，不静默跳过水印。无需数据库迁移；同步发布达人端以区分核验照。

离线回归（不会连接业务数据库或 COS）：

```bash
uv run python scripts/check_wechat_auth.py mediafiles.tests mediafiles.test_identity_watermark providers.tests.ProviderSelfManagementTests
```
