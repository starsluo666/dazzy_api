# DAZZY 汇付聚合支付迁移说明

> 版本：V1.0
> 日期：2026-09-18
> 当前范围：微信服务号 H5 `T_JSAPI`；Android `T_APP` 保留后端扩展点，尚未开放客户端

## 1. 当前结论

DAZZY 保留现有订单、支付单、退款单、状态机、通知事件表和支付成功业务动作，
仅把支付网关从托管支付替换为 V4 聚合支付。

当前服务号 H5 使用：

- 聚合下单端点：`POST /v4/trade/payment/create`
- `trade_type=T_JSAPI`
- Python SDK：`dg-sdk==2.0.24`
- SDK 入口：`dg_sdk.Payment.create(PaymentCreateRequest)`
- 前端调起依据：同步响应 `pay_info`
- 最终状态：接口异步通知验签、幂等处理后，再通过
  `dg_sdk.Payment.query(PaymentQueryRequest)` 主动查单确认

不再使用：

- `HUIFU_PROJECT_ID`
- 托管预下单 `pre_order_id`
- 托管收银台 `jump_url`
- Checkout JS
- `HUIFU_CALLBACK_URL`

旧托管字段通过正向迁移移除，不参与聚合支付交易。

## 2. 前后端契约

### 2.1 获取服务号授权状态

```http
GET /api/v1/provider-orders/{order_no}/payment-authorization/
```

已绑定当前服务号 OpenID：

```json
{
  "data": {
    "authorized": true,
    "authorize_url": ""
  }
}
```

未绑定时返回后端生成的微信网页授权地址。授权状态 `state` 由 Django 签名，
绑定时再次校验用户、订单、订单状态和有效期；AppSecret 只存在服务端环境变量。

### 2.2 创建聚合支付会话

```http
POST /api/v1/provider-orders/{order_no}/payment-session/
Content-Type: application/json

{
  "payment_scene": "official_account"
}
```

后端白名单映射：

| `payment_scene` | 汇付 `trade_type` | 客户端调起类型 |
| --- | --- | --- |
| `official_account` | `T_JSAPI` | `WECHAT_JSAPI` |
| `mobile_app` | `T_APP` | `WECHAT_APP` |

当前 H5 响应：

```json
{
  "data": {
    "invoke_type": "WECHAT_JSAPI",
    "pay_info": {}
  }
}
```

前端只把 `pay_info` 交给 `WeixinJSBridge.invoke('getBrandWCPayRequest', ...)`。
前端成功回调只进入支付结果确认页，不直接修改订单状态。

支付结果页通过以下接口触发服务端主动查单，避免异步通知丢失后订单一直停留在待支付：

```http
POST /api/v1/provider-orders/{order_no}/payment-status/
```

接口只允许订单本人访问，并设置独立频率限制。支付超时任务在关闭订单前也会执行一次
主动查单；如果查单暂时不可用，任务进入重试而不会直接关闭可能已经付款的订单。

## 3. 服务号 OAuth 与 OpenID

公众号/服务号 `T_JSAPI` 必须使用与 `sub_appid` 同一应用授权得到的
`sub_openid`。DAZZY 的流程为：

1. 登录用户请求订单授权状态。
2. 后端生成带签名 `state` 的服务号网页授权地址。
3. 微信回调后端 `GET /api/v1/payments/wechat/oauth/callback/`。
4. 后端用 AppID、AppSecret 和一次性 `code` 换取 OpenID。
5. 按 `user + app_id` 保存身份绑定，不保存网页授权 access token。
6. 返回 H5 支付页，再创建聚合支付会话。

OpenID 不由前端自由提交，也不跨服务号复用。

## 4. 服务端下单字段

下单金额、商品说明和失效时间只从本地订单读取。关键字段：

- `req_date`：创建时生成并持久化。
- `req_seq_id`：复用本地支付单号并持久化。
- `huifu_id`：服务端商户配置。
- `trade_type=T_JSAPI`。
- `trans_amt`：服务端应付金额从分转元，两位小数。
- `goods_desc`：订单服务名称。
- `notify_url`：服务端公网 HTTPS 地址，无查询参数或片段。
- `time_expire`：订单支付失效时间。
- `fee_flag=1`：当前“平台承担通道费”的外扣配置；上线前仍需与汇付开通配置人工核对。
- `method_expand`：在业务层建模后一次序列化，包含匹配的
  `sub_appid`、`sub_openid` 和订单号 `attach`。

同步响应保存：`req_date`、`req_seq_id`、`hf_seq_id`、`party_order_id`、
`out_trans_id`、`trade_type`、响应码和响应摘要。`pay_info` 仅作为当前支付会话的
客户端调起参数，不写日志。

## 5. 通知、幂等和终态

通知地址：

```http
POST /api/v1/payments/huifu/notify/
```

处理顺序固定为：

1. 读取原始 `resp_data` 字符串和 `sign`。
2. 使用汇付 RSA 公钥对原始业务数据验签，不重新排序。
3. 严格解析 JSON，校验商户号、请求日期、请求流水，以及通知中实际携带的金额和状态。
4. 使用商户号、请求流水、全局流水、状态等生成事件幂等键。
5. 锁定本地支付单并落通知事件。
6. 使用原交易 `req_date/req_seq_id` 或已落库 `hf_seq_id` 主动查单。
7. 以主动查单结果确认当前状态，并校验金额和交易类型；通知与查单之间允许正常的状态推进。
8. 只在 `trans_stat=S` 时执行一次支付成功业务动作。
9. 核心状态落库后标记通知已处理，并返回
   `RECV_ORD_ID_{req_seq_id}`。

同步 `resp_code`、浏览器回跳和 `WeixinJSBridge` 回调均不属于支付终态。

## 6. 环境变量

生产凭据只能由部署环境或密钥管理服务注入：

```text
HUIFU_PAYMENT_ENABLED
HUIFU_ENV
HUIFU_SYS_ID
HUIFU_PRODUCT_ID
HUIFU_MERCHANT_ID
HUIFU_RSA_PRIVATE_KEY
HUIFU_RSA_PUBLIC_KEY
HUIFU_SKILL_SOURCE
HUIFU_NOTIFY_URL
HUIFU_FEE_FLAG
HUIFU_CONNECT_TIMEOUT_SECONDS

WECHAT_OFFICIAL_ACCOUNT_APP_ID
WECHAT_OFFICIAL_ACCOUNT_APP_SECRET
WECHAT_OFFICIAL_ACCOUNT_OAUTH_CALLBACK_URL
WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL
WECHAT_MOBILE_APP_ID
WECHAT_OAUTH_STATE_MAX_AGE_SECONDS
WECHAT_OAUTH_TIMEOUT_SECONDS
```

私钥、AppSecret、完整 OpenID、原始签名和完整 `pay_info` 禁止进入代码仓库或普通日志。

## 7. 上线前人工确认

- 汇付已为商户开通 `T_JSAPI`，并绑定当前服务号 AppID。
- 微信公众平台网页授权域名与后端 OAuth 回调域名一致。
- 微信支付授权目录覆盖实际 H5 支付页面。
- 通知地址公网 HTTPS 可达、无跳转、无查询参数。
- `fee_flag=1` 与合同及汇付后台实际扣费方式一致。
- 真实环境完成下单、取消支付、支付成功、重复通知、通知缺失查单、金额不一致拒绝等联调。
- Android 开放前补齐移动应用 AppID、汇付 `T_APP` 渠道绑定和原生微信 SDK 调起测试。

## 8. 本轮依据

本轮实现按 `huifu-pay-integration` V1.3.5 的以下资料执行：

- `copilot-existing-system.md`
- `aggregation-order.md`
- `aggregation-order-method-wechat.md`
- `aggregation-python-adapter.md`
- `shared-async-notify.md`

汇付接口字段与渠道开通结果仍以联调时官方开放平台返回和汇付人工确认为准。
