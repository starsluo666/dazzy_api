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

## 运营待办与站内提醒（第一期）

运营总览和顶部通知铃铛使用同一待办口径，按当前管理员的业务处理权限、城市范围筛选。
包括达人入驻/开通/资料变更/服务变更审核、活动发布审核、履约异常、历史超时待核查、
转客服订单、达人订单售后、活动售后、客服投诉与举报、活动举报、达人订单及活动报名退款异常。
仅登记“已联系用户”不代表协调完成；转客服订单仍需完成业务处理才移出待办。
已取消/已退款订单不再作为履约异常待核查；退款失败仍可单独进入退款异常队列。

退款异常包含退款失败、对应退款任务失败，以及 pending/processing 超过 24 小时的退款。
此处只提示核查，不调用支付渠道，不重建退款单，不改变原有退款、分账或信用扣分规则。
第一期未包含的分账/提现本地核账与关键任务告警，现由下文第二期补充；活动发布费退款仍在原业务页面核查。

待办直接查询尚未处理完成的业务记录，不复制一套业务状态。总数按事项计算，并非去重订单数。
已读记录按管理员、事项、事件版本保存：标为已读不会关闭待办；处理完成后自动移出；
新履约异常、用户补充回复/申请复核、退款重试再次失败、事项首次进入超时提醒时会重新变为未读。
“去处理”携带分类及记录标识，后端再次校验权限与城市；列表顶部可退出待办筛选。
通知抽屉不是永久通知档案，已完成事项请到原业务记录及审计日志查询。

初始提醒阈值为：紧急异常、订单协调与历史超时核查 1 小时；售后、投诉及举报 4 小时；
审核 24 小时。等待时间按对应业务节点计算；这些是运营提醒阈值，不是履约时间容差或退款承诺，
目前集中在 `backoffice/operations_queue.py`，不支持管理后台调整。

后台打开且页面可见时每 30 秒刷新，切回页面也会刷新。新提醒按事件去重，聚合弹出一次，
每类摘要携带至多 5 条最新未读信号；完整数量不截断，抽屉每页 20 条。
已读状态保存在服务端；弹出提醒的去重记录按账号存于当前浏览器标签页的 sessionStorage。
离开后台后不会推送手机消息；尚未接入短信、微信订阅或桌面系统推送。
履约时间异常及客服核查通过时，另向用户、达人发送各自的站内消息，不包含内部审核备注。

接口：`GET /api/v1/admin/work/summary/`、`GET /api/v1/admin/work/items/?queue=...`、
`POST /api/v1/admin/work/items/`（传 `queue`、`object_id`、`event_version` 标为已读）。
不要求 `dashboard.view`，但只展示当前账号有权处理的业务队列，不可越权读取或标记。

部署先执行 `backoffice.0034_admin_work_read_receipt` 数据库迁移，再更新 API 与管理后台；
Worker 同步新代码以发送履约异常站内消息，Beat 保持正常运行以产生原有超时检查任务。
部署后验证不同权限/城市账号、已读与处理分离、待办跳转、新异常重新提醒以及处理后移除。

离线后端回归（临时内存库，不连接业务库）：

```bash
uv run python scripts/check_wechat_auth.py backoffice.test_operations_queue backoffice.tests orders.test_fulfillment orders.test_timeouts notifications.tests supportcases.tests
```

后台运行 `npm run test:operations-work` 与 `npm run build`。启动本地后台后，准备 Playwright /
Chromium，再运行 `npm run test:operations-work:browser`；浏览器用例使用模拟接口，不连接业务 API。
SQLite 会明确跳过 4 项订单超时 PostgreSQL 并发测试，上线前仍需在获授权的隔离 PostgreSQL 环境验证。

## 资金与任务告警、外部通知扩展（第二期）

后台新增「财务管理 → 资金异常核查」，与运营总览、通知铃铛共用权限、城市范围和待办条件。
要求同时具有 `order.finance.view`、`order.finance.manage`；页面只读取本地记录，不发起渠道查询，
不修改业务终态、余额、提现冻结，不提供补账、重发分账、重复提现或解除冻结按钮。
通知已读与业务处理仍然分离，重复查询相同结果不会持续弹新提醒。

| 待办 | 进入条件 | 不进入的正常情况 |
| --- | --- | --- |
| 分账结果待核查 | failed/unknown、证据冲突或已有核查标记；提交/处理中超过 30 分钟 | 正常新提交、无冲突的成功结果 |
| 分账前置核验异常 | blocked；或同类预检查失败至少 3 次（不含 configuration/local_conditions） | 开关/白名单未满足、业务冻结期或其他本地条件未满足、已经登记渠道请求 |
| 分账成功未入余额 | manual_cash_v1 成功分账、正数达人金额、没有 credit 流水且记录超过 5 分钟未更新 | 历史自动结算、0 元收入、已有入账、仍有证据冲突 |
| 提现结果及流水待核查 | unknown/attention、缺少匹配预留或终态流水；提交中超过 30 分钟；银行处理中超过 72 小时 | 正常银行处理中；失败且已经核实退回余额、流水齐全 |
| 达人余额核账 | 账户处于 hold，或可用/预留/累计提现汇总与本地收入流水合计不符 | 本地汇总一致且没有 hold |
| 关键任务失败或超时 | 任务中心关键任务失败、可执行时间过期 5 分钟仍 pending、执行超过 5 分钟；排除出发提醒和默认好评 | 尚未到重试时间、成功、取消、正常执行中 |

这些阈值是提示人工核查的观察窗口，不是汇付到账承诺，也不据此判定交易失败。
银行处理中按自然小时提醒，不推算渠道节假日到账时间。金额展示优先使用不可变分账请求快照；
不使用当前费率重新计算历史金额。资金告警按记录计数，同一异常可能涉及分账与账户冻结两个事项。
前置核验的等待时间从最近检查节点计算，余额核账从最近账户更新节点计算。

`GET /api/v1/admin/work/finance/?queue=...&page=1` 提供分页核查详情，可选 `work_id` 精确定位。
只返回必要的业务流水号、达人名称、金额、状态和安全核查提示；不返回请求快照、完整卡号、
开户身份信息、卡 token、接收方渠道账号、原始渠道响应或密钥。
任务中心沿用原有查看/重试权限与城市约束；告警跳转只设置筛选，不自动重试任务。

当前核账范围是**本地记录及已保存的渠道结果**，不等同于完整的汇付账单/银行账单持续对账。
尚未落库的渠道差异、长周期银行退汇、Worker/Beat 全部离线但尚无到期任务等，需要现有运维监控及
人工渠道对账补充。此次不新增 Worker/Beat 进程心跳或外部告警服务，也不修改资金定时任务的执行逻辑。
证据冲突及账户 hold 不会因为一次后续正常查询或“标为已读”自动解除；核账仍由财务走既有受控流程。

### 短信与微信：只预留接口

`GET /api/v1/admin/work/channels/` 返回后台通知可用、sms/wechat 为 `reserved` 且 `enabled=false`。
该接口只读，没有启用或发送接口；未配置凭据、联系人、模板或任何外部推送任务。
`notifications/external_channels.py` 定义 `NotificationEnvelope`、`NotificationChannel.send()` 和
`DeliveryResult`；两个保留适配器无论调用多少次都返回 `not_configured`，不会产生发送回执或网络请求。
这与汇付支付结果回调 `notify_url` 无关，不得混用。

后续接通需单独确认通知对象、授权/订阅、模板与频率，增加事务后 outbox、权限/接收人再校验、
按「事件版本 + 接收人 + 渠道」的幂等去重、失败重试及真实送达回执。
禁止从 GET、财务数据库事务或前端直接发送，不得将支付请求、敏感快照、身份或银行卡信息放入通知。

### 验证与发布

沿用第一期 `backoffice.0034` 已读表迁移，第二期不新增资金表或资金字段。
发布 API 与后台前端；如第一期尚未发布，应同时更新 Worker 以包含第一期站内消息逻辑。
保持原有 Worker/Beat 与资金任务开关正常配置，不因发布告警功能自动开启分账或提现。

```bash
uv run python scripts/check_wechat_auth.py backoffice.test_operations_queue backoffice.test_finance_work backoffice.tests orders.test_fulfillment orders.test_timeouts orders.test_distribution_safety orders.test_distributions providers.test_withdrawals taskcenter.tests notifications.tests supportcases.tests
```

后台运行 `npm run test:operations-work`、`npm run test:operations-work:browser`、
`npm run test:finance-work:browser`、`npm run build`。浏览器接口全部模拟，支持通过
`PLAYWRIGHT_MODULE`、`CHROME_EXECUTABLE`、`ADMIN_TEST_URL` 指定本地运行环境。
先用隔离测试数据验收：不同城市/角色、无权精确跳转、正常等待不误报、告警已读后仍可核查、
重复查询不重复提醒、真实业务处理后移出；不要在生产手改余额、状态或流水来制造测试异常。

本次按 `huifu-pay-integration` 检查存量 Python/Django 聚合支付服务的增量告警边界，参考
`copilot-existing-system`、`copilot-solution-selection`、`shared-async-notify`、
`copilot-troubleshooting-playbooks` 四份规范。现有官方 Python SDK、请求/验签/查单补偿、
幂等键、终态保护及账务更新责任均保留；本次不调用渠道、不修改传输或调试日志配置。
真实到账、长期对账及隔离 PostgreSQL 并发仍需人工/授权环境验收，离线测试不证明渠道生产可用。

## 客服人工退款：登记、审批及主管复核

后台「订单管理 → 履约详情」增加登记退款；活动详情的报名用户，以及活动账务的参与支付记录，
可按报名支付单登记退款。弹窗读取服务端实付、已退款、仍占用和剩余可退金额，必须填写原因并二次确认。
登记只创建售后申请，不发起渠道退款；沿用售后冻结自动确认/结算流程，人工退款本身不扣达人信用分。
本次仅覆盖达人订单和活动参与报名，不新增活动发布费退款或整场活动取消入口。

### 发布与授权

先执行 `activities.0016_staff_refund_review`、`backoffice.0035_staff_refund_controls` 迁移，再发布 API、后台和 Worker。
迁移不自动授予现有角色新权限，两个额度默认均为 0；超级管理员及已有通配权限角色仍具有全部权限。
`grant_platform_admin` 是管理员显式执行的授权命令，本次未运行；执行它会给目标平台管理员包括下列新权限。

| 操作 | 必需权限 |
| --- | --- |
| 登记达人订单售后 | `order.after_sales.view` + `order.after_sales.create` |
| 登记活动报名退款 | `activity_finance.view` + `activity_after_sales.create` |
| 领取、驳回、转主管 | 对应业务原售后处理权限；已转主管的驳回还须 `refund.supervise` |
| 限额内批准退款 | 对应业务原售后处理权限 + `refund.approve` |
| 主管审批 | 对应业务原售后处理权限 + `refund.supervise`；城市数据范围不放宽 |
| 核实后重试原退款 | 原退款重试入口权限 + `refund.retry`；任务中心退款重试同样要求该权限 |

运营配置的「平台运营参数 → 客服退款审批额度」以元填写，服务端以整数分保存。
`support_refund_single_limit` 是同一订单累计退款上限，不是本次申请上限；包含其他审批人及历史退款，不能拆申请绕过。
`support_refund_daily_limit` 是每名审批人按北京时间自然日的两类退款合计，包含已生成但未成功及失败待重试的退款。
额度不因失败、重试或跨页面处理而释放，重试沿用原退款单，不新增占用。
任一额度为 0、超额或已经转主管时，普通审批只保留申请并标记待主管，不创建退款单。
登记人员不自动获得批准权限；额度配置也不自动赋权。角色配置应分别授予登记、审批和重试权限。

审批与退款记录创建在同一事务内；订单/支付行锁保护可退金额，审批人行锁串行化两类订单的每日额度判断。
审批时再次校验金额和资金状态，界面预览不是授权依据。重复批准同一申请不会生成第二笔退款。
转主管、登记、批准/驳回与原退款重试均有审计；超额记录保留拟批准金额、核实说明及当时额度。
主管待办按城市、查看和处理权限过滤，进入实时待办与站内铃铛；不新增微信或短信发送。

### 保留的业务与资金边界

- 达人订单部分退款仍依次分配服务费、其他费、路费；全额包含剩余路费，不按新比例重算历史订单。
- 活动报名可分别核准 AA 本金与平台服务费，批准后会取消该用户报名，**部分退款也取消报名**，不会取消整场活动。
- 已结算、已进入渠道分账（包括失败/待核实记录）不能从本入口退款；主管不绕过该保护。分账回退仍须单独实现和核验。
- 存在处理中、失败待核实的原退款时不创建新退款；应核查并使用原单恢复入口。
- 原路退款继续使用原有余额/渠道分配、官方 SDK、请求流水、通知验签、查询补偿和成功后的账务更新。
  审批通过或请求受理不等于资金已退回，不在前端修改成功状态。
- 此额度只控制新人工售后审批；原有用户取消、系统超时退款、整场活动取消仍执行各自业务规则和权限。
  不要为普通客服额外授予整场活动取消等管理权限来替代售后审批。

### 回归与上线验收

```bash
uv run python scripts/check_wechat_auth.py backoffice.test_staff_refunds backoffice.test_staff_refund_concurrency backoffice.tests backoffice.test_operations_queue backoffice.test_finance_work activities.tests.ActivityModelTests
```

此内存库命令验证迁移、权限、金额、重复请求和待办；明确跳过 3 项新增 PostgreSQL 行锁并发用例。
`activities.tests.ActivityRefundConcurrencyTests` 也必须在隔离 PostgreSQL 上运行，不在内存 SQLite 上验证。
用 `scripts/check_postgres_regression.py` 及经本次授权的隔离测试连接，补跑
`backoffice.test_staff_refund_concurrency`、`activities.tests.ActivityRefundConcurrencyTests`、
`providers.test_income_concurrency`；该脚本要求创建专用临时库，不能对业务库迁移或清空。
后台运行 `npm run build` 与 `npm run test:staff-refunds:browser`，后者所有接口均模拟；
现有 `test:operations-work:browser`、`test:finance-work:browser` 同时回归。
部署后先保持普通审批额度为 0，核对角色及主管接单人，再用获授权测试账号完成退款结果、通知及账务验收。

本次为存量 Python/Django 汇付聚合支付系统的本地权限/界面增量，未新增或更改汇付接口合同。
按 `huifu-pay-integration` 的 `copilot-existing-system`、`copilot-solution-selection`、
`shared-async-notify` 三份参考检查：保留请求、SDK 传输和调试配置、通知验签、幂等和终态责任；
没有真实渠道调用或密钥写入。界面依照 `emil-design-eng` 使用清晰金额分组、权限可见性和二次确认。
真实渠道退款、隔离 PostgreSQL 并发、分账后退款回退不因离线回归通过而视为验收完成。
