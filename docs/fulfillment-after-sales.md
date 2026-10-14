# 履约异常核查与退款

## 客服操作

入口：后台 → 订单管理 → 达人订单 → 详情 → 履约异常核查。

- 核实无误，恢复正常：沿用原功能；有待处理履约售后、退款记录或订单已结束时禁止直接恢复。
- 存在问题，处理售后：登记“履约异常售后”，保留异常、暂停自动确认与分账；已有未完成售后时进入原单，不重复创建。
- 继续核查：保存核查说明，保留冻结，不发起退款。

退款／售后列表也可筛选“履约异常售后”，打开“核实责任与退款”。必须核实双方陈述、履约证据和实际取消／结束时间，不能把客服审批时间当作取消时间。

责任选择为用户、达人、双方或非双方责任。只有用户责任可使用原订单取消规则；达人提前离场、服务质量争议不自动折半退款，也不套用用户违约金。人工裁定分别填写服务费、交通费、其他费用退款。用户责任改用人工金额时必须由退款主管审批。

## 金额与规则

1. 按订单保存的取消规则版本、原始分成比例和优惠后实付明细计算，不补用今天的新规则。缺少原始规则时只能人工核查；缺少分成快照时只允许无剩余款的全额退款。
2. 核实时间之前实际发生的出发、到场、服务开始决定阶段。公交／地铁 30／80 元使用原规则快照，不另加路费；其他分支同原取消计算器。
3. 用户失联扣费需已有到场、联系及足时等待记录，不能早于预约开始或到场计时；用户回应过则不能套用失联规则。
4. 已有成功退款时禁止重复完整套用取消规则，人工退款按各费用剩余可退额度限制。未完成或失败待核实的退款会阻止新裁定。
5. 预览列出每项实付、剩余可退、本次退款、退款去向和剩余待核账金额；混合支付按原退款分配方法处理，预览和创建退款共用同一函数。
6. 请求金额单位为分，禁止负数、超退和跨费用挪用。余额扣款分配、付款人及历史退款记录不一致时转核账。

## 流程与安全边界

转售后不退款；预览不退款；超限转主管不退款。裁定需要履约审核、售后审核及退款审批权限，并受现有城市范围、单订单累计额度、同一审批人跨订单类型日额度约束。

预览使用 15 分钟签名凭证，绑定订单、售后、履约版本、原规则、退款记录和计算结果。确认时锁定订单与售后并重新核算。表单变化即清除前端预览；状态、资金或权限变化由后端再次拦截。重复相同裁定返回原结果，退款单仍使用 `provider-after-sales:{case_no}` 幂等键。

裁定后将履约业务状态设为取消或终止，并清除自动确认定时；这不代表退款成功。退款沿用 `create_provider_order_refund` → 现有任务执行与渠道查询 → `_complete_provider_order_refund`。只有已有核验逻辑确认成功后，才更新支付、售后和财务结果；失败继续处理原退款单，不新建退款。

全退成功后关闭结算。部分退款或无需退款的剩余资金保持 `dispute_frozen`，显示“售后剩余款待核账”，不自动分账、不计入可提现余额。分账回退、剩余款财务放行不在本次实现内。已经进入渠道分账流程的订单被拦截，不通过本入口绕过回退核查。

驳回售后不代表履约正常，异常标记保留并返回履约待核查。实时待办从履约核查移至售后处理，完成后转剩余款核账；退款异常使用原异常退款待办。核账待办仅向同时具备售后查看及财务查看权限的人员展示。通知只复用站内订单通知，不启用短信或微信推送。

## 接口与部署

- `POST /api/v1/admin/provider-orders/{order_no}/fulfillment-after-sales/`：转售后或记录继续核查。
- `GET /api/v1/admin/order-after-sales/{case_no}/fulfillment-context/`：已付／可退明细、规则、证据时间线及权限。
- `POST /api/v1/admin/order-after-sales/{case_no}/fulfillment-preview/`：只读计算并签发预览凭证。
- `POST /api/v1/admin/order-after-sales/{case_no}/fulfillment-resolve/`：凭预览签名审批／升级主管。

上线需要执行 `backoffice.0040_fulfillment_after_sales`，增加售后类型和裁定快照字段；不改写历史订单、支付或退款。后端迁移与新版服务就绪后再发布后台前端。按现有发布流程备份并执行 `deploy --migrate`；本功能不需要新增汇付密钥、回调或开户配置。

## 验证

本地离线测试，迁移全新内存 SpatiaLite 库，不接触业务数据库：

```powershell
uv run python scripts/check_wechat_auth.py backoffice.test_fulfillment_after_sales backoffice.test_fulfillment_after_sales_concurrency backoffice.test_staff_refunds orders.test_fulfillment orders.test_termination orders.test_cancellations orders.test_distribution_safety orders.test_settlement_plans backoffice.test_operations_queue wallets.test_consumption_discount orders.tests
```

后台 `npm run build` 和 `npm run test:fulfillment-refunds:browser`；浏览器测试自启本地静态服务、模拟所有 API 并阻止其他网络请求。可用 `PLAYWRIGHT_MODULE` 指向已有 Playwright、`CHROME_EXECUTABLE` 指向 Chrome，无需在仓库内保存浏览器或凭据。

上线前仍须在专用 PostgreSQL 测试环境运行 `backoffice.test_fulfillment_after_sales_concurrency`，覆盖重复裁定、转售后与正常审核竞争、驳回与裁定竞争、跨订单日额度竞争。本地 SQLite 会跳过这些真实行锁用例，不能据此宣称 PostgreSQL 并发已通过。使用已有 `scripts/check_postgres_regression.py` 的隔离建库流程；凭据经 stdin 输入，不落盘，不迁移或清空给定业务库。默认 TLS；非加密连接须另获明确授权。

部署后用白名单测试单分别验收：用户责任按原规则退款、达人责任全退／部分退、超限主管审批、退款失败后查原单、站内通知和剩余款核账待办。核对原支付渠道实际退款结果；本地模拟测试不证明真实通道权限或资金到账。

## 汇付接入约束

本次使用 `huifu-pay-integration` skill，属存量聚合支付系统的业务流程增强：Django／Python、官方 `dg-sdk 2.0.24`，后台 Vue；不是新增汇付退款协议。实际使用的参考为 `references/copilot-existing-system.md`、`references/copilot-solution-selection.md`、`references/copilot-go-live-checklist.md`。

新增由客服业务层完成；原退款 Request、原交易定位字段、官方 SDK 请求与查询、签名验签、通知金额／订单校验、安全配置及失败查询补偿全部保留。本次不修改通道参数，不输出凭据，也不以同步受理或前端提示代替资金终态。真实环境配置、TLS 和渠道权限须由上线人员继续核验。
