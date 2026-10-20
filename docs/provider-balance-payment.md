# 达人余额 / 组合支付渠道结算：受控执行阶段

协议核对日期：2026-10-19；执行阶段离线验证：2026-10-20。现有系统为 Python/Django，采用官方 `dg-sdk==2.0.24`。
本阶段依照 `huifu-pay-integration` 的 `copilot-existing-system.md`、
`shared-server-sdk-matrix.md`、`aggregation-python-adapter.md`、`aggregation-query-payment-query.md` 增量接入；
实际请求签名与响应验签仍由 SDK 完成，不自建资金 HTTP 客户端。

## 路线与当前边界

业务方选择汇付「余额支付」：平台汇付账户向达人汇付账户付款，**不是再次扣用户的钱包，也不是银行卡提现**。
对于余额 / 组合支付订单，目标是按订单只支付一次达人应得金额；平台抽成留在平台账户，不把用户余额部分和微信部分各向达人支付一次。
原有全额外部延时交易仍走延时确认；旧 `delay_acct_flag=N` 订单不因此获得自动补付授权。

已完成：

- `orders/balance_payment_gateway.py`：单收款方、普通 HUIFU 非延时余额支付与原流水查单；内部金额为整数分，渠道金额为元字符串。
- 请求中使用 `out_huifu_id`，不使用支付接口已弃用的 `huifu_id`；查询接口仍使用发起方 `huifu_id`。
- 收款人、金额、出款主体、请求日期 / 流水、渠道流水及费用证据核对；只记录响应摘要，不暴露完整回包。
- 同步返回 `S` 仍待独立查询，不直接增加达人可提现余额；查询 `C` 是灵活用工专属状态，本适配器不解释为普通余额支付成功。
- 查询不存在、超时或未核实结果不能触发换流水重新付款。`failed` 也不提供自动重付入口。
- 普通个人账户场景按主动查询设计，不虚构通用余额支付回调。官方文档的异步通知有灵活用工 / LV3 等条件，不能复用聚合支付通知处理器。

已接入受控管理命令、`providers.process_income_transfers` 调度、查询补偿及达人收入幂等入账。
**真实执行默认关闭，尚未做真实资金验收。** 原有分账开关不会打开此付款路径；直接实例化网关不是生产操作方式。
后台页面仍为只读，没有付款或重付按钮。本地来源准备的 `execution_enabled`、`channel_funds_verified` 仍为 `false`，
表示这份准备快照本身不证明渠道资金可用，也不提供执行授权；实际渠道状态和费用另见出款记录。

## 官方合同与范围

- [余额支付产品介绍](https://paas.huifu.com/docs/bzb/#/zhzjfw/yezf/yezf)：支持账户间支付，商户归属关系、产品及权限须符合条件。
- [余额支付](https://paas.huifu.com/open/doc/api/#/yuer/api_acctpayzf.md)，文档更新 2026-09-23：`POST /v2/trade/acctpayment/pay`。
- [余额支付查询](https://paas.huifu.com/open/doc/api/#/yuer/api_acctpaycx.md)，文档更新 2026-09-21：`POST /v2/trade/acctpayment/pay/query`。
- [账户余额查询](https://paas.huifu.com/partners/api/doc/jyjs/api_jyjs_yuexxcx.md)：只核验显式配置的、正常基本户；严格解码 `acctInfo_list`，校验可用 + 冻结 = 总余额。

已读取正文、嵌套字段表和示例；实际取值以字段表为准，不复制示例业务类型、IP 或商户号到生产。
本适配器刻意不实现 ZXE、LV3 密码校验、短信、比例分账、灵活用工完成状态和延时余额支付。
分账 / 付款后退款继续走人工售后，不增加自动余额支付退款。

| 证据 / 参数 | 本系统要求 |
| --- | --- |
| 支付金额 | 本订单达人应收金额；平台支付手续费不能转嫁达人 |
| 风控 | 必填 `risk_check_data`，当前支持真实来源 IP 与经确认的 `transfer_type`；两者无默认值 |
| 手续费模式 | `merchant_payer` 为已确认的商户侧出款方承担模式，不发承担方参数；`request_payer` 仅用于已配置接口指定承担方的商户，发送 `OUT` |
| 查单流水 | 使用持久化的余额支付日期和请求号，不能用充值单或用户订单支付请求号替代 |
| 查询费用 | 使用 `acct_split_bunch.fee_amt`；缺失为未知，不能当零；非零费用必须核实由平台承担 |
| 收款行费用 | 官方只在收款方承担时返回；若有正数，视为费用规则冲突 |
| 金额 / 收款人不符 | 签名通过也不能算成功，抛出可审计证据冲突 |
| 查询缺少渠道流水等 | 保持待核实，不照抄成功示例中空流水或缺失费用来放行 |

## 执行器与资金保护

1. 数据库唯一出款意图 / 流水、不可变主体和金额快照、独立支付费用字段及观察日志；在任何资金请求前提交事务。
2. 沿用订单锁，核验结算冻结期、未处理售后、退款、历史人工审核标记、达人收款权限，并锁定付款与退款互斥状态。
3. 将余额扣款批次追溯到充值来源，核实组合支付外部资金归属；查询平台实际可用资金，处理并发资金占用。充值记录成功不证明当下资金可用。
4. 提交后只按原流水查询补偿；签名、金额、收款人及手续费全部核实后幂等入账一次。后续冲突应冻结收入并保留已记账事实，不能抹账或重复支付。
5. 独立的默认关闭开关、单笔限额、人工异常入口和灰度验收；没有授权不得调用真实商户交易。

上述执行保护已实现于 `orders/wallet_distributions.py`：

- 复用结算单的唯一 `ProviderOrderDistribution`，新增 `route=balance`，两条路线不能各付一次。`payment_fee_amount` 对余额路线保持 `null`，余额支付费用使用独立字段，不将充值手续费重复记入每笔消费。
- 出款前主动查充值 / 外部支付原流水，核对成功状态、非延时且非分账、入账成功、所属出款账户和金额；缺少账户证据或存在冻结信息时停止，不用平台其他账户补付。
- `ProviderBalancePaymentPool` 对每个配置账户只允许一笔未核实付款。数据库条件唯一约束与行锁双重保护；网络查询在事务外，回库后重核订单快照、账户代次和查询时效（30 秒）。
- 必须有足够可用资金覆盖达人应收 + 已配置的费用预留。该预留是本系统安全额度，不是汇付账户冻结，也不是自行推算的费率；不能阻止平台在系统外支用资金，实际仍以渠道结果为准。
- 查询确认成功与收入入账、释放本地付款占位在同一事务中；入账失败共同回滚，下次按原流水恢复，不再次付款。
- 未知 / 查不到交易保留占位并持续查单；明确失败、费用超预留或证据冲突会暂停该平台账户后续余额付款。已记收入不抹账，异常账户暂停新增提现。解除平台账户暂停需另行人工核账，当前没有一键解冻 / 重付命令。
- 后台现有财务待办覆盖余额付款异常、长时间处理中和成功未入账；普通串行等待与未启用配置不重复产生财务告警。

原有取消、部分退款、提前终止、履约争议及历史人工核账限制不放开。登记渠道付款后，自动退款仍被拦截，交由人工售后。

## 配置与部署

迁移 `orders/0038_provider_balance_payment_execution.py` 只扩展字段、账户串行表和约束；已有出款默认仍为 `delayed`，不扫描或补付历史订单。部署先迁移，再启动新版本 API / worker / beat。

配置均在服务端；本次没有修改真实 `.env`，也没有执行生产迁移：

| 配置 | 默认 / 作用 |
| --- | --- |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED` | `false`；独立真实付款开关 |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_CONFIRMED` | `false`；须确认平台向达人付款权限、资金用途、费用模式和风控来源后才可设置 |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_MAX_CENTS` | `0`；单笔达人付款限额，单位分 |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_ACCT_ID` | 空；已核实的实际平台基本户，必须与余额查询及原付款入账账户一致 |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_MODE` | 空；经渠道确认的 `merchant_payer` 或 `request_payer` |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_RESERVE_CENTS` | `-1`，阻断；确认费用上限后设置非负整数，不能将未知费用配置成零 |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_TRANSFER_TYPE` | 空；经渠道确认的实际业务类型，不照抄示例 |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_RISK_IP` | 空；渠道认可且真实的风控 IP，不能使用示例或回环地址；若必须用户实时 IP，此部署方式暂不可启用 |
| `HUIFU_PROVIDER_BALANCE_PAYMENT_START_AT` | 空；必须带时区的明确启用时刻，仅覆盖之后支付的新订单，不向过去调整以补付旧单 |

仍使用原有服务端 SDK / RSA 配置及 `HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED`。自动任务另需 `HUIFU_PROVIDER_INCOME_JOBS_ENABLED`。
关闭新的付款开关只停止新增付款；已经登记的流水仍由原任务按原渠道范围查询。不要为停止新付款而关闭查询补偿。

```bash
# 本地只读 / 本地准备：不访问渠道、不付款
sudo bash ./deploy-docker.sh manage provider_wallet_settlement inspect DZY实际订单号
sudo bash ./deploy-docker.sh manage provider_wallet_settlement prepare DZY实际订单号
# 已有余额付款仅查原流水；核实成功后可能补齐一次本地收入记账
sudo bash ./deploy-docker.sh manage provider_wallet_settlement query DZY实际订单号
```

受控小额实付使用同一命令的 `execute` 操作，必须额外传 `--confirm-real-funds`，且全部独立开关、限额、业务和资金核验通过才会提交。
重复 `execute` 只返回既有记录，不能生成新流水；更不能删除原出款单后重付。

仍需汇付 / 业务方确认：平台商户向当前达人个人账户付款的权限和账户范围，实际业务类型枚举，手续费模式 / 费率，风控 IP 的合法真实来源，以及充值、微信资金可用于此产品的账户与周期。
不能仅凭文档存在或 SDK 适配完成就认定以上条件已满足。

## 离线验证

```powershell
.\.venv\Scripts\python.exe scripts/check_wechat_auth.py orders.test_wallet_distributions orders.test_balance_payment_gateway orders.test_wallet_distribution_concurrency orders.test_wallet_settlement_preparation orders.test_settlement_plans orders.test_settlement_fees orders.test_distributions orders.test_distribution_safety wallets.test_consumption_discount backoffice.test_manual_refunds providers.test_withdrawals backoffice.test_finance_work
```

支付网关测试采用独立临时 RSA 密钥，运行真实 SDK 构造、签名、验签链路，只模拟 HTTP。
覆盖默认禁止提交、准确路由和字段、金额与主体冲突、未知费用、篡改 / 无签名回包、超时、查不到订单、SDK 状态恢复。
不使用生产数据库或真实资金。`orders.test_wallet_distribution_concurrency` 覆盖并发登记、跨订单账户占位、重复查询及退款竞态，必须用独立 PostgreSQL 测试库执行；内存 SpatiaLite 会跳过，不能当作并发或真实到账验收。

执行阶段回归共 235 项：228 项通过，7 项 PostgreSQL 专用测试跳过（4 项余额结算并发、3 项钱包折扣并发）。
覆盖先登记再请求、超时不重发、资金不足、来源变动、重复查询只记账一次、与退款互斥、费用超限、冲突冻结、账户串行、配置关闭后继续原流水查询及后台只读序列化。
迁移一致性检查通过（无遗漏模型变更），Ruff 与差异空白检查通过；运营端类型检查 / 构建及离线浏览器测试通过。
当前环境没有可用的独立 PostgreSQL 测试连接，行锁并发用例尚未执行；没有真实资金或生产迁移验收。以上结果不能代替这些上线前检查。
