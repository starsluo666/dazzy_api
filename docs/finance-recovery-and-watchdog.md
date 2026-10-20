# 资金异常处置与支付任务巡检

## 边界

存量 Django / Python 汇付聚合支付、原支付延时分账、账户余额付款及提现系统。
本轮没有增加支付协议，没有新增出款、退款或分账回退接口，不处理活动发起人出款。
依旧使用 `dg-sdk==2.0.24` 和现有签名/验签适配器，保持原请求日期、流水、
金额、收款方、渠道范围和历史观察记录。

采用汇付支付接入技能，实际参考：

- `copilot-existing-system.md`
- `copilot-go-live-checklist.md`
- `copilot-troubleshooting-playbooks.md`
- `shared-server-sdk-matrix.md`
- `aggregation-python-adapter.md`

未触发新接口接入硬检查点：协议/回退路径未变，密钥仍通过现有服务端安全配置提供。
不使用自行签名 HTTP，不把同步受理当成终态，不记录原始渠道响应或异常消息。
读页面没有渠道请求。只有显式“查询原流水”及恢复复核会调用原有查询适配器；
查询成功后的幂等入账仍由既有执行器完成，不伪造成功、不生成新出款流水。

## 操作入口

后台 → 财务管理 → 资金处置与巡检：

1. 从“资金异常核查”的分账、提现、收入账户详情登记处置单；
   平台余额付款账户暂停从新页“平台付款暂停”登记。
2. 填写核查说明，必要时显式查询原流水，每单查询至少间隔一分钟。
3. 提交内部凭证编号，由另一位财务独立复核。禁止保存下载链接、令牌、密钥。
4. 核查结案只记录结果，**不会移除原资金异常或账户限制**。

已结算人工退款仍使用原人工售后流程；对账差异仍使用每日对账工作台。
不会因处置单结案而关闭这些待办。

### 受控恢复的第一阶段范围

默认 `HUIFU_FINANCE_RECOVERY_ENABLED=false`，登记、查询、凭证复核和巡检不受此开关影响。
开通受控恢复前，先完成 PostgreSQL 并发测试及真实渠道小额验收，配置两名独立财务。

仅允许：平台余额付款原流水已明确失败、没有任何历史证据冲突，
账户仅有这笔未解除预留的付款，由两人提交/复核后恢复**后续订单**处理。

恢复复核重新查询原流水及实际账户余额，校验渠道/账户范围、金额、查询摘要、
最新观察记录、原证据指纹、账户 generation 和 30 秒有效期。
再次核验失败/未知、可用资金不足、配置变化或出现新结果时均保留暂停。

恢复仅释放本地串行付款占位并清除该平台账户暂停：

- 原失败订单仍为失败，唯一流水及全部观察记录保留，禁止重付原单。
- 不修改达人钱包金额或资金流水，不修改对账差异、不关闭退款/售后。
- `failure_reviewed_at` 仅表示该失败记录已完成这次受控恢复。
- 后续出现冲突，即使该失败记录已复核，也重新暂停账户。
- 退汇、金额/终态冲突、达人账本不平、未知资金结果不在在线解除范围，
  须继续人工与渠道核查。不能通过选择“核查结案”绕过。

同一目标只保留一张进行中的处置单。所有动作带 revision，查询使用带期限的运行令牌，
旧请求不能覆盖新核查结果；网络请求不持有数据库事务锁。

## 权限与通知

新增 `finance.recovery.view/manage/review` 三项权限，仅全平台数据范围有效。
迁移仅补给全平台 `platform-admin`；其他财务角色按实际职责分配。
登记人和提交人均不能审核自己的处置单。

待核查、待独立复核、平台付款暂停、支付任务异常进入运营实时待办。
已读不改变资金状态。同一未变化事件不重复变成未读。
没有接入短信或微信推送。

## 支付任务巡检

记录以下实际任务的开始、结束、成功、运行租约及脱敏错误码：

- 渠道出款与提现查单（60 秒调度，5 分钟静默阈值）
- 充值补偿查单（60 秒调度，5 分钟静默阈值）
- 每日对账调度（30 分钟调度，90 分钟静默阈值）
- 到期业务任务执行（10 秒调度，3 分钟静默阈值）
- 业务任务补建（180 秒调度，15 分钟静默阈值）

执行租约 15 分钟，对账 30 分钟。运行令牌仅隔离心跳写入，资金幂等仍由原执行器保证。
任务失败不写原异常字符串；任务返回 disabled 不更新成功时间。
开启中的任务超过间隔、执行超时、最近失败会告警；配置关闭显示未启用。
每日对账另查最近已到核对期限的账单覆盖，20:30 后检查前日五类账单，
不把“调度器运行了但账单未核对”显示为一切正常。
成功执行只表示任务跑完，不代表每一笔交易成功；逐笔异常由原资金待办呈现。

读页面和下面命令独立检查当前时间与数据库记录，不依赖 beat 自己报告异常：

```bash
sudo bash ./deploy-docker.sh manage check_payment_jobs
```

异常时非零退出。生产应由独立于 Celery 的服务器监控定期调用，并监控数据库/API
不可用和调用失败；此命令不发送消息、不重试任务、不操作资金。
若服务器、数据库及后台同时不可用，后台本身不能代替外部可用性监控。

## 部署与验收

包含 `orders.0039`、`financeops.0001/0002`、`backoffice.0043` 迁移。
先迁移，再让 API、worker、beat 使用同一代码版本。迁移会初始化五条任务记录；
首次执行有静默宽限，不需要手工制造“成功”心跳。

离线测试只验证状态机、权限、签名模拟返回、幂等和故障注入，不验证真实权限、费率、
资金到账。生产开关不要仅凭离线测试开启。

## 此前本地验证（SpatiaLite）

- 扩展回归共 338 项：318 项通过、20 项 PostgreSQL 专用并发测试跳过。
- 最终增补重跑 `financeops.tests/test_watchdog/test_pool_recovery/test_concurrency`：
  29 项中 27 项通过、2 项 PostgreSQL 专用测试跳过（与上轮部分重叠，不累加计算）。
- 三个后台浏览器脚本通过：资金处置、原资金异常核查、每日对账；
  最终 UI 调整后再次通过资金处置脚本。
- TypeScript 检查及生产构建通过；保留项目原有大包体积提示。
- Django 系统检查、迁移一致性检查、Ruff 与 diff 空白检查通过。

以上后端测试使用独立内存 SpatiaLite，渠道为模拟签名返回，禁止真实网络支付。
未对生产数据库迁移或执行资金操作，未改变生产配置。
PostgreSQL 并发测试文件已提供，可使用现有
`scripts/check_postgres_regression.py` 在新建一次性测试库中执行；
真实小额渠道验收和服务器外部监控配置仍需上线前完成。

## 2026-10-20 PostgreSQL 首次验收（曾有一项错误，后续补验见下节）

本轮按用户授权使用原专用测试服务器，PostgreSQL 18.4 / PostGIS 3.6.4。
服务器不支持 TLS；用户明确允许本次测试连接使用非 TLS 后，才使用
`--allow-plaintext` 执行。没有改变业务环境连接配置或汇付 SDK 的 TLS 校验。
SDK 实测版本为 `dg-sdk==2.0.24`。

原连接库只用于只读前置检查，迁移、合成数据和测试均在全新一次性库
`codex_regression_20261020_141426_cf49e90c` 中运行。Django 系统检查、
迁移一致性检查和全量迁移通过；未加载真实渠道配置，真实 HTTP 出口被拦截。

### 结果

- 340 项：339 项通过、1 项错误、0 项跳过，测试执行耗时 1373.955 秒。
- PostgreSQL 专用并发组 20 项：19 项通过、1 项错误（包含在上述总数内）。
- `financeops` 的 29 项全部通过，包括双人并发复核、任务租约、权限、
  查询限频、证据变化拦截及受控恢复，不与上述总数重复累加。
- 测试进程按预期返回非零退出码，没有把本轮验收标为通过。
- 脚本校验临时库身份后完成删除；随后再次只读探测，
  `existing_regression_databases=[]`，没有遗留本轮测试库。

执行入口为 `scripts/check_postgres_regression.py --allow-plaintext`，
连接参数仅经标准输入传入，不保存或回显密码；本轮测试标签为：

```text
financeops.test_concurrency
reconciliation.test_concurrency
orders.test_wallet_distribution_concurrency
providers.test_income_concurrency
financeops.tests
financeops.test_watchdog
financeops.test_pool_recovery
reconciliation.tests
backoffice.test_operations_queue
backoffice.test_finance_work
backoffice.test_manual_refunds
backoffice.test_staff_refunds
orders.test_balance_payment_gateway
orders.test_wallet_distributions
orders.test_wallet_settlement_preparation
orders.test_distributions
orders.test_distribution_safety
orders.test_settlement_plans
orders.test_settlement_fees
providers.test_withdrawals
taskcenter.tests
wallets.test_wechat_payment
```

### 未通过项及下一步

`orders.test_wallet_distribution_concurrency.WalletDistributionConcurrencyTests.
test_concurrent_refund_wins_before_durable_registration` 在调用 `self.refund()` 时，
被 `orders/services.py` 的已结算退款保护拒绝：
“订单资金已经结算，不能直接退款，请转异常交易处理。”

测试夹具的订单已经结算，但用例仍假设可以直接创建普通退款，因此在验证
“预检期间新增退款阻止付款”的断言前就退出。这个报错不是重复出款的证据，
也不能证明该用例原本想验证的并发场景已经通过。

后续应在不放宽业务保护的前提下拆清两类测试：

1. 已结算订单的普通退款仍被拒绝。
2. 另一连接提交合成待处理退款记录后，出款二次检查会阻止登记和发送。

同模块相关的非并发用例
`WalletDistributionTests.test_refund_during_read_only_preflight_prevents_payment`
也应核对：当前宽泛捕获 `ValidationError` 可能只覆盖退款入口拒绝，
未真正覆盖预检后出现待处理退款的场景。

首次验收未修改业务代码或测试断言，未提交、推送、部署或开启受控恢复。
真实支付、退款、分账、提现、次日渠道账单及生产监控仍须独立验收。
本轮使用支付集成技能的三份 reference：
`copilot-existing-system.md`、`copilot-go-live-checklist.md`、
`copilot-troubleshooting-playbooks.md`；保留原请求流水、签名验签和服务端终态规则，
不把本次合成数据库回归当作真实渠道权限或资金到账证明。

## 2026-10-20 退款测试修正与补验（通过）

用户确认后，只修改两个测试文件及本验收记录，未修改业务退款或出款规则：

- `orders/test_wallet_distributions.py`：加入仅测试使用的待退款记录构造，
  费用分项及余额退款路径满足数据库约束，不调用真实退款接口。
  修正余额查询期间新增待退款记录的用例，明确断言
  `DistributionBlocked.code == "local_conditions"`、余额查询实际执行、
  待退款记录存在、付款未发送、分账及收入流水未创建。
- `orders/test_wallet_distribution_concurrency.py`：在渠道预检处使用同步屏障，
  核实两个不同 PostgreSQL 后端连接；第二连接持订单锁提交合成待退款记录后，
  才放行付款线程，验证出款二次检查确实拦截。
  另加独立用例确认预检期间普通退款仍被“已经结算”保护拒绝，
  不产生退款单；未受退款影响的原付款也只登记、发送一次。

不再把退款入口自身抛出的任意 `ValidationError` 当成付款二次检查通过。
原有签名验签、原流水、出款状态、幂等和已结算转人工售后的规则全部保留。

### 补验结果

- PostgreSQL 关联回归 **71 项全部通过，0 失败、0 错误、0 跳过**，
  耗时 418.657 秒；包含全部 **21 项 PostgreSQL 专用并发测试**。
- 完整本地 SpatiaLite 回归 341 项：320 项通过、21 项并发测试按设计跳过，
  耗时 149.458 秒；这 21 项已由上述 PostgreSQL 补验覆盖。
- 修改后先行运行的余额结算模块 23 项全部通过，与上述结果重叠，不累加。
- Django 系统检查、迁移一致性、临时库全量迁移、Ruff 和 diff 空白检查通过。

本轮不是把完整 341 项在 PostgreSQL 上重新跑一遍：
首次 PostgreSQL 340 项中的 339 项通过记录保留，本轮针对修改涉及的模块、
全部并发组及复用同一测试数据的资金处置模块复跑。完整当前测试集另在
SpatiaLite 回归，并用 PostgreSQL 补齐全部并发跳过项；不把重叠结果相加。

PostgreSQL 补验标签为：

```text
orders.test_wallet_distribution_concurrency
orders.test_wallet_distributions
financeops.test_concurrency
reconciliation.test_concurrency
providers.test_income_concurrency
financeops.tests
financeops.test_watchdog
financeops.test_pool_recovery
```

延续用户对专用测试连接使用非 TLS 的授权，新建临时库
`codex_regression_20261020_145951_a12bca70`，未复用或清空既有业务库。
脚本退出码为 0，临时库已自动删除；事后只读检查确认
`existing_regression_databases=[]`。真实 HTTP 仍被拦截，渠道结果为模拟数据，
没有真实退款、分账、提现或业务配置修改，未提交、推送或部署。

继续使用本节前列出的三份支付集成 reference 保持存量系统安全边界。
本次关闭的是测试场景不一致的问题，真实渠道小额联调、实际到账和生产监控
仍须按独立上线验收执行，不能仅凭本次结果开启资金恢复开关。

### 提交前复核

- 再次运行资金处置、任务巡检、受控恢复、运营待办及资金异常工作台后端回归：
  50 项通过，0 跳过；使用独立内存数据库并拦截真实 HTTP。
- 后台 TypeScript 检查和生产构建通过；仍有项目原有的大包体积提示。
- `financeRecovery.browser.cjs`、`financeWork.browser.cjs`、
  `reconciliation.browser.cjs` 三项浏览器回归通过，所有 API 均为合成模拟。
- 本次变更范围 Ruff、暂存前空白检查通过，未发现新增内嵌私钥或数据库连接 URL。
- 保留默认关闭的受控恢复开关、独立复核及已结算订单退款保护；
  本次复核不代替真实渠道上线验收，也不操作生产配置。
