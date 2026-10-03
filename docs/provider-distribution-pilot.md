# 达人延时分账：受控试点（第二阶段）

## 当前范围与未完成项

本阶段接通官方 SDK 2.0.24 的延时交易标记、原支付核验、交易确认（分账）、确认查询和分账/退款互斥。**默认关闭，不回填历史订单，不部署或执行真实出款。** 新增独立关闭的自动任务与[达人余额手动提现](provider-income-withdrawals.md)：分账核验后先入达人余额，不自动结算到卡。仅在首次创建支付请求时选择测试白名单达人、全额外部支付的服务订单；钱包/混合、活动发布、充值不改变原支付参数。订单抽成仍取原佣金快照，不全局固定为 30%。

这是小范围联调入口，不是全量生产闭环：

- 已分账的自动回退、部分退款后重新分账尚未接通。普通退款创建及退款任务执行都检查分账记录；一旦登记过分账（包括失败/未知），转异常交易处理，**不能人工删记录、改状态后硬退**。应先按渠道流水核账，受控回退能力完成并验收后再处理；不允许以本地“回退成功”替代真实渠道证据。
- 不自动重发分账，包含已核验 F。即使崩溃发生在真正发送前，登记的流水仍只可查单/人工核账。这是有意的安全限制。
- 不表示银行卡已到账；分账记录不推算提现费用，保持 `null`。实际提现费用记录在独立提现单，不能重复计入各订单成本。
- 尚需隔离 PostgreSQL 的并发验收及真实小额测试。离线测试不证明商户权限、平台可用余额或分账费用配置生效。

## 官方合同与适配边界

2026-10-03 根据 `huifu-pay-integration` 1.3.5 的存量系统/聚合支付/Python SDK/交易完整字段目录，以及汇付官方文档核对：

- 下单 `/v4/trade/payment/create`：仅新试点支付 `delay_acct_flag=Y`，其余 `N`。原支付查询 `/v4/trade/payment/scanpay/query`，同时验证商户、日期、支付流水、渠道流水、S、Y、实付和待确认余额，以及 `payment_fee` 中的内扣/外扣标记、平台承担方及实际金额。扣款标记必须与首次支付快照一致，不使用当前环境变量重新解释已支付订单。
- [交易确认](https://paas.huifu.com/partners/api/doc/smzf/api_jyqr.md)（更新 2026-09-21）：`/v2/trade/payment/delaytrans/confirm`；生成独立确认日期/流水，原交易指向支付日期/流水。`acct_split_bunch` 是 String(JSON object)，`acct_infos` 是原生数组，只提交接收方和元金额，不把响应手续费字段复制到请求。零金额的接收项省略。
- [交易确认查询](https://paas.huifu.com/partners/api/doc/smzf/api_jyqrcx.md)（更新 2026-06-10）：`/v3/trade/payment/delaytrans/confirmquery`；`org_req_date/org_req_seq_id` 指向 **分账确认请求**，不是支付流水。`00000000` 仅代表查询成功，必须核对 `trans_stat`。
- V3 官方 `acct_split_bunch` 顶层文字称 JSON-array，嵌套字段表定义 `acct_infos`。当前仅按嵌套表对象结构解析，返回不同形状、缺少费用明细或全局流水时保留待核实。需要汇付确认实际商户响应契约后才能补充形状适配，不自动猜测。成功时逐项核对接收方、金额、去重、费用及承担方；缺少手续费不能记零。
- [交易确认退款](https://paas.huifu.com/partners/api/doc/smzf/api_jyqrtk.md) 列明 `23000004`：原分账交易带手续费时不支持部分退款。当前不实现猜测性的回退或垫资请求，这是已分账退款停止在异常处理的原因。

使用 SDK 原请求和 HTTP/TLS/签名验签；严格响应封装防止 SDK 无签名响应被误当可信数据。不输出签名、密钥、完整响应或个人资料。与支付/开户共用 SDK 锁，调用后恢复全局配置。支付预检摘要、不可变请求快照、每次提交/查询的脱敏摘要与状态留档。

## 数据与幂等

迁移 `0029_provider_order_distribution` 增加支付单的延时标记/范围快照，以及分账记录和查询审计表，**无历史回填**。先迁移，再运行新 API/任务进程。

执行前：订单确认完成、冻结期到期、本地账务已结算；无未处理售后及任何未成功退款（含失败）；本地资金构成一致；当前渠道范围与首次支付一致；收款人未变化；达人本人提现卡、手动取现、自动结算已关闭在最近 30 分钟内核验；提现手续费外扣账户为平台。

原支付只读查单后，在订单锁下重查上述条件，确保其间新建退款/售后或金额变化会中止分账。一个结算单只能登记一次分账流水，登记事务先提交，再请求渠道。重复执行只返回原记录；超时、网络错、未验签、查无记录均不授权重发。同步 S 仍需独立查询核验。终态不被乱序处理中的响应覆盖，冲突进入人工核账提示。

后台「达人结算」只读显示：准备记录、渠道分账状态、请求流水、支付手续费扣款方式、平台扣费前抽成、平台分账金额、分账请求总额、平台支付/分账费用及异常原因。没有分账执行按钮，也没有客户端指定金额/收款人入口。

### 支付手续费与分账金额

`HUIFU_FEE_FLAG` 是支付手续费扣款方式，不是费率，也不是提现费用配置。付款时保存到支付单范围快照，分账时核对已验签的原支付查询响应 `payment_fee`，只使用实际 `fee_amount`，不硬编码 `0.35%`，不猜测缺失费用为零。

| 原支付扣款方式 | 达人分账 | 平台分账 | 待确认金额校验 |
|---|---|---|---|
| `1` 外扣 | 原结算达人金额 | 原平台抽成 | 达人金额 + 平台抽成 |
| `2` 内扣 | 原结算达人金额 | 原平台抽成 − 实际支付手续费 | 达人金额 + 平台扣费后分账金额 |

例：服务订单实付 100 元、原结算约定达人 70% / 平台 30%，查单核实内扣手续费 0.35 元：达人分账 70 元、平台分账 29.65 元，分账总额 99.65 元。原业务结算仍记达人 70 元、平台抽成 30 元；手续费作为独立成本记录，不把达人收入重算成 `99.65 × 70%`。汇付已内扣的费用不再发起一次扣款。外扣模式仍分 70 / 30 元，由平台另行承担渠道扣费。

平台份额不足以覆盖内扣费用、扣费模式/承担方不一致、费用缺失或待确认余额不等于本次分账总额时，在登记分账请求前中止并提示人工核账，不减少达人收入、不自动垫资、不尝试部分分账。平台份额恰好被费用抵完时省略零元接收项。分账费用仍单独核验为平台承担，提现费用仍归属提现单，平台分账金额不代表最终净收益。

新增快照字段 `fee_flag/platform_split_amount/split_amount` 不改变数据库结构。历史已登记分账仍按原接收方和金额查单，不重发；缺少新增字段的旧外扣记录按原平台分账金额展示。切换环境中的扣款方式只影响之后的新支付，不重算已保存支付/分账快照。

## 上线前配置（请勿现在批量开启）

两个开关独立：停止新延时下单可以保持存量查单；关闭分账执行也仍可查询。支付渠道基础配置须仍有效。

```dotenv
HUIFU_PROVIDER_DELAYED_PAYMENT_ENABLED=false
HUIFU_PROVIDER_DISTRIBUTION_ENABLED=false
# ProviderProfile 的数字 ID，逗号分隔，不是手机号或汇付账户号。
HUIFU_PROVIDER_DISTRIBUTION_IDS=
# 三类费用渠道规则确认后才设 true；这个开关不会修改汇付配置。
HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED=false
# 单笔允许的金额，单位分；0 表示未授权任何额度。不代替渠道自身限额。
HUIFU_PROVIDER_DISTRIBUTION_MAX_CENTS=0
```

渠道必须 prod，支付费用按实际开通方式配置 `HUIFU_FEE_FLAG=2`（内扣，仅扣平台份额）或 `1`（平台外扣）。提现是独立配置，不因支付内扣而改为达人承担：新开户优先使用后台「收款与提现配置」，首次发布前兼容 `HUIFU_USER_CASH_CONFIG`；`out_fee_flag=1`、`out_fee_huifu_id` 为平台支付商户号。详情查询使用不同响应键 `out_cash_flag/out_cash_huifuid/out_cash_acct_type`。旧自动结算配置不能直接用于本流程。真实费率、账户类型、平台备付余额由渠道核实；更新后台表单或环境变量均不会自动修改既有渠道账户。表单权限、版本冲突与生效范围见[收款配置](provider-receiving-account.md)。

## 你准备好测试达人后

1. 完成本人身份/银行卡资料、新版本手动提现授权及取现开通。刷新渠道状态，确认本人提现卡、手动提现、自动结算关闭及平台承担提现费用。
2. 确定一名测试达人的 ProviderProfile ID、一个已审核服务、小额订单金额和真实执行授权。先确认生产部署迁移、平台手续费余额、渠道三类费用承担规则，并完成上述 PostgreSQL/回退风险验收；不要批量启用。
3. 仅对这个达人启用新延时下单；用**无钱包抵扣**的新服务订单支付。支付成功后检查渠道 `delay_acct_flag=Y`、`payment_fee` 和 `unconfirm_amt`，核对内扣/外扣模式、实际手续费以及可分账总额。旧支付单不改 Y。
4. 正常接单、服务、用户确认完成，等待配置的冻结期；不直接改库绕过冻结或售后。执行前再次刷新收款账户。
5. 在服务器按订单运行下列命令。`execute` 会请求真实资金操作；只有明确确认小额实测时才运行。本次开发未运行。

```bash
sudo bash ./deploy-docker.sh manage provider_distribution inspect 订单号
sudo bash ./deploy-docker.sh manage provider_distribution execute 订单号 --confirm-real-funds
sudo bash ./deploy-docker.sh manage provider_distribution query 订单号
```

`inspect` 只查本地已有记录，不调用渠道。`query` 只查原流水；可重复查询，结果未知/失败请联系平台核账，不能删记录重新 execute。渠道分账核验完成后核对达人余额，再由达人主动申请提现并核对到账。完成分账回退、退款及资金对账闭环之前不扩大范围。

## 离线验证

```powershell
$env:COS_BUCKET='offline-test-1250000000'
uv run python scripts/check_wechat_auth.py orders.test_distributions providers.test_withdrawals orders.test_settlement_plans orders.test_settlement_fees orders.tests orders.test_huifu orders.test_huifu_gateway orders.test_payment_recovery wallets.tests.WalletServiceTests backoffice.tests backoffice.test_receiving_settings taskcenter.tests
```

独立内存库迁移，HTTP 完全替身；SDK 本体签名验签链使用临时 RSA 密钥。不是生产资金验收。
