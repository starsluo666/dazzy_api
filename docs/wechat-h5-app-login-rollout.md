# 微信内网页与原生 App 登录接入说明

## 范围与账号规则

- 用户端微信内网页使用服务号 OAuth（`snsapi_base`）；原生 App 使用微信开放平台移动应用 OAuth。普通浏览器网页不展示微信登录，继续使用手机号登录。
- 两端共用现有 `accounts_user`，手机号仍是必填且唯一的账号标识。微信身份已绑定时直接登录；首次授权必须验证手机号：已有手机号绑定原账号，不存在则创建无密码账号。
- 登录身份保存到 `accounts_wechat_login_identity`，不使用 `accounts_wechat_official_identity` 支付授权记录作为登录凭证。支付授权也不改变登录绑定。登录绑定和历史支付身份均不能证明当前付款微信，支付必须使用当前会话、当前业务订单的一次性授权。
- 只有服务号与移动应用确实绑定同一微信开放平台账号时，才可开启 `WECHAT_CROSS_CHANNEL_UNIONID_ENABLED=true`，用 UnionID 识别两端同一微信。冲突时拒绝自动合并。
- 只有真正创建的新账号触发邀请注册奖励；绑定已有账号不重复领取。

## 微信平台和部署配置

1. 服务号确认已开通网页授权，配置回调域名；服务器环境填写 `WECHAT_OFFICIAL_ACCOUNT_APP_ID`、`WECHAT_OFFICIAL_ACCOUNT_APP_SECRET`（支付也在使用）、`WECHAT_H5_LOGIN_CALLBACK_URL`、`WECHAT_H5_LOGIN_RETURN_URL`。
2. `WECHAT_H5_LOGIN_CALLBACK_URL` 指向 `https://<API域名>/api/v1/auth/login/wechat/h5/callback/`。`WECHAT_H5_LOGIN_RETURN_URL` 指向 `https://<H5域名>/#/pages/auth/login`。两者必须是公网 HTTPS，AppSecret 只能放服务器。
3. 在微信开放平台申请并审核移动应用微信登录，填写与最终安装包一致的 Android 包名/签名、iOS Bundle ID/Universal Link。服务器配置 `WECHAT_MOBILE_APP_ID`（现有支付也使用）和 `WECHAT_MOBILE_APP_SECRET`。
4. 在用户端 `src/manifest.json` 的 App OAuth 模块和 `sdkConfigs.oauth.weixin` 中配置移动应用 AppID、iOS Universal Link；根级 DCloud `appid` 也需按正式 App 工程填写。当前这些值保留为空，不能直接用于正式云打包。不要把 AppSecret 写进安装包。使用自定义基座或正式安装包真机联调。参见 [uni-app 官方说明](https://uniapp.dcloud.net.cn/tutorial/app-oauth-weixin)。
5. 生产短信网关尚未接入。当前非 DEBUG 环境短信请求会明确返回 503，不会假装发送。选定服务商、签名和模板并完成集成后，首次绑定/注册才能上线。
6. 部署需包含 `accounts.0005_wechatloginidentity`、`accounts.0006_wechatunionidentity`、`orders.0026_payment_wechat_payer_digest`、`activities.0014_payment_wechat_payer_digest`、`wallets.0004_recharge_wechat_payer_digest`。先在测试库验证，再按发布流程执行业务库迁移；实际执行状态以目标数据库的 `django_migrations` 记录为准。

## 接口与防护

- `GET /api/v1/auth/login/wechat/h5/start/` 返回授权链接和浏览器应保存的随机 state。
- 微信回调 `/api/v1/auth/login/wechat/h5/callback/` 校验一次性 state，换取 OpenID，生成短时效、一次性 ticket，并回到固定 H5 登录页。H5 再比对本地 state；不在 URL 中放 JWT 或 AppSecret。
- `POST /api/v1/auth/login/wechat/mobile/` 接收 App SDK 获取的临时 code，返回一次性 ticket。
- `POST /api/v1/auth/login/wechat/resolve/` 用 ticket 尝试登录，或返回 `bind_required`。
- `POST /api/v1/auth/login/wechat/bind/sms/`、`POST /api/v1/auth/login/wechat/bind/` 完成手机号验证与绑定/建号。ticket 和短信验证码都限时、限次；失败或超时可重新发起微信授权。
- `POST /api/v1/auth/password/initial/code/` 仅为已登录且无密码用户的绑定手机号发送验证码；不接受自选收件手机号。
- `POST /api/v1/auth/password/initial/` 验证独立用途短信后设置初始密码，锁定用户行防止覆盖已设置密码，同时使旧 JWT 失效并返回当前设备新会话。用户在账号安全页可直接设置，其他敏感操作会先引导设置。

## 支付与并发边界

- 新登录的 JWT 带 `session_id`，刷新 access token 时保持不变；旧 token 回退使用 `jti`，刷新后可能需要重新授权。Django 已认证浏览器会话使用独立的 session key，不退回用户 ID。
- 达人订单、活动发布、活动报名、充值均采用短时随机支付 state，回调校验账户状态、登录版本、AppID 和业务单状态。授权按用户/AppID/登录会话/业务类型/订单隔离，创建支付会话时原子消费。state、OpenID 和凭据不写日志。
- 原预支付流水保存付款微信摘要，重试保持原流水、原付款人。切换微信时不会重新生成可能重复扣款的流水，也不会返回其他微信的旧预支付参数，需切回原微信、按现有取消流程重新下单，或等待旧单失效后重新下单（充值无手动取消入口）。
- 历史待支付单若已经提交过网关，但没有付款微信摘要，不能推断其付款人，因此拒绝复用；发布前应评估这批旧单，按现有关单/取消流程处理。已支付订单、退款、通知和账务不变。
- UnionID 归属新增独立唯一约束，跨端身份可多条但只能指向一个用户。应用在同一事务内原子认领归属；历史身份在下一次绑定/登录时校验并认领，有冲突则拒绝，不自动合并。仅在相同开放平台命名空间下启用跨端开关。

## 本地回归与未覆盖项

`uv run python scripts/check_wechat_auth.py` 在内存 SpatiaLite 库执行完整迁移与账号、支付回归，不读取 `.env`、不连接业务数据库。需安装 GIS 运行库；可通过 `SPATIALITE_LIBRARY_PATH` 指定模块。

此测试不能替代 PostgreSQL 真并发压测、Redis 多进程验证或微信真机测试。生产短信、微信 AppID/回调域名、原生包签名与 Universal Link 仍需部署配置和联调。

本次支付修复按 `huifu-pay-integration` 的存量改造规范执行，采用 `copilot-existing-system.md`、`payment-operations-faq.md`、`copilot-troubleshooting-playbooks.md` 三份参考。现有产品为汇付聚合支付，Python/Django 服务端、uni-app H5/App 客户端；仅修复 OAuth 付款身份及预支付复用边界。官方 SDK 传输、商户配置、请求流水/金额、通知验签、查单和终态确认保持原逻辑，不触发真实交易。

## 联调清单

- 微信内 H5：首次绑已有账号、首次建号、再次登录、取消授权、回调重放、state 不匹配。
- App：Android/iOS 真机首次登录和再次登录；微信未安装、用户取消授权、签名/Universal Link 错误。
- 跨端：同一微信 UnionID 登录同一账号；UnionID 缺失时走手机号；冲突不自动合并。
- 账号与业务：停用/注销账号不能登录；绑定已有账号不发新人奖励；原有微信内支付授权仍可正常使用且不会改变登录绑定。
