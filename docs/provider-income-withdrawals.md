# 达人收入余额与手动提现（受控试点）

## 已实现的流程

服务完成/用户确认 → 原冻结期和售后检查 → 平台账务结算 → 汇付延时交易确认分账 → 查询核验成功 → 达人收入余额 → 达人主动申请 → 金额转为提现中 → 查询核验到账。

这次按用户确认做增量改造：保留原订单状态、佣金快照和冻结/退款条件，新增独立达人收入台账，不复用消费者充值钱包，也不改成平台直接代付。70/30 只是订单抽成的示例，不改历史/分类佣金。支付、分账和提现费用由平台承担。

- `ProviderIncomeWallet` 记录可用、提现冻结及已提现金额；`ProviderIncomeEntry` 记录唯一来源的金额变动，单位均为整数分。
- 仅 `income_mode=manual_cash_v1` 且查询核验成功的分账入账，一笔分账只入一次；历史账务、钱包/混合支付、旧自动结算不自动补余额。
- 提现前重新查询本人取现卡、取现能力、自动结算已关闭、费用外扣方，以及汇付基本户可用余额。只允许 T1/D1，本阶段不推测 D0/DM 额度规则。
- 金额和请求 UUID 来自达人，收款用户、银行卡 token、账户及费用配置只取服务端已核验资料，客户端不能指定。银行卡 token 加密保存，API 不返回。
- 先在事务内冻结余额并登记唯一流水，提交事务后才调用 SDK；同一请求 UUID 重复提交不再出款。每位达人同时只允许一笔非终态提现。
- 同步“成功”只视为受理，独立原流水查询确认终态；超时、未验签、查无记录等保持冻结，禁止生成新流水重发。进程在发请求前崩溃也只能核账，不盲目重试。
- 失败查询还须核验渠道可用资金足以归还本地余额，才释放冻结，且只释放一次。银行卡退汇/冲突终态进入人工核账并阻止新提现，不虚构退汇入账。
- 新增独立任务开关。Celery Beat 每分钟调度受控分账和提现查询；原任务中心继续负责账务冻结期释放。提现任务永不代替达人创建申请；近七天成功提现也查退汇，超过七天由日常渠道账单和人工查询补充，不代表完整长期对账系统。

## 渠道与部署前置条件

此代码未部署，也未执行真实开户/改配置/分账/提现。不能把离线测试当成生产资金验收。

1. 先部署后端迁移至 `orders/0030`、`providers/0022`，再更新 API、Celery Worker/Beat、达人端及后台；不要只发布前端。
2. 新开户配置 `HUIFU_USER_CASH_CONFIG`（一个 JSON 对象）：`cash_type=T1/D1`，`fix_amt/fee_rate` 至少一个，费用为已核实的两位小数字符串；`out_fee_flag=1`、`out_fee_huifu_id=HUIFU_MERCHANT_ID`、`out_fee_acct_type=01/02/05`。D1 可分别提供工作日费用。**不提供可直接抄用的假费率。** 上级商户须开通对应个人用户取现能力及平台费用外扣权限。
3. 新开户只提交 `cash_config` 和本人 `card_info`，不提交 `settle_config_list`。旧 `HUIFU_USER_SETTLEMENT_CONFIG` 不再作为新开户参数。
4. **已经开通自动结算的旧账户必须由平台在汇付侧先切换**，本次没有自动发送 `/user/busi/modify`，不会仅凭用户点按钮就修改渠道。达人重新确认 `huifu-personal-cash-v2` 授权，后端只记录新授权；随后刷新核验实际配置。
5. 查询必须明确返回 `settle_config_list="[]"` 或所有条目 `settle_status="0"`，缺失字段不能证明已关闭。若真实渠道对未开通用户省略字段，应向汇付确认协议后补适配，不强行放行。本人卡必须为正常取现卡并返回 token；配置费用响应键是 `out_cash_*`，不是请求的 `out_fee_*`。
6. 延用分账试点白名单、单笔限额、平台费用规则确认、全额外部新支付范围。另配置提现单笔限额，默认 0。不扩大历史订单范围，不绕过账户注销/售后限制。
7. 所有开关默认关闭：

```dotenv
HUIFU_PROVIDER_DELAYED_PAYMENT_ENABLED=false
HUIFU_PROVIDER_DISTRIBUTION_ENABLED=false
HUIFU_PROVIDER_WITHDRAWAL_ENABLED=false
HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS=0
HUIFU_PROVIDER_INCOME_JOBS_ENABLED=false
```

先验证开户/手动模式和渠道费用 → 一位白名单达人小额新订单 → 正常完成并等待冻结期 → 显式单笔分账/查单 → 核对余额 → 达人主动提现 → 核对银行卡全额到账与平台费用 → 再决定启用自动任务。手动取现权限、基本户余额查询对个人用户的权限、实际返回结构和分账费用字段都需真实小额验收。

## 查询及异常处置

达人端收入页提供余额、提现中、已提现、提现申请与结果刷新；后台达人资料区显示手动提现与自动结算关闭的核验状态。提现 API 仅本人可访问、响应不缓存。支持命令只查询，不替达人申请：

```bash
sudo bash ./deploy-docker.sh manage provider_withdrawal inspect 提现流水号
sudo bash ./deploy-docker.sh manage provider_withdrawal query 提现流水号
```

关闭新分账/提现开关不会阻断原流水查询；任务开关可单独关闭，仍能手工查询。发现异常先停新增资金操作、保留台账及迁移，不删流水、不回滚已产生的资金数据。确认退款回退、银行退汇和人工调账须由财务与汇付核对后处理，本次不实现未经确认的自动回退/提现重发。

完整生产验收还包括隔离 PostgreSQL 下的真实并发、查询补偿、渠道长期对账、到账失败/退汇、平台费用不足和存量账户切换。分账字段 wire 冲突见 [分账试点说明](provider-distribution-pilot.md)。

## 官方合同与 SDK 核对

使用 `huifu-merchant-onboarding` 和 `huifu-pay-integration` skills；2026-10-03 对照官方字段表与本项目 `dg-sdk==2.0.24` 原始 Request 类。保留官方 HTTP、TLS、签名验签，共用既有进程 SDK 锁，严格拒绝未签名响应。不以 UI、HTTP 200 或同步 ACK 判资金到账。

- [用户业务入驻](https://paas.huifu.com/partners/api/doc/yhgl/api_yhgl_ywrz.md) 与 [用户信息查询](https://paas.huifu.com/partners/api/doc/yhgl/api_yhgl_yhywcx.md)：手动提现能力、本人卡与自动结算分开核验。
- [主动取现](https://paas.huifu.com/partners/api/doc/jyjs/qx/api_qx.md)：`V2TradeSettlementEncashmentRequest` → `/v2/trade/settlement/encashment`，使用已绑定 token 和独立提现日期/流水；不发送只适用于中信 e 账户的 `fee_type`。本阶段不传通知地址，按原流水主动查询。
- [出金交易查询](https://paas.huifu.com/partners/api/doc/jyjs/qx/api_cjjycx.md)：`V2TradeSettlementQueryRequest` → `/v2/trade/settlement/query`。核对原日期/流水、金额、费用、`trans_status`，单独识别 `re_exchange=Y`；字段不完整时保持待核实。
- [账户余额查询](https://paas.huifu.com/partners/api/doc/jyjs/api_jyjs_yuexxcx.md)：`V2TradeAcctpaymentBalanceQueryRequest` → `/v2/trade/acctpayment/balance/query`，验证返回列表中目标用户的正常基本户，检查可用/冻结/总额一致；不会将余额支付的 LV3 开通条件套到个人 LV1 手动提现。

## 离线回归

### 2026-10-03 审查修复

- 已核验的分账成功/失败终态遇到超时或乱序处理中查询，保留终态与审计，不新增余额锁定。只有明确相反的渠道终态才触发人工核账。既有锁定不自动清除，尤其不能用后续成功覆盖真实冲突。
- 新试点支付在订单锁内发现收款核验过期时，先回滚本轮本地准备，再在事务外查询原收款用户；至多重做一次完整预检。仍未核验、账户不安全或订单过期则停止，不降级成普通支付、不重复下单。不影响已登记支付的原流水恢复。
- 收入页是只读资格展示：已核验账户仅因缓存过期不禁用申请入口；真正提现仍强制获取新的已验签本人卡/手动模式查询、渠道余额证明，并在锁内复查。
- 提现配置指纹比较已解密 token 的身份与账户、费用配置的临时摘要，不比较随机化密文；摘要及原 token 均不写日志、不返回前端。真实换卡或配置变化仍阻止提交。
- 准备单仅表达本地业务条件，不再无条件附加执行关闭/渠道费用未知等阻断。实际渠道分账及费用单独展示，提现费用归属独立提现单。

本轮是存量 Django/Python SDK 2.0.24 聚合延时分账与个人收款用户查询的缺陷修复，不改接口 wire、同步受理/终态规则或通知协议，不新增真实配置修改。继续使用官方 SDK、安全配置、验签及原流水查单；商户权限、响应合同与实际资金结果仍待独立验收。PostgreSQL 并发回归结果见下节。

使用 `huifu-pay-integration` 的 `copilot-existing-system.md`、`copilot-go-live-checklist.md`、`payment-operations-faq.md`；以及 `huifu-merchant-onboarding` 的 `user-onboarding-detail-query.md`、`user-onboarding-field-contracts.md`。

```powershell
$env:COS_BUCKET='offline-test-1250000000'
uv run python scripts/check_wechat_auth.py providers.test_withdrawals providers.test_receiving_onboarding providers.test_huifu_user_transport orders.test_distributions
```

内存 SpatiaLite 数据库，官方 SDK 本体运行，HTTP 全部模拟，签名由临时独立密钥产生；未接触业务数据库或真实资金。

达人端 `npm run test:income-withdrawal` 覆盖金额校验、重复点击、提交前保存流水、超时/重启复用原流水、重试失败不丢弃原流水、设备存储失败与账号隔离；配合类型检查、H5/小程序构建和 `test:receiving-account:mp` 验证。

### 2026-10-03 PostgreSQL 隔离回归

已在用户授权的专用测试服务器（PostgreSQL 18.4 / PostGIS 3.6.4）完成两组测试：既有回归 336 项、新增资金并发回归 9 项，共 345 个不同用例全部通过，无跳过。既有组包括此前 SQLite 无法验证的注销并发及首次创建消费者钱包并发。

新增 `providers/test_income_concurrency.py` 使用独立连接、事务行锁和同步屏障验证：

- 同一订单并发执行分账：只登记一笔、只发一次。
- 并发查询分账成功：只创建一个收入钱包、只入账一次。
- 已结算订单在分账预检期间申请普通退款：由原结算保护拒绝；未放宽退款条件。
- 预检后第二个连接提交未完成退款测试记录：分账二次检查拦截，不发渠道请求。
- 分账流水已提交但渠道结果未定：普通退款不能插入。
- 同 UUID 并发提现：只冻结一次、只发一次，重复请求返回同笔申请。
- 不同 UUID 同时争用余额：只允许一笔，无超额冻结或重复出款。
- 提现成功并发查询：只扣一次冻结金额。
- 提现失败且资金已返回并发查询：只释放一次冻结金额。

`scripts/check_postgres_regression.py` 默认运行以上两组。连接参数 `host/port/dbname/user/password` 通过标准输入 JSON 提供，不读取 `.env`、不保存凭据；`--probe` 仅做只读检查。默认要求 TLS，只有专用临时测试库获得明确授权后才可加 `--allow-plaintext`，不改变业务环境配置。

脚本只对传入数据库做只读前置检查，再从 `template0` 新建随机命名的 `codex_regression_*` 库，启用 PostGIS、应用迁移和合成测试数据；绝不在传入库执行迁移/flush。结束时校验本次创建库的 OID 与属主，通过新连接删除该临时库。首轮发现长测试结束后原管理连接断开造成清理失败，已改为重新连接清理，遗留临时库也已清除。显式 `--cleanup-created-db` 只接受创建日志中的完整临时库名，并校验属主与专用标记，禁止批量通配删除、强制终止连接或删除传入数据库。

资金边界：这是存量 Django / Python 官方 SDK 2.0.24 聚合延时分账、收入台账与提现的数据库回归，不是汇付正式资金联调。HTTP 出口被测试脚本拦截，协议测试使用模拟签名响应；新增并发测试模拟渠道已核验结果以控制交错时序。未发真实开户、分账、提现或退款，不新增通知协议，也不改同步受理与查询终态规则。测试库凭据不写入仓库。

本轮使用 `huifu-pay-integration` skill 的 `references/copilot-existing-system.md`、`references/copilot-go-live-checklist.md`、`references/copilot-troubleshooting-playbooks.md`。按其存量系统、幂等和终态检查要求补齐验证；真实权限、渠道费用与银行卡到账仍需下一步小额受控验收。
