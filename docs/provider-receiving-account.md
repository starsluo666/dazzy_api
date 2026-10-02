# 达人收款资料管理（第一阶段）

## 本次交付范围

在现有 Python/Django + 汇付聚合支付系统上增量增加资料登记。达人端“我的”和“收入明细”均可进入收款账户；后台达人详情对具备达人审核权限的人员显示脱敏资料。

**不是完整分账上线。当前实现不调用汇付开户、绑卡、分账、结算或提现接口。**

- `materials_saved` 只表示平台保存了资料。
- `channel_status` 固定为 `not_connected`，没有可手动改成渠道成功的接口。
- 保留原支付、订单、退款、佣金和账务结算逻辑。原支付请求的 `delay_acct_flag` 仍为 `N`。
- 未增加第四个接单门槛，不阻止现有达人继续履约。
- 不处理、补分或迁移任何历史已付款订单。

## 本地 API

`/api/v1/providers/me/receiving-account/`

- GET：本人资料状态、脱敏号码、非敏感表单项、资料收集说明和版本；不创建记录。
- PUT：必须为审核通过且已完成实名认证的本人；保存前校验证件摘要一致、有效期、号码格式和本版说明同意。编辑时三个敏感字段留空表示保留之前加密值，初次填写不得留空。
- DELETE：清除本人未提交渠道的全部收款资料；收集开关关闭或密钥不可用时仍允许清除。不会删除平台实名认证、订单、资金账务记录。
- 请求按用户限流，响应禁止缓存；无前端持久化表单。

## 安全与启用

默认**禁止收集**。先完成迁移 `providers.0020_providerreceivingaccount`，再在部署使用的服务端环境文件中配置：

```dotenv
PROVIDER_RECEIVING_ACCOUNT_COLLECTION_ENABLED=true
PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY=<独立的32字节随机密钥，经标准Base64编码>
```

这两个参数**只开放资料登记，不会开启真实开户或分账**。没有有效加密密钥时保存返回 503，不能退化成明文。这里没有提供默认密钥；用部署密钥管理系统生成、保管并备份，不要提交仓库、放入前端或发送给客服。

使用 PyCryptodome AES-256-GCM，每次保存使用新的随机 nonce，AAD 绑定达人 ID 与密文版本。完整身份证、银行卡、电话及申请快照只存在于加密内容中。后台和 API 只回显号码掩码。错误日志/监控不得采集该接口的请求体；部署也须检查反向代理、APM、浏览器录屏配置。

密钥丢失不能读取已有资料；不要直接替换密钥，轮换需要先使用旧密钥解密、使用新密钥重新加密并验证，再切换配置。数据库备份也要限制访问并遵循平台保留策略。

账号完成注销时清除这些尚未提交渠道的资料。用户也可自行清除；这不影响已有资金记录。未来接入正式渠道后，渠道账户关闭和法定保留要求需单独设计，不能继续将正式账户当成可删除草稿。

收集说明明确仅用于准备申请；当前同意**不授权未来自动向汇付发送资料或发起扣款/分账**。正式提交前需要再次告知并由达人确认。

## 尚需确认与后续接入

当前未确认商户实际支持的账户等级、结算产品、结算周期、手续费承担和分账比例权限，因此没有用官方示例值猜测开户配置。

确认后另行实现：

1. 服务端持久化开户请求流水，调用官方 SDK `V2UserBasicdataIndvRequest`。
2. 依据实际产品补齐资料、地区官方编码和渠道协议，调用 `V2UserBusiOpenRequest`；本阶段银行省市为用户填写的名称，**不能直接作为汇付编码上送**。
3. 增加签名响应/通知校验、状态查询补偿、请求幂等、超时待核实状态。收到成功受理不能一律展示已开通，更不能展示已到账。
4. 对新订单接入合适的延时分账产品；服务完成、售后冻结期结束后分账，独立记录分账结果与银行结算结果，并处理退款和对账。
5. 历史订单按真实渠道交易状态另行制定方案，不补改本地状态冒充实际资金操作。

渠道资料以官方为准：[个人用户开户](https://paas.huifu.com/partners/api/doc/yhgl/api_yhgl_gryhjbxxzc.md)、[用户业务开通](https://paas.huifu.com/partners/api/doc/yhgl/api_yhgl_ywrz.md)。不从本文推断商户已有生产权限。

## 验证

```sh
uv run python scripts/check_wechat_auth.py providers.test_receiving_accounts accounts.test_account_closure
```

使用全新内存 SpatiaLite 数据库，不加载业务 `.env`、不连接业务数据库、不调用真实汇付。该验证不替代 PostgreSQL 并发测试和真机小程序验证。

前端：`npm run type-check`、`npm run build:h5`、`npm run build:mp-weixin`；后台：`npm run build`。

本轮支付 skill 实际参考：`copilot-existing-system.md`、`copilot-solution-selection.md`、`aggregation-python-adapter.md`。因渠道合同未唯一确认，按 skill 边界暂停真实接口实现，交付本地资料管理；Apple design 用于分组表单、即时反馈、状态区分与统一输入高度。
