# DAZZY 汇付支付、分账与达人提现对接方案

> **历史方案提示（2026-09-18）**：本文中的托管支付、`project_id`、
> `pre_order_id`、`jump_url` 与 Checkout JS 实施内容已停止作为当前支付实现依据。
> 当前用户支付已经迁移到 V4 聚合支付；请以
> [`huifu-aggregate-payment-migration.md`](./huifu-aggregate-payment-migration.md) 为准。

> 文档版本：V1.0
> 编制日期：2026-09-16
> 适用范围：用户端 H5、用户端 Android App、达人端入驻/余额/提现、Django API 及异步任务
> 当前阶段：H5 第一批代码已完成；真实商户参数未注入，生产开关保持关闭

## 0. 2026-09-16 实施进度

已完成：

- 官方 Python `dg-sdk==2.0.24` 已在项目 Python 3.12.14 环境验证并锁定依赖。
- `ProviderOrderPaymentOrder` 已补充 `req_date`、`req_seq_id`、`pre_order_id`、商户号、预下单状态、响应摘要、尝试次数和时间字段，并增加条件唯一约束。
- 已实现官方 SDK H5 托管预下单适配器，固定 `pre_order_type=1`、`request_type=M`，只开放 `T_JSAPI,A_JSAPI`，金额只取服务端支付单。
- 已实现 `POST /api/v1/provider-orders/{order_no}/payment-session/`；同一支付单固定同一请求日期和请求流水号，重复请求直接复用预下单结果。
- 已实现 `POST /api/v1/payments/huifu/notify/`；直接对原始 `resp_data` 做 RSA 验签，再校验商户、请求流水、金额、预下单号和交易状态，并通过官方查单接口二次确认。
- 已建立通知事件幂等表；只有核心入账完成后才返回 `RECV_ORD_ID_{req_seq_id}`。
- H5 支付页已改为创建真实支付会话并跳转汇付 `jump_url`；模拟支付仅在开发构建且显式设置 `VITE_ENABLE_MOCK_PAYMENT=true` 时启用。
- 已增加 H5 支付结果确认页，按 5 秒间隔、最多 30 次轮询本地订单；页面回跳不会直接展示支付成功。
- 后端订单模块 33 项回归测试通过；新增汇付定向测试覆盖官方 SDK 请求、服务端金额、防篡改、预下单幂等、失败重试、原文验签、主动查单和重复通知。

仍保持关闭/待完成：

- `HUIFU_PAYMENT_ENABLED=false` 是默认值；在真实商户号、系统号、产品号、RSA 密钥、托管项目号、HTTPS 通知地址和 H5 回跳地址完成联调前不得开启。
- 关闭订单、退款/退款查询、定时补偿查单、对账文件尚未切换至汇付正式通道。
- 达人汇付开户、订单分账、钱包和银行卡提现属于后续资金阶段，当前代码未假设其已开通。
- Android 支付尚未开放；App 构建会明确提示先使用 H5，不会静默回退到模拟支付。

## 1. 结论先行

DAZZY 的支付接入建议分三期完成：

1. **H5 首期**：采用汇付“托管支付预下单 + 斗拱收银台 JS SDK”。DAZZY 后端创建预订单，H5 只负责展示和发起收银台；最终支付结果只由汇付异步通知或后端主动查询确认。
2. **Android 次期**：先复用已验收的 H5 收银台，通过系统浏览器完成支付，再返回 App 并查询 DAZZY 后端结果。当前公开资料明确了 H5/PC 和非 ATU 环境跳转策略，但没有提供可直接落地的 Android 原生支付 SDK 契约，因此原生 SDK 不能凭经验猜测，须由汇付确认后再评估替换。
3. **达人资金期**：达人先完成汇付侧个人用户/业务入驻和结算卡配置；订单完成、风控冻结期结束后，通过汇付合规分账产品把达人应得金额进入其汇付账户余额；达人再提交取现。平台本地 `SETTLED` 只能代表内部结算完成，不能直接代表汇付分账成功或银行卡到账。

这三个链路必须共享以下原则：

- 金额永远由服务端根据数据库订单计算，不能采信 H5/App 传入金额。
- 前端回调、页面跳转、`callback_url` 都不能作为支付成功依据。
- 对异步通知先验签、再校验商户/订单/金额/状态、最后在数据库事务内幂等更新。
- 所有与汇付通信均使用官方 SDK；不自行拼装签名或手写网关 HTTP 客户端。
- 私钥、汇付公钥、商户号、系统号、产品号只保存在服务端密钥系统，绝不进入 H5、App、Git 或普通日志。

上述选择依据汇付官方的[支付接入说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/SKILL.md)、[托管支付预下单说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-preorder.md)、[Checkout JS 接入说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/checkout-js.md)和[异步通知规范](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-async-notify.md)。

## 2. 文档证据等级

为避免把推断写成“汇付规则”，下文使用三种标记：

- **[文档确认]**：汇付公开文档或官方 SDK 仓库已明确。
- **[DAZZY 设计]**：为适配当前业务和代码结构提出的工程方案。
- **[需汇付确认]**：公开材料无法确认，必须由汇付客户经理/技术支持书面确认后才能上线。

## 3. 官方资料与接口基线

### 3.1 用户提供的官方入口

- [API 接口说明](https://paas.huifu.com/docs/api/#/api_jksm)
- [SDK 与开发工具](https://paas.huifu.com/open/doc/devtools/#/)
- [公私钥获取](https://paas.huifu.com/docs/start/#/kfjr/hqmy)
- [异步通知说明](https://paas.huifu.com/docs/start/#/kfjr/ybxx)
- [API 联调工具](https://paas.huifu.com/open/doc/devtools/#/tools_apiltgj)

汇付在线文档是前端路由页面，接口字段会随产品版本变化。实施时应从商户控制台当前产品页重新导出字段表，并用官方联调工具验证请求；本方案同时引用汇付官方 GitHub 资料，以便固化可审计的接口行为。

### 3.2 本方案采用的接口族

| 能力 | 接口/SDK | 用途 | 依据 |
|---|---|---|---|
| 托管支付预下单 | `/v2/trade/hosting/payment/preorder` | H5/App 支付前生成 `pre_order_id` | [预下单说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-preorder.md) |
| 支付查询 | `/v2/trade/hosting/payment/queryorderinfo` | 通知补偿、前端回跳后的最终确认 | [支付查询说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-query-payment-status-query.md) |
| 关闭订单 | 官方托管支付关闭请求类 | 支付超时、取消未支付单 | [Python 适配说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-python-adapter.md) |
| 退款/退款查询 | 官方托管退款及查询请求类 | 全额/部分退款闭环 | [Python 适配说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-python-adapter.md) |
| 个人用户入驻 | `/v2/user/basicdata/indv` | 建立达人汇付用户 | [个人用户基本信息注册](https://paas.huifu.com/open/doc/api/#/yhgl/api_yhgl_gryhjbxxzc) |
| 用户业务入驻 | `/v2/user/busi/open` | 开通交易、结算/取现相关能力 | [用户业务入驻](https://paas.huifu.com/open/doc/api/#/yhgl/api_yhgl_ywrz) |
| 取现 | 汇付取现接口族 | 达人余额到结算银行卡 | [取现接口](https://paas.huifu.com/open/doc/api/#/jyjs/qx/api_qx) |
| 对账文件 | 官方对账文件请求类 | 日终资金核对 | [Python 适配说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-python-adapter.md) |

接口字段和请求类以实施时安装的官方 SDK 版本为准；表中路径用于确定能力边界，不允许据此绕开 SDK 自行发包。

## 4. 当前项目现状与差距

### 4.1 已有基础

- `dazzy_app` 是 uni-app，已经同时支持 H5 和 App 构建。
- 用户支付页 `dazzy_app/src/pages/booking/payment.vue` 当前调用 `simulateProviderOrderPayment`，适合替换为“创建预订单—调起收银台—查询确认”的真实链路。
- `orders.ProviderOrderPaymentOrder` 已保存支付单号、订单、付款人、应付金额、支付渠道、网关交易号和支付时间。
- `orders.ProviderOrderRefundOrder` 已有退款单、幂等键、退款金额和退款状态。
- `orders.payment_gateway.ProviderOrderPaymentGateway` 已建立支付网关边界，目前实现是 Mock。
- 项目已有 Celery + Redis，可承担关闭超时单、支付查询补偿、退款查询、分账/取现查询和日终对账。

### 4.2 必须补齐的差距

1. 支付单缺少汇付请求追踪字段：`req_date`、`req_seq_id`、`pre_order_id`、汇付商户/用户 ID、原始响应摘要、最近查询时间、失败码。
2. 当前支付状态没有单独表达“预下单中、待用户支付、确认中、支付失败”。不能把前端回调直接写成 `PAID`。
3. Mock 网关只有 `confirm_payment/refund`，真实适配器需要拆成 `create_preorder/query/close/refund/query_refund`。
4. 达人实名认证是 DAZZY 自有审核，尚不等价于汇付个人用户注册和业务入驻。
5. `ProviderOrderSettlement.status=SETTLED` 目前是平台内部“结算入账”，不含汇付分账结果；如果直接拿它计算可提现余额，会形成账实不符风险。
6. 尚无达人汇付账户、资金台账、提现单和异步通知事件表。

## 5. 总体架构

```text
用户 H5 / Android App
        |
        | 1. 请求 DAZZY 创建支付会话（只传订单号、所选渠道/运行环境）
        v
DAZZY Django API
        |-- 校验订单、付款人、金额、有效期
        |-- 官方 dg-sdk 创建托管预订单
        |-- 保存 req_date / req_seq_id / pre_order_id
        v
汇付托管收银台 ----------> 支付宝 / 微信等支付环境
        |
        | 2a. 页面回跳（仅用于体验，不改最终状态）
        | 2b. 异步通知（验签、校验、幂等）
        v
DAZZY 支付状态机 <-------- Celery 主动查询补偿
        |
        | 3. 订单履约完成、退款/争议窗口结束
        v
DAZZY 内部结算 -----> 汇付分账到达人汇付账户 [需产品确认]
        |                          |
        |                          v
        +-------------------- 达人可提现余额
                                   |
                                   v
                         汇付取现 -> 达人结算银行卡
```

## 6. 第一期：H5 收款方案

### 6.1 支付方式选择

**[文档确认]** 汇付托管支付支持 H5/PC 预下单，`pre_order_type=1`；预下单返回 `pre_order_id` 和页面/收银台所需信息。Checkout JS 的职责是展示收银台并把用户操作交给托管支付，前端不得保管密钥或直接调用需要签名的汇付接口。

**[DAZZY 设计]** H5 首期使用“自定义支付页 + Checkout JS”，保留当前微信/支付宝选择界面。后端统一创建预订单，避免为每个前端复制签名逻辑。

### 6.2 前端与服务端接口

#### `POST /api/v1/provider-orders/{order_no}/payment-sessions/`

客户端请求建议：

```json
{
  "channel": "wechat",
  "client": "h5",
  "browser_environment": "wechat|alipay|system"
}
```

服务端不得接收或采信 `amount`、`goods_desc`、付款人 ID。它们分别来自：

- 金额：`ProviderOrderPaymentOrder.payable_amount`，以“分”为存储单位，转换为汇付接口要求的金额字符串。
- 商品说明：由订单的服务名称和脱敏订单号生成，并限制长度。
- 付款人：当前登录用户，且必须等于订单 `customer`。

服务端响应建议：

```json
{
  "payment_no": "POP...",
  "pre_order_id": "...",
  "req_seq_id": "...",
  "req_date": "YYYYMMDD",
  "huifu_id": "...",
  "expires_at": "2026-09-16T12:30:00+08:00",
  "status": "pending_payment"
}
```

Checkout JS 的 `createPreOrder(selectedType)` 至少需要从商户后端取得 `pre_order_id`、`req_seq_id`、`huifu_id`、`req_date`，这是[预下单返回契约](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/checkout-js-create-preorder-contract.md)明确的最小集合。

#### `GET /api/v1/provider-orders/{order_no}/payment-status/`

只返回 DAZZY 已确认的状态，供页面回跳、App 恢复和短时轮询使用。前端不得自行解释汇付状态码。

建议响应：

```json
{
  "payment_no": "POP...",
  "status": "pending_payment|confirming|paid|closed|failed|partially_refunded|refunded",
  "paid_at": null,
  "order_status": "pending_payment"
}
```

### 6.3 预下单请求映射

| 汇付字段 | DAZZY 来源 | 规则 |
|---|---|---|
| `req_date` | 服务端当前业务日期 | 与 `req_seq_id` 一起持久化，后续查询复用 |
| `req_seq_id` | 服务端唯一请求流水 | 全局唯一；同一幂等请求重试时复用，不重复创建业务支付单 |
| `huifu_id` | 服务端配置 | 仅服务端可见 |
| `pre_order_type` | 运行环境/所选渠道 | 普通 H5/PC 为 `1`；系统浏览器跳支付宝/微信时按官方 JS SDK环境策略使用 `2/3` |
| `trans_amt` | 数据库应付金额 | 客户端金额一律忽略；转换时不得使用浮点数 |
| `goods_desc` | 订单快照 | 不含用户手机号、身份证等敏感信息 |
| `notify_url` | 固定服务端 HTTPS 地址 | 不带查询参数、不重定向 |
| `callback_url` | 固定 H5 回跳地址 | 只用于界面恢复，不表示支付成功 |
| `hosting_data` | 服务端 JSON 序列化字符串 | H5/PC 含 `project_id`、`project_title` |
| `fee_split_flag` | 分账产品配置 | 是否及何时启用，须先完成汇付产品确认 |

`hosting_data`、`biz_info`、`miniapp_data`、`app_data` 等字段在 SDK 中是“JSON 字符串字段”，不能把语言对象直接塞入请求；该约束见[Python 适配说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-python-adapter.md)。

### 6.4 浏览器环境策略

依据汇付官方[斗拱收银台 JavaScript SDK 说明](https://paas.huifu.com/bbs/topic/28/%E6%B1%87%E4%BB%98%E6%94%AF%E4%BB%98%E6%96%97%E6%8B%B1%E6%94%B6%E9%93%B6%E5%8F%B0-javascript-%E8%AF%AD%E8%A8%80-sdk-dg-js-sdk)：

- 微信内浏览器只展示微信支付。
- 支付宝内浏览器只展示支付宝。
- 普通系统浏览器可展示微信和支付宝；微信通常跳托管小程序，支付宝调起支付宝。
- PC 可按收银台能力展示二维码。

汇付资料中曾出现 `dg-element`、`dg-js-sdk` 与较新资料中的 `@dg-elements/js-sdk` 三种包名。**实施前必须由汇付确认当前租户对应的包名和稳定版本，并在 `package.json` 精确锁定；不能根据旧示例猜包。**

### 6.5 前端支付流程

1. 页面加载订单并检查仍是 `pending_payment`。
2. 用户选择渠道；H5 检测微信/支付宝/系统浏览器，仅把环境枚举传给后端。
3. 前端调用 DAZZY `payment-sessions`。
4. 将后端返回的四个必要字段交给锁定版本的 Checkout JS。
5. SDK 回调或 `callback_url` 返回后，页面显示“正在确认支付”，调用 DAZZY `payment-status`。
6. 短轮询采用退避策略，例如 1s、2s、3s、5s；到达前端时限后提示用户稍后在订单页查看，不判失败。
7. 只有 DAZZY 后端返回 `paid` 才跳转支付成功页。

汇付明确要求[Checkout JS 回调不能作为最终支付状态](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/checkout-js-callback-and-confirmation.md)。

## 7. 支付最终状态、通知和查询补偿

### 7.1 支付异步通知入口

建议独立路径：

```text
POST /api/v1/payments/huifu/notify/payment/
```

处理顺序必须固定：

1. 保存原始请求体的安全摘要和接收时间，不记录完整身份证、卡号、密钥。
2. 使用汇付 API 通知 RSA 公钥验证签名；验证对象保持官方要求的原始业务数据格式，不自行重新排序。
3. 解析支付通知使用的业务载荷（官方资料说明通常为 `resp_data`）。
4. 校验 `huifu_id`、请求流水、支付单号、金额、币种/产品（接口提供时）与本地记录完全匹配。
5. 对事件唯一键加数据库唯一约束，在事务内锁定支付单。
6. 只允许合法状态迁移；已 `paid` 的重复成功通知只返回成功，不重复推进订单、发消息或记账。
7. 提交事务后触发订单通知等副作用。
8. 按汇付规范返回 HTTP 200，响应体为 `RECV_ORD_ID_` 加该请求流水号。

汇付[异步通知规范](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-async-notify.md)说明：通知采用 UTF-8 POST，默认超时约 5 秒，超时和部分服务端错误会重试；通知地址不应重定向或携带查询参数。因此通知处理必须短、幂等，耗时工作放 Celery。

### 7.2 通知与 Webhook 不得混用

- 交易异步通知：RSA 验签，按 `RECV_ORD_ID_...` 应答。
- 控制台 Webhook：端点密钥和原始 body 的 MD5 规则，成功应答是任意 2xx。

两者签名和应答完全不同，必须使用不同 URL、解析器和事件表类型。依据见[Webhook 签名说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-webhook-signing.md)。首期交易闭环使用 `notify_url`，Webhook 只在明确需要平台事件时再启用。

### 7.3 主动查询补偿

支付查询使用 `/v2/trade/hosting/payment/queryorderinfo`。官方说明支持：

- `party_order_id`；或
- `huifu_id + org_req_date + org_req_seq_id`。

因此 `req_date` 和 `req_seq_id` 必须入库，不能只保留 `pre_order_id`。

Celery 任务建议：

- 新预订单在 15s、30s、1m、3m、5m 查询，直到终态或支付有效期结束。
- 前端回跳但本地未确认时，立即投递一次高优先级查询。
- 通知解析失败、状态不明确时进入人工可观察的重试队列。
- 超时仍未支付：先查询，再调用关闭接口；不能仅按本地时钟直接关闭。

具体 `order_stat`、`trans_stat`、`close_stat` 枚举必须按当前接口字段表建立显式映射，未知枚举进入告警而不是默认成功/失败。

## 8. 支付、退款与订单状态机

### 8.1 支付状态

```text
CREATED
  -> PREORDERING
  -> PENDING_PAYMENT
  -> CONFIRMING
  -> PAID

PENDING_PAYMENT -> CLOSING -> CLOSED
任意非终态 -> FAILED（仅明确不可恢复错误）
PAID -> PARTIALLY_REFUNDED -> REFUNDED
```

订单只在支付单从非成功态首次进入 `PAID` 时，原子迁移：

```text
ProviderOrder.PENDING_PAYMENT -> ProviderOrder.PENDING_ACCEPTANCE
```

需要使用 `select_for_update()` 或条件更新防止异步通知和查询任务同时重复推进。

### 8.2 退款

退款入口继续复用现有 `ProviderOrderRefundOrder.idempotency_key`。真实网关应实现：

1. 校验原支付已成功、累计退款不超过实付金额。
2. 在事务内创建本地退款单并冻结可退款额度。
3. 事务提交后调用官方托管退款请求类。
4. 同步响应只更新为 `PROCESSING`；根据退款通知/退款查询更新终态。
5. 退款通知使用独立入口，例如：

```text
POST /api/v1/payments/huifu/notify/refund/
```

官方[托管支付异步说明](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-async-webhook.md)指出支付通知与退款通知的业务载荷结构可能不同（支付常见 `resp_data`，退款可能为 `data`），不能共用一个“猜字段”的解析器。

如果已向达人分账后发生退款，需要按汇付分账/退分账产品规则处理；在规则未确认前，订单争议冻结期内不应把对应款项变成可提现。

## 9. 第二期：Android App 支付方案

### 9.1 可落地的基线方案

当前 `dazzy_app` 使用 uni-app App-Plus。用户给出的 SDK 页面和当前公开资料明确的是托管支付及 JavaScript 收银台，没有给出 Android 原生 SDK 的 Maven 坐标、Activity/回调协议和签名要求。

因此 Android 首版采用：

1. App 向 DAZZY 后端创建 App 支付会话。
2. 后端返回一次性 HTTPS 收银台 URL/会话标识。
3. App 使用**系统浏览器**打开 H5 收银台，避免先假定内嵌 WebView 能正确处理支付宝/微信跳转、Cookie 和返回链路。
4. 普通浏览器选择支付宝时按汇付资料使用相应预订单类型 `2`；选择微信时使用类型 `3` 并配置官方要求的 `app_schema`/`miniapp_data`（最终字段以当前文档为准）。
5. 汇付 `callback_url` 回到 DAZZY H5 中转页；中转页可尝试受控 Deep Link/Universal Link 返回 App。
6. App 回到前台后只调用 DAZZY `payment-status`；即使 Deep Link 没回来，订单页重新打开仍可恢复结果。

此策略复用已经验收的 H5 支付核心，最终结果仍由通知/查询闭环，不依赖 Android Activity 回调。

### 9.2 上线前必须验证

- 微信、支付宝是否允许当前商户产品从 Android 系统浏览器跳转。
- Android 各主流浏览器对返回 Scheme/App Link 的限制。
- 汇付托管页对 uni-app WebView 的官方支持结论；未确认前不放进内嵌 WebView。
- 用户取消支付、App 被杀、切换账号、重复点支付时的恢复行为。
- Android 包名、签名证书摘要、应用 Scheme 是否需要在汇付/支付渠道备案。

### 9.3 原生 SDK 作为后续可选项

**[需汇付确认]** 如果汇付能提供当前产品适用的 Android 原生 SDK，应先取得：官方仓库/制品地址、校验值、最低 Android 版本、混淆规则、隐私清单、回调协议、渠道备案要求和升级策略。确认前不在方案中虚构依赖或方法名。

## 10. 第三期：达人开户、分账与提现

### 10.1 先区分四个概念

1. **DAZZY 实名认证**：平台自己的达人身份审核。
2. **汇付个人用户注册/业务入驻**：汇付侧建立资金账户和业务能力。
3. **分账/结算入汇付余额**：订单资金按规则进入达人汇付账户。
4. **取现**：达人把汇付账户可用余额转到结算银行卡。

四者不能用一个 `identity_status` 或 `SETTLED` 字段代替。

### 10.2 达人汇付入驻流程

建议门槛：DAZZY `identity_status=VERIFIED` 后，才允许发起汇付个人用户注册；汇付审核成功后再发起用户业务入驻和结算卡配置。

```text
DAZZY 实名通过
 -> 汇付个人用户基本信息注册
 -> 查询/异步确认注册结果
 -> 汇付用户业务入驻
 -> 配置结算卡、自动结算或取现能力
 -> 达人资金账户 ACTIVE
```

汇付培训资料说明，用户侧需要“个人用户基本信息注册 + 用户业务入驻”，结算/取现配置包含相应 `settle_config`/`cash_config` 与卡信息。参考[开发者培训和考核文档](https://paas.huifu.com/common-web/ossApi/assessment/%E5%BC%80%E5%8F%91%E8%80%85%E5%9F%B9%E8%AE%AD%E5%92%8C%E8%80%83%E6%A0%B8%E6%96%87%E6%A1%A3.html)。

敏感信息处理：

- 优先让敏感开户/绑卡信息直达汇付托管页或按汇付要求加密传输。
- DAZZY 只保留汇付用户 ID、申请流水、审核状态、卡号后四位/掩码和必要审计信息。
- 不把完整身份证号、银行卡号、照片 URL、请求原文写进普通日志、监控标签或异常上报。

### 10.3 订单资金怎样进入达人账户

**推荐方案 [需汇付产品确认]：订单交易分账到达人汇付用户余额。**

DAZZY 订单包含平台佣金、达人服务收入、交通费、其他费用和退款分配快照，天然适合用汇付分账能力表达：

```text
用户实付
 = 已退款
 + 平台佣金
 + 达人可结算金额
```

但“在支付时分账、履约完成后确认分账、冻结期结束后分账”具体采用哪一接口和资金冻结能力，必须由汇付根据 DAZZY 的陪玩/陪伴服务场景、产品资质和退款时点确认。未取得书面确认前：

- 不把平台自有银行账户收款后再私下转账给达人设计成默认方案；这可能形成合规和二清风险。
- 不把本地 `ProviderOrderSettlement.SETTLED` 当成汇付侧资金已到达人余额。
- 不允许对应款项提现。

### 10.4 自动结算与手动取现

汇付[结算/取现 FAQ](https://paas.huifu.com/bbs/topic/39/%E7%BB%93%E7%AE%97-%E5%8F%96%E7%8E%B0%E5%B8%B8%E8%A7%81%E9%97%AE%E9%A2%98faq)区分：

- 自动结算：按配置的周期、方式、最低/留存金额自动把余额结算到银行卡。
- 手动取现：客户主动选择金额、日期和银行卡发起余额出金。

FAQ 给出的常见类型包括 T1、D1、DM、D0；自动结算通常是 T1/D1，D0 有额外准入条件。实际支持类型、手续费、限额、到账时间和节假日规则必须以 DAZZY 商户产品开通结果为准。

**[DAZZY 设计]** 达人端需要“提现”体验，因此建议关闭或保留极低频的自动结算，开通手动取现；否则余额可能被自动划走，达人端显示的可提现余额会与实际不一致。该选择必须在开户产品参数中统一，不能由前端临时决定。

### 10.5 已确认的提现手续费策略

以下为 **[DAZZY 已确认业务规则]**，不是对汇付合同费率的推断：

| 配置项 | DAZZY 默认值/规则 |
|---|---|
| 提现费率 | `0.35%`，数据库以 `35 basis points` 保存 |
| 费率管理 | 可在 DAZZY 管理后台设置，采用不可变版本和生效时间 |
| 手续费承担方 | `provider`（达人）或 `platform`（平台） |
| 扣款方式 | `internal`（内扣）或 `external`（外扣） |
| 推荐组合一 | 达人承担 + 内扣：手续费从申请提现金额中扣除 |
| 推荐组合二 | 平台承担 + 外扣：达人按申请金额到账，手续费从平台手续费账户另扣 |
| 失败取现 | 不收费；本金与预计手续费全部解除冻结，最终手续费记为 `0` |
| 发票 | 支持发票申请；开票主体、抬头和金额按真实收费关系确定 |

管理后台不开放逻辑冲突的“平台承担 + 内扣”组合。若未来开放“达人承担 + 外扣”，必须先确认达人手续费扣款账户、余额不足处理和汇付实际配置，首期不启用。

费率配置采用版本化模型：

```text
rate_bps = 35
fee_bearer = provider | platform
deduction_mode = internal | external
min_fee_amount / max_fee_amount
rounding_mode
effective_from / effective_to
version / enabled
```

每笔提现创建时保存策略快照，后台调整只影响新提现单，不得重算历史单。金额计算全程使用整数分或 `Decimal`，禁止使用二进制浮点数。默认 `0.35%` 的展示例子：达人承担且申请提现 `¥1,000.00` 时，预计手续费 `¥3.50`、预计到账 `¥996.50`；平台承担时达人预计到账仍为 `¥1,000.00`。

DAZZY 后台策略与汇付真实计费是两层配置：DAZZY 决定产品展示、承担关系和内部账务；汇付合同/控台决定渠道实际费率、外扣账户及取现权限。两者必须在上线前人工核对。汇付分账查询会返回 `split_fee_amt` 和 `split_fee_huifu_id`，实际账单必须与本地策略逐笔对账；参考[交易分账明细查询](https://paas.huifu.com/partners/api/doc/smzf/api_fzmxcx.md)。汇付资金运营资料也要求在取现失败时检查费率、外扣规则和手续费扣款户余额；参考[分账/结算取现 FAQ](https://paas.huifu.com/bbs/category/11/%E5%88%86%E8%B4%A6-%E4%BD%99%E9%A2%9D%E6%94%AF%E4%BB%98-%E7%BB%93%E7%AE%97%E5%8F%96%E7%8E%B0)。

发票按真实收费主体处理：若渠道手续费由汇付向 DAZZY 收取，则申请汇付开具给签约主体；若 DAZZY 另行向达人收取平台服务费，则由 DAZZY 按财税确认的项目和金额开具。系统需要保存发票申请状态，但不能把“支持申请”误写成任意主体均可互相开票。汇付提供商户手续费发票申请入口，具体规则仍由财务与汇付确认；参考[汇付手续费发票入口](https://paas.huifu.com/bbs/category/4/%E5%B8%B8%E8%A7%81%E9%97%AE%E9%A2%98)。

### 10.6 提现流程

```text
达人提交金额
 -> 校验账户 ACTIVE、卡已绑定、可提现余额、限额、冻结/风控
 -> 本地创建 withdrawal_no，原子冻结余额
 -> 官方 SDK 发起汇付取现
 -> 同步受理：PROCESSING（不是到账）
 -> 异步通知/主动查询确认
 -> SUCCEEDED：按汇付实际结果确认手续费，扣减冻结金额并登记实际到账金额
 -> FAILED：释放本金和预计手续费，最终手续费记为 0，并记录可展示原因
```

建议 API：

```text
GET  /api/v1/provider/wallet/
GET  /api/v1/provider/wallet/transactions/
POST /api/v1/provider/withdrawals/
GET  /api/v1/provider/withdrawals/{withdrawal_no}/
```

`POST` 请求只接受金额和已绑定卡引用，不接受任意卡号：

```json
{
  "amount": 50000,
  "settlement_card_id": "card_reference"
}
```

金额仍使用整数分；服务端校验最低/最高/单日次数/手续费后，再转换为汇付接口格式。提现页需明确显示预计手续费、预计到账金额、预计到账时效和当前处理状态。

### 10.7 提现幂等与并发

- 客户端提交使用 `Idempotency-Key`，服务端建立唯一约束。
- 冻结余额和创建提现单在同一数据库事务内，锁定钱包行。
- 可提现余额必须按“已确认汇付入账 - 已成功/已冻结提现 - 风险/争议冻结”计算。
- 同一提现单重试复用汇付请求流水，禁止生成第二笔出金。
- `SUCCEEDED/FAILED` 终态通知重复到达时只应答，不重复记账。
- 提现申请阶段只冻结预计费用，不确认收入或成本；只有查询/通知确认成功后才登记实际手续费。
- 失败终态必须以只追加台账释放本金和预计手续费，不直接覆盖原冻结流水。
- 若汇付实际账单与本地 `0.35%` 策略不一致，提现单记录真实金额并生成对账差异，禁止静默修改历史策略。
- 提现成功后退款/追偿的业务规则须单独建立负余额和追偿策略，不能静默冲销其他达人资金。

## 11. 建议的数据模型改造

### 11.1 支付相关

#### 扩展 `ProviderOrderPaymentOrder`

- `gateway = huifu`
- `req_date`
- `req_seq_id`（唯一或与网关联合唯一）
- `pre_order_id`
- `huifu_id`
- `party_order_id`（若使用）
- `gateway_status_code`
- `gateway_failure_code/message`（message 需脱敏）
- `last_queried_at`
- `confirmed_source = notify|query`
- `raw_response_digest`

#### 扩展 `ProviderOrderRefundOrder`

- 原请求日期/流水
- 汇付退款流水
- 网关状态/失败码
- 最近查询时间
- 确认来源

#### 新增 `HuifuNotifyEvent`

- `event_type = payment|refund|split|withdrawal`
- `event_key` 唯一
- `req_seq_id`
- `signature_verified`
- `payload_digest`
- `processing_status`
- `attempt_count`
- `received_at/processed_at/error_code`

保留摘要和必要审计字段，不默认存储完整敏感 payload。

### 11.2 达人资金相关

#### `ProviderHuifuAccount`

- `provider` 一对一
- `huifu_user_id`
- `basic_registration_status`
- `business_open_status`
- `settlement_card_ref/card_masked`
- `cash_config_status`
- `last_gateway_code`
- 申请/审核时间

#### `ProviderFundLedger`（只追加，不修改历史）

- `ledger_no`
- `provider`
- `entry_type = split_credit|withdrawal_hold|withdrawal_release|withdrawal_debit|refund_debit|adjustment`
- `amount`（有符号整数分）
- `available_delta/frozen_delta`
- `source_type/source_id`
- `gateway_reference`
- `created_at`

#### `ProviderWalletSnapshot`

- `available_amount`
- `frozen_amount`
- `version`（乐观锁）
- 余额必须可由台账重放校验。

#### `WithdrawalFeePolicy`

- `version` 唯一且不可变
- `rate_bps`，默认 `35`
- `fee_bearer = provider|platform`
- `deduction_mode = internal|external`
- `min_fee_amount/max_fee_amount`
- `rounding_mode`
- `effective_from/effective_to/enabled`
- `created_by/created_at`

策略修改必须新增版本并记录审计日志，不能原地覆盖已被提现单引用的版本。

#### `ProviderWithdrawalRequest`

- `withdrawal_no`
- `idempotency_key`
- `provider/account/card_ref`
- `amount/estimated_fee/actual_fee/net_amount`
- `fee_policy_version/fee_rate_bps/fee_bearer/deduction_mode` 快照
- `cash_type`
- `status = created|submitting|processing|succeeded|failed|cancelled`
- `req_date/req_seq_id/gateway_withdrawal_no`
- `failure_code/failure_message`
- `invoice_status = not_requested|requested|issued|rejected`
- `invoice_request_id`（如有）
- `submitted_at/succeeded_at/failed_at`

#### 拆分 `ProviderOrderSettlement` 语义

至少新增：

- `internal_credited_at`：平台内部结算完成。
- `huifu_split_status`：未提交/处理中/成功/失败。
- `huifu_split_req_date/req_seq_id/reference`。
- `withdrawable_at`：汇付到账且冻结条件满足后才设置。

如允许调整枚举，建议把现有 `SETTLED` 改名为 `INTERNAL_SETTLED`，避免运营误解。

## 12. 服务端模块结构

```text
payments/
  domain/
    states.py
    money.py
  gateways/huifu/
    client.py          # 官方 dg-sdk 的薄适配层
    payment.py
    refund.py
    onboarding.py
    split.py
    withdrawal.py
    notify.py
    status_mapping.py
  services/
    payment_service.py
    refund_service.py
    settlement_service.py
    withdrawal_service.py
  tasks.py
  models.py
  views.py
```

`client.py` 只负责 SDK 配置、调用和统一错误对象；业务状态机留在 service 层，避免把汇付状态码散落在 view/model 中。

官方 Python SDK 的当前公开基线在汇付资料中为 `dg-sdk`（导入名 `dg_sdk`），示例请求类包括托管预下单、查询、关闭、退款、退款查询和对账；参考[SDK 矩阵](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-server-sdk-matrix.md)与[官方 Python SDK 仓库](https://github.com/huifurepo/dg-python-sdk)。

### Python 3.12 兼容性门槛

当前 `dazzy_api` 要求 Python `>=3.12,<3.13`。公开 SDK 资料对较新 Python 版本的声明不充分，因此编码前先做隔离验证：

1. 在 Python 3.12 虚拟环境固定候选 `dg-sdk` 版本。
2. 验证安装、导入、配置对象和所有需要的 Request 类。
3. 跑一条联调工具生成的预下单测试请求。
4. 若不兼容，向汇付索取兼容版本；不得降级主项目 Python 或改成手写签名作为临时上线方案。

## 13. 密钥与安全方案

### 13.1 所需配置

服务端至少需要：

- `HUIFU_SYS_ID`
- `HUIFU_PRODUCT_ID`
- `HUIFU_MERCHANT_ID/HUIFU_ID`
- `HUIFU_RSA_PRIVATE_KEY`
- `HUIFU_RSA_PUBLIC_KEY`
- `HUIFU_PROJECT_ID`
- `HUIFU_PROJECT_TITLE`
- `HUIFU_NOTIFY_BASE_URL`
- `HUIFU_CALLBACK_BASE_URL`
- SDK 环境（联调/生产）

具体获取步骤以[公私钥获取文档](https://paas.huifu.com/docs/start/#/kfjr/hqmy)为准。

### 13.2 管理要求

- 本地 `.env.example` 只写变量名，不放真实值；真实密钥使用部署平台 Secret/KMS。
- 私钥不得返回前端、写数据库、打印异常或上传错误追踪。
- 汇付公钥与商户私钥分环境管理，测试与生产不可复用。
- 建立密钥版本号和轮换流程；轮换窗口需支持旧通知验签，具体双公钥机制与汇付确认。
- 管理后台只显示配置是否完整和公钥指纹，不显示密钥内容。
- 所有支付/提现管理操作写不可篡改审计日志，并限制到最小权限角色。

## 14. 幂等键设计

| 动作 | DAZZY 幂等键 | 防止的问题 |
|---|---|---|
| 创建支付会话 | `order_id + payment_attempt_version` | 双击造成多笔有效预订单 |
| 支付通知 | 网关事件标识；无独立事件号时使用业务流水+状态+交易号摘要 | 重复推进订单 |
| 支付查询确认 | `payment_id + target_terminal_status` | 查询与通知并发重复入账 |
| 退款 | 现有 `ProviderOrderRefundOrder.idempotency_key` | 重复退款 |
| 分账 | `settlement_no` | 一笔达人收入重复分账 |
| 提现 | 客户端 `Idempotency-Key` + `withdrawal_no` | 双击/网络重试重复出金 |
| 通知副作用 | `event_id + handler_name` | 消息、积分等重复执行 |

## 15. 对账与可观测性

### 15.1 日终对账

官方 SDK 资料提供对账文件查询请求类。每天按汇付可用时间拉取：

- 支付交易；
- 退款；
- 分账/结算（产品支持时）；
- 提现与手续费。

生成差异类型：本地有/汇付无、汇付有/本地无、金额不一致、状态不一致、手续费不一致。差异不得自动“猜测修复”，先进入运营工单；明确可补偿的只允许重新查询后按状态机处理。

### 15.2 监控指标

- 预下单成功率和 P95/P99 延迟。
- 支付通知验签失败数、金额不匹配数、未知状态码数。
- `PENDING_PAYMENT/CONFIRMING` 超时数量。
- 查询补偿成功率。
- 退款/分账/提现处理中超时数量。
- 每日账实差异总额。
- 同一订单创建多预订单的异常计数。

日志关联键统一使用：`order_no`、`payment_no`、`req_date`、`req_seq_id`、`pre_order_id`、`gateway_trade_no`；不记录私钥、完整卡号、证件号或原始签名材料。

## 16. 联调和测试计划

### 16.1 汇付联调工具

使用官方[API 联调工具](https://paas.huifu.com/open/doc/devtools/#/tools_apiltgj)完成并保存脱敏样例：

1. H5 预下单成功/参数错误。
2. 微信、支付宝不同运行环境。
3. 支付成功、失败、取消、超时关闭。
4. 通知重放、乱序、错误签名、金额不一致。
5. 查询早于通知、通知早于查询、两者并发。
6. 全额退款、部分退款、重复退款、退款处理中。
7. 达人注册/业务入驻成功与驳回。
8. 分账成功/失败/重试和退款关联。
9. 取现受理、成功、失败、重复提交、余额不足、手续费不足。
10. 默认 `0.35%`、达人内扣、平台外扣、费率版本切换及历史提现快照不变。
11. 失败取现最终手续费为 `0`，本金和预计手续费均正确解冻。
12. 手续费发票申请状态与实际收费主体匹配。
13. 对账文件下载和四类差异。

### 16.2 自动化测试

- 单元测试：金额转换、状态映射、签名适配器边界、通知 ACK、敏感字段脱敏。
- 数据库并发测试：通知与查询同时确认、两次提现同时冻结余额。
- 契约测试：用官方联调样例校验 SDK 请求/响应序列化。
- H5 E2E：微信/支付宝/系统浏览器环境矩阵。
- Android 真机：Chrome、厂商浏览器、支付宝/微信安装与未安装、App 被杀/恢复。
- 回归：现有下单、取消、超时、售后、退款、达人结算流程。

禁止在单元测试中调用真实生产网关；网关适配器使用录制后脱敏的契约 fixture。

## 17. 分阶段实施与验收

### 阶段 0：商务/产品和密钥准备

- 确认商户主体、行业和 H5 支付产品已开通。
- 获取 `sys_id/product_id/huifu_id/project_id` 和测试/生产密钥。
- 确认允许的支付渠道、域名、回调域名和结算规则。
- 书面确认达人用户入驻、订单分账、余额查询、手动取现产品链路。
- 确认平台抽佣、退款后退分账及争议冻结是否被产品支持。

**验收**：汇付提供产品清单、接口清单、费率/限额、测试账号和技术联系人。

### 阶段 1：H5 支付闭环

- SDK 兼容性 spike。
- 数据库迁移、真实 Huifu gateway、预下单/查询/关闭。
- H5 Checkout JS。
- 支付通知、查询补偿、支付状态页。
- 灰度开关保留 Mock 与 Huifu 双适配器，但同一订单只绑定一个网关。

**验收**：前端关闭/刷新/重复点击/通知延迟均不导致重复支付或错误发单；支付成功只能由服务端确认。

### 阶段 2：退款与对账

- 全额/部分退款和退款查询。
- 日终对账、差异工单、运营后台。

**验收**：累计退款不超实付；本地、汇付和订单金额守恒。

### 阶段 3：Android

- 系统浏览器收银台、App Link 回跳、前台恢复查询。
- 真机渠道矩阵。

**验收**：不依赖回跳也能在订单页恢复最终状态；App 被杀不丢单。

### 阶段 4：达人账户与分账

- 汇付个人用户注册/业务入驻/结算卡。
- 订单分账及分账查询。
- 内部结算与汇付资金状态解耦。

**验收**：达人可提现余额只包含汇付已确认入账且解除冻结的资金。

### 阶段 5：达人提现

- 钱包、只追加台账、余额冻结、取现、查询/通知、手续费。
- 提现费率策略后台：默认 `0.35%`，不可变版本、生效时间、承担方和扣款方式。
- 首期只开放“达人承担 + 内扣”和“平台承担 + 外扣”两个有效组合。
- 失败取现手续费归零、资金解冻；手续费发票申请和收费主体校验。
- 风控限额、后台审核策略（若业务要求）和日终资金核对。

**验收**：并发提交不超提；失败自动解冻且手续费为 `0`；历史提现不受后续费率调整影响；成功金额、实际手续费、承担方、扣款账户、发票状态和银行卡到账记录可追踪；本地费率与汇付账单不一致时自动生成差异告警。

## 18. 上线前待汇付书面确认清单

1. DAZZY 商户当前应使用的 Checkout JS 正式包名、版本和 SRI/制品校验方式。
2. H5 微信/支付宝在普通浏览器、微信内、支付宝内的 `pre_order_type` 与必要字段最终矩阵。
3. Android 是否有官方原生 SDK；若有，提供完整接入资料和适用产品。
4. 陪伴/陪玩业务是否允许“履约完成后分账”，对应接口、冻结/解冻和退分账规则。
5. 达人作为个人用户的开户注册、业务入驻、银行卡鉴权和实名材料范围。
6. 手动取现支持的 `cash_type`、单笔/单日限额、到账时间和节假日规则。
7. DAZZY 默认 `0.35%` 与汇付合同实际费率的对应关系，以及达人内扣/平台外扣时分别使用的手续费扣款账户。
8. 汇付确认失败取现不收费；如出现已扣费后失败，明确退费时点、通知/查询字段和对账表现。
9. 手续费发票的开票主体、抬头、可开金额、开票项目、周期及申请入口。
10. 自动结算与手动取现能否共存；DAZZY 采用手动提现时的推荐配置。
11. 支付、退款、分账、提现各通知的完整验签字段、ACK 和重试周期。
12. 测试与生产网关、IP 白名单、回调端口/域名、证书要求。
13. `dg-sdk` 在 Python 3.12 的官方支持版本。

任何一项未确认都不能靠旧博客、示例密钥或逆向行为直接投入生产。

## 19. 实施任务拆分建议

1. `PAY-001`：汇付产品/接口确认与密钥准备。
2. `PAY-002`：Python 3.12 SDK 兼容性验证。
3. `PAY-003`：支付/通知事件数据迁移。
4. `PAY-004`：Huifu gateway 与状态映射。
5. `PAY-005`：预下单、状态查询和关闭 API。
6. `PAY-006`：H5 Checkout JS 与结果确认页。
7. `PAY-007`：异步通知验签、幂等和查询补偿。
8. `PAY-008`：退款/退款查询。
9. `PAY-009`：日终对账和运营告警。
10. `PAY-010`：Android 系统浏览器支付与 App Link。
11. `FUND-001`：达人汇付账户/业务入驻。
12. `FUND-002`：订单分账和内部结算状态拆分。
13. `FUND-003`：钱包与只追加台账。
14. `FUND-004`：提现、余额冻结和异步查询。
15. `FUND-005`：达人资金对账与后台处置。

## 20. 关键官方依据索引

- 汇付支付集成总则：[Huifu Pay Integration Skill](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/SKILL.md)
- 托管预下单：[Hosting Pay Preorder](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-preorder.md)
- H5 本地联调字段基线：[Local Sandbox Final Plan](https://github.com/huifurepo/dg-payment-skills/blob/main/LOCAL_SANDBOX_FINAL_PLAN.md)
- Checkout JS：[Checkout JS](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/checkout-js.md)
- 预下单前后端契约：[Create Preorder Contract](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/checkout-js-create-preorder-contract.md)
- 前端回调与最终确认：[Callback and Confirmation](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/checkout-js-callback-and-confirmation.md)
- 前端 SDK 矩阵：[Frontend SDK Matrix](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-frontend-sdk-matrix.md)
- Python SDK 适配：[Python Adapter](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-python-adapter.md)
- 服务端 SDK 矩阵：[Server SDK Matrix](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-server-sdk-matrix.md)
- 支付查询：[Payment Status Query](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-query-payment-status-query.md)
- 异步通知：[Async Notify](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-async-notify.md)
- 支付/退款通知差异：[Hosting Pay Async](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/hostingpay-async-webhook.md)
- Webhook 签名：[Webhook Signing](https://github.com/huifurepo/dg-payment-skills/blob/main/huifu-pay-integration/references/shared-webhook-signing.md)
- 官方 Python SDK：[dg-python-sdk](https://github.com/huifurepo/dg-python-sdk)
- JS 收银台官方论坛说明：[斗拱收银台 JavaScript SDK](https://paas.huifu.com/bbs/topic/28/%E6%B1%87%E4%BB%98%E6%94%AF%E4%BB%98%E6%96%97%E6%8B%B1%E6%94%B6%E9%93%B6%E5%8F%B0-javascript-%E8%AF%AD%E8%A8%80-sdk-dg-js-sdk)
- 结算与取现 FAQ：[结算/取现常见问题](https://paas.huifu.com/bbs/topic/39/%E7%BB%93%E7%AE%97-%E5%8F%96%E7%8E%B0%E5%B8%B8%E8%A7%81%E9%97%AE%E9%A2%98faq)

---

下一步进入汇付联调环境：注入测试商户配置、把 `HUIFU_CALLBACK_URL` 指向 H5 `/#/pages/booking/payment-result`、把 `HUIFU_NOTIFY_URL` 指向后端支付通知端点，然后用官方联调工具完成真实预下单、支付通知和主动查单三方核对。退款、关单和对账完成前仍不进入生产灰度。
