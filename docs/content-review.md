# 公开内容审核上线说明

## 覆盖和边界

- 用户修改昵称、头像：新资料先送审，原资料继续展示，通过后替换。
- 达人入驻生活照、名称、简介、相册照片和视频、服务说明，以及活动文字和封面、用户订单评价：在提交时生成不可变审核快照；业务审核通过前须内容审核通过。
- **不发送身份证、自拍、资质、履约证据、客服附件**到本内容检测服务。身份验真仍使用原独立流程。
- 达人、活动、评价的原业务审核继续保留。机器通过不直接开通达人、发布活动或通过评价。
- 下架隐藏公开资料，阻止新预约/新报名；不取消订单、不改活动成局状态、不退款、不改分账。已报名人员和组织者仍可从活动详情处理履约/售后。
- 图片依然使用现有公开 COS 素材链路。此功能控制应用内发布/展示，不把已上传的公开对象变成私有对象，也不能撤回用户已缓存的链接。若要求“审核前对象 URL 也不可访问”，需另做隔离桶和发布复制流程。
- 已发布历史内容不自动批量扫描、不自动下架；后台可登记需要复核的现有内容。旧的待业务审核记录启用后也需要先登记并完成内容核查。

## 配置

先部署迁移、后台和用户端。推荐先启用人工审核验证流程：

```dotenv
CONTENT_REVIEW_ENABLED=true
TENCENT_CONTENT_REVIEW_ENABLED=false
CONTENT_REVIEW_DAILY_LIMIT=50
```

总开关默认开启；腾讯自动检测仍默认关闭。未配置腾讯时，新提交进入“待人工复核”，不会绕过审核直接发布。上线前请安排审核人员并确认“内容审核”菜单权限；API、worker、beat 必须使用同一组配置和版本。已有环境若显式设置了 `CONTENT_REVIEW_ENABLED=false`，仍须修改该环境变量为 `true`。

接入腾讯自动检测前，开通文字、图片、视频内容安全，配置独立最小权限密钥和真实审核策略：

```dotenv
CONTENT_REVIEW_ENABLED=true
TENCENT_CONTENT_REVIEW_ENABLED=true
TENCENT_CONTENT_REVIEW_SECRET_ID=
TENCENT_CONTENT_REVIEW_SECRET_KEY=
TENCENT_CONTENT_REVIEW_REGION=ap-guangzhou
TENCENT_CONTENT_REVIEW_TEXT_BIZ_TYPE=
TENCENT_CONTENT_REVIEW_IMAGE_BIZ_TYPE=
TENCENT_CONTENT_REVIEW_VIDEO_BIZ_TYPE=
```

文字和图片不配置 BizType 时使用腾讯默认策略；视频必须使用开通后取得的真实 BizType，不能照抄示例。公开素材 HTTPS 地址需腾讯可正常访问、无重定向；配置 CDN/防盗链时一起验证。region、策略和实际开通服务地域须匹配。密钥不回传前端、不写入日志；这里不回退复用支付、短信或身份验真密钥。

Beat 每30秒调度 `moderation.process_pending`。文字/图片同步接口在后台调用；视频任务只创建一次，保存 TaskId 后轮询。未知结果、网络错误、策略缺失均转人工，不视为通过。视频创建响应丢失时不自动重新创建；客服人工核查，避免重复计费。已知 TaskId 查询失败可在后台“重新检测”继续查询。

自动检测开关关闭时，**已有等待检测/检测中的记录不会偷偷放行，也不自动发出腾讯请求**，客服可直接在后台人工复核；超过15分钟进入实时待办。重新开启自动检测后可继续处理队列。

## 后台操作

菜单：**内容审核**。平台管理员迁移后获得 `content.review.view`、`content.review.manage`；其他全平台角色按需分配。城市/代理角色不能访问全局内容。只读角色只能查看。

- 可按状态、类型、审核编号/文字查询、分页，查看送审文字、媒体、检测结果和处置历史。
- “登记现有内容”：选择类型，填写原业务记录的数据库 ID；与页面的 UUID 公开编号不是同一字段。登记本身不改变原业务状态。
- “核实通过”：人工确认内容安全；达人/活动/评价仍需原业务审核。
- “驳回内容”：未发布的内容不替换旧资料；匹配当前公开版本时隐藏。
- 内容未通过不会自行驳回原业务申请或触发退款。需要退回达人资料/服务修订、退还活动发布费时，客服还须在原业务审核中驳回，沿用原通知、退款与重新提交流程。
- “下架内容”：已通过的当前公开版本停止展示；“恢复内容”必须在原下架记录核实操作。其他版本的记录不能覆盖原下架限制。
- 所有人工操作要求至少5字依据并使用版本校验。内容或结果变化会拒绝旧操作，需刷新重新核查。
- 昵称、头像检测/审核结果可在用户“编辑个人资料”查看；原达人、活动、评价业务审核通知流程保持不变，不接入短信/微信推送。

实时待办包含人工复核和超15分钟的检测队列。仅查看/刷新/登记不会触发任何支付或退款。

## 验收

离线测试：`python scripts/check_wechat_auth.py moderation.tests moderation.test_business moderation.test_gateway`（只用内存 SpatiaLite，腾讯 HTTP 已模拟）。后台 `npm run build` 和 `node tests/contentReviews.browser.cjs`（模拟接口）。这不替代生产腾讯账号的小量图片、视频、超时/拒绝样本验收，也不替代 PostgreSQL 并发锁测试。

### 2026-10-22 PostgreSQL 实际验收

- PostgreSQL 18.4 / PostGIS 3.6.4；独立临时库执行完整迁移，未对已有业务库迁移、清表或修改业务数据。
- 执行 `moderation.test_settings`、`moderation.test_gateway`、`moderation.tests`、`moderation.test_business`、`moderation.test_concurrency`：**52 项通过，0 失败，0 错误，0 跳过**，测试用例耗时 105.111 秒（不含迁移）。
- 11 项并发用例使用独立数据库连接，验证重复提交去重、最新版本唯一、多人审核版本保护、任务领取互斥、检测时不持有数据库事务、晚到结果隔离、过期租约隔离，以及下架与资料/服务发布竞争。
- 实例不支持 TLS，经此前测试用途授权，使用 `--allow-plaintext`；未修改服务器 TLS 配置。只在测试子进程中使用连接配置，不落盘凭据。
- 全部样本为合成测试数据；外部 HTTP 被测试程序拦截，腾讯网关响应使用模拟数据，**此次不是腾讯真实接口联调**。
- 本轮临时库 `codex_regression_20261021_161808_a556f4c0` 已由测试程序在验证库身份后删除，未删除其他数据库。
- 复现入口：`scripts/check_postgres_regression.py`；连接参数由标准输入 JSON 提供，勿将密码写进命令参数或测试报告。

官方接口：

- [TextModeration](https://cloud.tencent.com/document/api/1124/51860)，TMS v2020-12-29，UTF-8 Base64文字。
- [ImageModeration](https://cloud.tencent.com/document/api/1125/53273)，IMS v2020-12-29，服务端构建 FileUrl。
- [CreateVideoModerationTask](https://cloud.tencent.com/document/api/1265/80017)、[DescribeTaskDetail](https://cloud.tencent.com/document/api/1265/80016)，VM v2021-09-22。创建结果兼容文档定义的 `OK` 和官方示例的 `200`；只有 `FINISH` 且明确 `Pass` 才通过。
