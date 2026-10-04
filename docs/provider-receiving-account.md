# 达人收款资料、个人开户与余额提现

## 交付范围（2026-10-03 更新）

本次在已有资料登记基础上，接通**个人分账/结算用户（LV1）**开户、本人银行卡业务入驻、查询和通知处理。这不是新支付商户进件，也不是订单分账或转账接口。

后端为 Python/Django，使用项目锁定的官方 `dg-sdk==2.0.24` 专属 Request；达人端为 uni-app，运营端为 Vue。代码通过隔离数据库和模拟网络测试，**尚未进行真实汇付开户、真机或银行卡到账验收**。生产开户开关默认关闭，本轮没有部署或变更生产配置。

### 已实现流程

1. 达人保存本人收款资料，敏感字段加密；保存本身不触发开户。
2. 达人单独确认 `huifu-personal-cash-v2` 开户及余额提现授权后，持久化申请流水，调用 `/v2/user/basicdata/indv`。
3. 获得汇付用户号后先落库，再调用 `/v2/user/busi/open` 配置本人储蓄卡和手动提现参数。`card_info` 与 `cash_config` 分别为一层 String(JSON Object) 和 String(JSON Array)，手机号使用本人银行预留号码；不再发送自动结算 `settle_config_list`。
4. 同步受理和审核通知不直接视为配置就绪；通过 `/v2/user/basicdata/query` 核对身份、本人卡、手动提现参数及平台承担手续费，并明确核验自动结算已关闭。字段缺失不能视为关闭。
5. 开户结果未知时不重复创建。`/v2/user/list/query` 严格核对归属，并经详情查询确认身份后恢复用户号；查不到不表示可以再次开户。

地区来自官方编码表，不使用用户手填名称作为渠道编码。资料保存、渠道开户、审核和银行卡结算状态分开展示；前端延续分组表单及明确的操作反馈。提交渠道后禁止直接修改或删除本地资料，换卡、更正和外部账户注销需另行处理，不自动调用渠道变更接口。

### 严格验签适配

官方 SDK 2.0.24 的 `ApiRequest._build_return_data` 会直接返回缺少 `sign` 的 JSON；因此业务层只判断 dict 或 `resp_code`，不能证明验签发生过。

经用户授权，`providers/huifu_user_transport.py` 在**项目适配层**增加调用期间的严格保护：

- 请求仍由官方 Request、SDK 签名及 HTTP 传输完成；不修改 site-packages、不手写 HTTP、不关闭 TLS 校验。
- 同步响应必须为受支持的签名信封；实际密码学验签仍交由原 SDK 解析器完成。缺签、篡改、异常 JSON、验签失败和超时都进入“结果待核实”，不自动重发开户。
- 限定 SDK 2.0.24 和官方生产地址，SDK 调试日志必须关闭；升级 SDK 后需重新验证适配契约。
- 复用支付模块的 `orders.huifu._SDK_LOCK`，并在成功或失败后恢复 SDK 方法和全局配置，避免开户与支付并发时串用商户配置。
- 返回值只保留业务必需字段，不向业务层传递开户初始密码；不将 SDK 原始异常、请求体、个人资料或密钥写入日志。

通知与同步响应采用不同验签输入：用户业务通知对原始 `data` 字符串验签，关联请求日期、流水、用户号及申请单后去重，防止旧的处理中通知覆盖审核终态。ACK 为 `RECV_ORD_ID_` 加请求流水。个人开户和详情查询本身没有异步通知。

**后续分账与余额提现已加入默认关闭的试点代码**：仅白名单中新创建的全额外部支付订单使用延时交易，满足完成、冻结及售后条件后核验分账，再计入达人可提现余额；银行卡出款必须由达人主动申请。其他订单路径保持不变，不自动补分历史订单。详见[余额与手动提现](provider-income-withdrawals.md)。开户就绪不代表余额已入账或银行卡到账。

旧自动结算账户需要重新确认 v2 授权，并由运营在渠道完成关闭自动结算、开通手动提现，再刷新核验；重新授权只保存授权，不会自动修改渠道账户。

## 部署配置与启用顺序

先审核并部署代码，备份数据库，按项目部署流程执行全部迁移（含 `orders.0029_provider_order_distribution` 与 `providers.0022_provider_income_wallet`）。以下全部是**服务端**配置，不提供真实默认值。

### 资料登记

```dotenv
PROVIDER_RECEIVING_ACCOUNT_COLLECTION_ENABLED=true
PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY=<独立的32字节随机密钥，经标准Base64编码>
```

这两个参数只开放资料登记，不会开启真实开户或分账。密钥由部署密钥管理系统生成、保管和备份，不提交仓库、不放入前端、不发送给客服。没有有效密钥时保存返回 503，不能退化成明文。

### 渠道开户与手动提现配置

```dotenv
HUIFU_USER_ONBOARDING_ENABLED=false
HUIFU_USER_UPPER_ID=<已确认归属主体的真实上级汇付号>
HUIFU_USER_NOTIFY_URL=https://<对外API域名>/api/v1/providers/receiving-account/huifu-notify/
# 未在后台配置时才读取此兼容项；新部署建议使用后台表单，不必手写 JSON。
HUIFU_USER_CASH_CONFIG=
HUIFU_USER_SKILL_SOURCE=hfps/1.3.5;hfms/1.0.4
```

复用现有 `HUIFU_ENV=prod`、`HUIFU_SYS_ID`、`HUIFU_PRODUCT_ID`、`HUIFU_RSA_PRIVATE_KEY` 和 `HUIFU_RSA_PUBLIC_KEY`。私钥只在服务端使用；请求签名由 SDK 自动生成，运营和达人均无需填写签名。

启用前需由平台与汇付确认：

- 系统号所属主体角色、上级汇付号、产品及个人用户开户、余额查询和手动提现权限。
- 商户私钥与已登记公钥配对，汇付公钥正确；通知地址 HTTPS 公网可达，且网关未要求用户登录。
- 在「运营配置 → 收款与提现配置」选择 T1/D1、真实固定费用/费率和平台扣费账户；D1 工作日差异配置须按合同填写。代码固定 `out_fee_flag=1`，承担方取服务器 `HUIFU_MERCHANT_ID`。不能照抄示例费率或推断费用承担方。旧 `HUIFU_USER_SETTLEMENT_CONFIG` 不再用于新开户。
- SDK 调试日志关闭，反向代理、APM 和录屏不采集收款请求体；数据库及密钥备份访问受限。

以上确认、部署和重启完成后，再由有权限的人员把开户开关设为 `true`。只有达人另行授权并主动提交，才会发送真实资料；开关本身不会批量开户。首个真实账户需获得明确授权后联调，核对渠道后台、通知与查询结果。当前未实施此生产步骤。

关闭开户开关会禁止新的主动提交；已提交账户仍需保留有效查询/通知配置以核对结果。不能直接改上级号或产品号来重试，跨渠道配置会被拦截。异常结果保持待核实，不通过人工改本地状态冒充渠道成功。

## API 与安全边界

### 后台收款与提现表单

部署时执行 `backoffice.0031_receivingwithdrawalsetting` 迁移，并更新运营前端。入口为「运营配置 → 收款与提现配置」，需 `operations.manage` 权限且属于平台全局数据范围（超级管理员亦可）；普通达人或城市代理不可访问。

- GET/PUT `/api/v1/admin/operation-settings/receiving-withdrawal/`。GET 不建记录；PUT 完整提交表单、当前 `revision`、显式 `confirmed=true` 与非空变更说明；并发旧版本返回 409，不覆盖其他管理员的修改。更新与审计记录同一事务。
- 表单无费率、周期或账户类型默认值。固定费用与百分比费率至少填一项，最多两位小数；明确的 `0` 表示免费，不将空值当零。两项同时填写相加；`0.05` 表示 `0.05%`。T1 不能带 D1 工作日字段。
- 首次发布前，继续兼容 `HUIFU_USER_CASH_CONFIG`；发布后数据库优先，后续新建开户配置请求自动读取，不需重启。数据库缺表/读取失败、配置损坏或承担商户变更时阻止新提交，**不退回旧环境费率**。商户变更须重新核实费用并发布。
- 环境变量中的商户号、上级号、产品、回调地址、密钥及开户/提现开关仍由运维配置。页面只返回本地检查结果，不返回密钥或原始渠道 ID；保存不更改这些参数，不触发任何汇付 HTTP 请求，不产生批量开户或出款。
- 页面「本地校验通过」不等于已获得渠道业务权限、回调公网可达或账户已开通。开户、提现和资料登记仍有各自的开关与安全条件，正式业务需独立验收。
- 发布仅影响后续新建请求的配置快照；已有请求、审核中账户及已开通账户不会自动变费率。原有核验与提现继续按各自持久化快照进行。旧账户调整仍需单独与汇付处理，不能改本地状态代替。
- 操作记录可在「系统管理 → 操作审计」查看「更新收款与提现配置」，含修改前后、操作者及变更说明，不记录密钥或银行卡。

离线回归：`uv run python scripts/check_wechat_auth.py backoffice.test_receiving_settings providers.test_receiving_onboarding providers.test_huifu_user_transport`；管理端 `npm run test:receiving-settings` 和 `npm run build`。

### 达人本人接口

本人基础路径：`/api/v1/providers/me/receiving-account/`。

- GET：返回本人状态、脱敏号码、非敏感表单项及授权说明，不创建记录。
- PUT：仅审核通过并已完成实名认证的本人可保存；验证实名摘要、证件有效期、号码格式、省市关系及资料登记授权。三个敏感字段编辑时留空表示保留原加密值，首次填写不可留空。渠道提交后冻结编辑。
- DELETE：只能清除尚未提交渠道的资料；资料收集开关关闭或加密密钥不可用时仍可清除未提交草稿，不删除实名或资金记录。
- `regions/` GET：官方省市编码选项。
- `submit/` POST：需独立的开户授权版本与确认标记；持久化请求并防止重复开户。
- `refresh/` POST：查询并核对渠道结果，不直接重发开户或绑卡。

上述本人接口均鉴权、限流并禁止缓存。通知路径 `/api/v1/providers/receiving-account/huifu-notify/` 不使用用户登录鉴权，但必须通过签名和申请关联校验。

### 刷新后的核验结果（2026-10-04）

- `cash_status/card_status` 为本地汇总状态，不是对汇付拒绝码的原样透传。刷新查询后，`S` 表示该项已核对，`F` 表示明确未满足条件（如提现关闭、费率或本人银行卡不匹配）；空字符串表示缺失、格式异常、脱敏或结果存在歧义，不能称为“开户失败”。
- `automatic_settlement_disabled=true` 仅来自明确空配置列表或所有结算开关关闭；`false` 表示仍有开启项；`null` 表示没有可靠结果。省略字段绝不推断为已关闭。
- 三组结果独立核验；某一组缺失不会吞掉其他组结果，也不会沿用上次银行卡或提现核验成功。`channel_notice` 给出固定白名单原因，不回传渠道原始响应、姓名、卡号、卡标识或手续费承担方 ID。
- 只有完整核验、当前授权及审核状态满足条件才进入 `active`；原有提现开关、白名单、限额、余额和时效校验不变。整体查询验签或身份核对失败时不采用响应内容。
- 已用于渠道申请的资料不再提示“尚未提交”或“可以直接清除”；资料登记可用时 `collection_unavailable_reason` 为空。旧数据不会批量改状态，部署后需点击「刷新渠道状态」重新查询；本次无需数据库迁移。

排查入口：达人端「收款账户 → 刷新渠道状态」，运营端「达人管理 → 详情 → 收款账户资料」。若提示缺少配置、返回格式异常或资料已脱敏，将具体提示及核验时间交平台与汇付核实，不必提供完整身份证或银行卡。只有明确提示自动结算仍开启时才要求渠道关闭；不要直接修改数据库状态，也不要重复开户。

完整身份证、银行卡、银行预留电话及申请资料保存在 AES-256-GCM 加密内容中；每次保存使用新 nonce，AAD 绑定达人 ID 与版本。本人接口和后台仅回显号码掩码，前端不持久化敏感表单。密钥丢失无法恢复已有资料；轮换需旧密钥解密、新密钥重加密并验证，不能直接覆盖旧密钥。

账号注销会清除尚未提交渠道的资料；只要存在渠道提交记录，就要求客服先核对外部账户，不能把正式渠道账户当成可直接删除的草稿。有收入余额、提现冻结款或账务异常时也禁止注销。平台资料保存授权 `receiving-materials-v1` 不授权自动对外提交；开户前必须另行确认 `huifu-personal-cash-v2`。

## 验证与沙箱边界

```sh
uv run python scripts/check_wechat_auth.py providers.test_cash_accounts providers.test_huifu_user_transport providers.test_receiving_onboarding providers.test_receiving_accounts accounts.test_account_closure orders.test_huifu_gateway orders.test_huifu orders.test_payment_recovery
uv run python scripts/check_wechat_auth.py
```

使用全新内存 SpatiaLite 数据库，不加载业务 `.env`、不连接业务数据库。适配测试使用合成 RSA 密钥，实际经过官方 SDK 请求签名及响应验签代码，只模拟网络；覆盖缺签、篡改、错误信封、超时、状态恢复、重复提交保护及支付/开户并发配置隔离。该测试不替代 PostgreSQL 锁并发、真实渠道或真机验收。

达人端：`npm run type-check`、`npm run build:h5`、`npm run build:mp-weixin`、`npm run test:receiving-account:mp`；运营端：`npm run build`。

官方 `hf-payment-local-sandbox` 1.0.0 预览版已安装在工作区 `.tools/huifu-sandbox/windows/`，不是 Codex skill，也不入库。外包和 Windows 包 SHA-256 已与官方发布索引核对，`version --json`、`doctor --json`、`validate contract` 通过。未启动服务或传入生产密钥。

该沙箱**不提供 `/v2/user/*` 用户进件端点**，不能用于证明真实开户联调成功。预览版发布包不带代码签名；版本、哈希和适用范围见[官方发布索引](https://cloudpnrcdn.oss-cn-shanghai.aliyuncs.com/huifuskills/hf-payment-local-sandbox-latest.json)。

## 仍需单独完成

本地分账准备记录、资金来源核对及退款暂停防护见[达人订单分账准备](provider-settlement-plans.md)；后续默认关闭的执行与提现试点见[余额与手动提现](provider-income-withdrawals.md)。

- 经授权的生产开户验收及 PostgreSQL 并发验证。
- 新订单分账至余额、手动提现的真实小额验收；分账退款回退、银行卡长期退汇对账仍需后续闭环。
- 换卡、外部账户关闭和对应的数据保留流程。
- 历史订单按真实渠道状态另行制定方案，不修改本地记录冒充资金操作。

## 文档依据

实际使用开户 skill references：`user-onboarding-individual.md`、`user-onboarding-business-open.md`、`user-onboarding-detail-query.md`、`user-onboarding-list-query.md`、`user-onboarding-complete-field-catalog.md`、`user-onboarding-field-contracts.md`、`user-onboarding-platform-contracts.md`、`user-onboarding-shared-server-sdk-matrix.md`、`user-onboarding-shared-signing-v2.md`、`user-onboarding-shared-credential-boundary.md`、`user-onboarding-shared-overview.md`、`user-onboarding-external-resources.md`、`user-onboarding-error-codes.md`。支付 skill references：`shared-local-sandbox.md`、`copilot-existing-system.md`、`copilot-go-live-checklist.md`、`copilot-solution-selection.md`、`aggregation-python-adapter.md`。

字段与协议依据：[个人用户开户](https://paas.huifu.com/partners/api/doc/yhgl/api_yhgl_gryhjbxxzc.md)、[用户业务入驻](https://paas.huifu.com/partners/api/doc/yhgl/api_yhgl_ywrz.md)、[用户信息查询](https://paas.huifu.com/partners/api/doc/yhgl/api_yhgl_yhywcx.md)。列表查询的字段表与示例存在字符串/数组差异，适配点兼容两种形态；若详情只返回脱敏号码或省略结算配置，不猜测匹配成功，保持待核实并联系汇付确认。
