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

## 已接单但未出发的超时处理

新增订单在下单时固定时间规则与信用扣分规则，后台“平台运营参数 → 达人订单”可配置
“接单后未出发宽限”（默认 30 分钟、0–180）及“超时未出发扣分”（默认 2 分、0–100）。
该宽限独立于开始/完成的履约容差。出发截止 = 预约开始 + 宽限；到达截止时仍未出发、
无到场或服务记录的已付款已接单订单，取消并发起剩余实付金额退款（包含路费、扣除优惠），
每单只扣一次信用分，最低扣至 0。点击联系不会延长时间，页面也不能绕过服务器截止校验。
下单后修改后台参数只影响后续新订单；本次不新增改期功能。

已出发但超过开始容差仍未开始、服务中超过结束容差仍未提交完成：转履约异常，
暂停自动确认和分账，客服核查，不自动退款扣分。恰好处于开始/完成容差边界不标异常。
已有争议、客服介入、未完成退款或分账/结算记录的未出发订单也转人工核实。
客服审核过的同一闲置超时记录不重复标记；后续实际开始/完成时间仍独立校验。
已转人工处理的未出发超时不因复核而重新触发自动退款；请使用已有售后流程作出处理决定。

接单后注册站内提醒（预约开始前 30 分钟；晚接单但未截止时尽快提醒）、未出发检查；
出发后注册未开始检查，开始后注册未完成检查。复用每 10 秒的到期任务处理及每 3 分钟
的遗漏任务补建，不承诺恰在截止秒到账。Worker/Beat 必须正常运行。任务重试与用户出发、
退款申请使用订单行锁；信用流水按“订单 + 系统事件”唯一，申诉也只能撤销一次。

本功能是现有 Python/Django + 汇付聚合支付系统的订单触发层增量；依照
`huifu-pay-integration` 的已有系统接入、方案选择和异步通知规则，沿用
`create_provider_order_refund`、退款任务、现有验签与查单，不改 SDK、支付通道配置或密钥。
余额与外部支付按原支付分配退回；渠道受理不等于到账。订单先显示取消、退款处理中，
核验成功后才显示已退款。退款失败或查单任务耗尽后显示待核实，禁止另建退款单重退。
全额退款成功后按既有逻辑释放优惠券，券有效期不自动延长；0 元单关闭并释放优惠券，
不创建虚假的资金退款。不存在自动扣回已完成分账的新增能力。

部署需要执行数据库迁移，同时发布 API / Worker / Beat、管理后台及两端前端。
迁移**不回填**旧订单截止时间：在后台“履约订单 → 历史超时待核查”查看积压单，
筛选口径为旧待服务/已出发超过开始 30 分钟，或旧服务中超过结束 30 分钟，仅供人工核查，
不批量自动退款扣分。新订单可用“未出发超时取消”“退款异常待核实”筛选。
超时扣分申诉通过需同时具备 `order.fulfillment.review` 与 `provider.credit.adjust` 权限，
仍受城市数据范围约束，写入审计日志与信用流水；只恢复实际扣分（总分不超过 100），
不会复活订单、撤销退款或恢复分账。

离线回归（临时内存库、禁止真实 HTTP，不连接业务库）：

```bash
uv run python scripts/check_wechat_auth.py orders.test_timeouts orders.test_fulfillment taskcenter.tests orders.tests backoffice.tests
```

`orders.test_timeouts.OrderTimeoutConcurrencyTests` 的四个并发用例需要隔离 PostgreSQL，
SQLite 下明确跳过；已纳入 `scripts/check_postgres_regression.py` 默认测试清单。
上线前还需在授权临时 PostgreSQL 库验证出发/超时竞争、重复超时、人工退款竞争及重复申诉，
并以专用测试订单验证站内提醒、配置快照、列表状态、退款回调和实际到账。
不使用真实用户的历史积压单做自动化测试，也不要直接在生产运行测试命令。

## 达人身份证照片水印

`POST /api/v1/media/provider-identities/` 接收 `file` 和可选 `kind`：
`identity_front_photo` / `identity_back_photo` 会在服务端添加“仅用于达人验证”浅色斜向水印；
`identity_face_photo` 不加水印。旧版客户端不传 `kind` 时，默认加水印（包括旧版上传的核验照）。
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
uv run python scripts/check_wechat_auth.py mediafiles.tests mediafiles.test_identity_watermark mediafiles.test_heif_uploads providers.tests.ProviderSelfManagementTests
```

## 苹果手机 HEIC / HEIF 照片

图片上传接口按文件内容识别 JPG、PNG、WebP、HEIC/HEIF，不依赖客户端文件名或 MIME。
HEIC/HEIF 通过 `pillow-heif` 解码主照片并转为 JPEG；不把原始 HEIF 容器存到 COS，
避免上传后在后台或其他设备上无法预览。身份证正反面直接在解码后的照片上加水印，
只编码一次；本人核验照、生活照和履约照片转换格式但不加身份证水印。
处理时保留方向并移除 EXIF/GPS/XMP，普通照片保留其 ICC 色彩配置（如有）。原有文件大小、2500 万像素
及访问权限限制继续生效，转换结果也校验大小；损坏或无法解码的文件仍拒绝上传。

部署需要重新构建后端镜像（锁文件增加 HEIF 解码依赖），无需修改小程序选图流程或数据库。
非 Docker 环境执行 `uv sync --locked` 后重启 API；Python 3.12 的 Linux/Windows 使用预编译轮子。
仍需用真实 iPhone/微信版本做上线验收；自动化用例使用合成 HEIF，覆盖旋转、网格分块、
多图主照片、文件类型误报、损坏内容和水印流程，不包含真实证件资料。

## 全端媒体上传兼容

用户端头像、活动封面、入驻生活照、评价图片、售后/投诉/举报附件，以及达人认证/履约照片、
后台素材库均使用上述内容识别和 HEIF 转换流程。前端不根据手机上报的扩展名或 MIME 拒绝文件。

新上传的展示视频支持 MP4/MOV 容器内的 H.264/HEVC 等可解码视频，服务端使用 FFmpeg
统一生成 H.264（8-bit yuv420p）/AAC MP4，按方向缩放至最高 1080p、30fps，不放大低分辨率；
应用旋转信息，HDR 的 HLG/PQ 基础层转 SDR，移除位置等附加元数据。只有转码并核验成功的
结果才上传 COS；历史文件不自动重写。视频展示与业务审核流程不变。

运行依赖、限制、测试及真机验收见 [媒体上传兼容部署说明](docs/media-upload-compatibility.md)。
