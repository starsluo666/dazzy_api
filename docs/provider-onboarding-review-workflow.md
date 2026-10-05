# 达人准入与资料、服务审核流程

## 目标

本流程覆盖达人申请、服务分类授权、首次开通审核，以及开通后的资料和服务变更审核。

核心规则：

- 申请姓名为真实姓名，必须与后续实名认证姓名一致。
- 达人名称是独立公开名称，不要求唯一，用户端不再使用账号昵称代替。
- 客服通过达人初始申请时，必须选择该达人允许经营的服务分类。
- 服务分类的价格区间按“按小时”和“按次”分别配置。
- 实名认证、达人资料、至少一项服务都已提交后，系统自动进入首次开通审核。
- 资料和服务的新增、修改、重新上架采用“先审核、后生效”；已有线上内容在审核期间保持不变。
- 服务下架立即生效，不需要审核。

## 状态流转

### 初始申请

1. 用户填写真实姓名、出生日期、近期生活照、服务城市等申请信息。申请不再要求简介和服务半径；两项在达人端完善正式资料，城市仍用于审核归属。
2. 客服审核申请；通过时必须勾选允许经营的服务分类。
3. 申请通过后，达人分别提交实名认证、达人资料和服务配置。
4. 三项均有待审核记录后，`onboarding_status` 自动变为 `pending_review`。
5. 客服在“开通审核”中统一审批：
   - 通过：实名认证、资料及待审核服务在同一事务中生效，达人变为可开通状态。
   - 驳回：三部分均记录驳回结果和原因，达人修正后重新提交。
6. 从未接过单的达人须完成接单学习并通过答题，才可开启在线接单及接受订单。其他开通、实名认证及服务限制继续生效。

### 首次接单学习

- 后台「运营配置 → 接单学习」，仅平台 `operations.manage` 权限可管理，城市代理即使持有该权限也不可操作。
- 可管理最多 30 篇分段文字资料、50 道单选题（2–6 个选项，两个选项可作判断题），支持排序、移除、答案说明和通过分数。默认 100 分全部答对，可重新作答。
- 保存草稿不改变线上内容；至少一篇资料及一道有效题目才可发布。每次发布生成不可变快照，用配置 revision 防止运营相互覆盖。
- 达人逐篇确认学习后作答；服务端校验全部阅读记录、题目和答案范围并判分。不向达人端发送标准答案或内部说明。按题目等权计分，显示分数向下取整。
- 通过时间、答题记录和学习版本保存在数据库。通过后不因后续版本更新要求重考；未通过者遇到发布新版本返回 409，需刷新重新学习。重复通过请求不重复记通过记录。
- 后端在开启在线、在线展示/可下单判断和接受订单时分别拦截，旧客户端不能绕过。未发布内容时新达人暂不能接单，须先由运营发布；不植入默认标准答案。
- `providers.0023_first_order_training` 将迁移时存在 `accepted_at` 的历史订单所属达人标记免考，其余须学习。不以入驻通过、订单数量、支付成功代替已接过单的证据。
- 入口：达人工作台提示卡及「我的 → 接单学习」。这不是人工开通审核的一部分，学习不自动批准实名认证/资料/服务，也不改变已接订单的履约流程。

接口：`GET /api/v1/providers/me/training/`；`POST /api/v1/providers/me/training/lessons/{uuid}/complete/`（version_id）；`POST /api/v1/providers/me/training/submit/`（version_id、answers）。管理端 `GET/PATCH /api/v1/admin/provider-training/`（revision、draft），`POST /api/v1/admin/provider-training/publish/`（revision）。

发布顺序：备份数据库 → 测试环境执行全部迁移 → 部署 API 和管理端 → 保存并发布学习内容 → 发布两端应用并验收。生产库迁移、正式资料发布和真机验收不由本地测试代替。并发锁使用数据库事务和行锁，本地 SpatiaLite 测试不替代 PostgreSQL 并发验收。

离线回归：`uv run python scripts/check_wechat_auth.py providers.test_training providers.test_media providers.tests orders.tests orders.test_fulfillment`；达人端 `npm run test:training`、`npm run type-check` 和 H5/小程序构建。城市分类的 JSON contains 用例仅在支持该查询的数据库（生产 PostgreSQL）运行。

### 开通后资料变更

- 达人提交后生成 `ProviderProfileRevision`，不会覆盖当前线上资料。
- 客服通过后才发布新资料；驳回时保留原资料并返回原因。
- 同一达人同时只允许有一条待审核资料修订。

### 开通后服务变更

- 新增、修改、重新上架会生成 `ProviderServiceRevision`。
- 提交时及审核通过时都会再次校验分类授权、分类启用状态和对应计费方式的价格区间。
- 审核通过后才创建或更新线上服务；驳回不影响已有线上服务。
- 下架直接将线上服务设为停用，并终止该服务尚未完成的待审核变更。

## 数据模型

- `ProviderProfile`
  - `application_real_name`、`application_birth_date`：初始申请实名信息。
  - `display_name`：面向用户展示的达人名称。
  - `onboarding_status`：首次开通审核状态。
- `ProviderCategoryGrant`：客服授予达人的服务分类范围。
- `ProviderProfileRevision`：待审核的达人资料快照。
- `ProviderServiceRevision`：待审核的服务新增或修改快照。
- `ServiceCategory`：分别保存按小时、按次的最低和最高价格（单位：分）。

## 后台接口

- 初始申请审核：`POST /api/v1/admin/provider-applications/{id}/review/`
  - 通过时必须传 `allowed_category_ids`。
- 变更审核列表：`GET /api/v1/admin/provider-change-reviews/`
  - 支持 `kind=onboarding|profile|service` 和 `status=pending|approved|rejected`。
- 变更审核操作：`POST /api/v1/admin/provider-change-reviews/{kind}/{id}/review/`
- 服务分类维护：原分类接口新增四个价格上下限字段。

## 发布与数据迁移

- 数据库迁移：`providers.0014_provider_onboarding_and_change_reviews`。
- 初始价格策略：`providers.0015_configure_initial_service_price_ranges`。
- 历史服务会自动生成对应分类授权。
- 历史已满足上线条件的达人会回填为已开通，尽量保持原有线上业务连续。

首版价格区间如下，单位为人民币元；运营后续可以在服务分类后台调整：

| 服务分类 | 按小时 | 按次 |
| --- | ---: | ---: |
| 旅游陪伴 | 100–500 | 300–3000 |
| 台球陪玩 | 100–300 | 200–1500 |
| 麻将陪玩 | 100–300 | 200–1500 |
| 桌游陪玩 | 100–300 | 150–1500 |
| 商务陪同 | 150–800 | 500–5000 |
| 城市陪伴 | 100–500 | 300–3000 |

## 验收重点

- 未获授权的分类在达人端不可选择，绕过前端直接请求也会被后端拒绝。
- 超出当前计费方式价格区间的服务不能提交，也不能被后台误审批。
- 首次开通审核通过前，达人不能公开展示、接单或上线接单。
- 审核中的资料和服务不会提前影响用户端展示和订单快照。
- 达人服务下架即时生效。
- 客服可在初始申请详情中查看完整手机号，列表仍使用脱敏手机号。
