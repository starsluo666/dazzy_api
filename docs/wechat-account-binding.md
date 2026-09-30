# 已登录账号绑定微信

个人资料里的绑定不同于登录页的“微信登录 + 验证手机号”：只能关联当前已登录账号，不创建账号、不更换手机号、不返回或覆盖登录会话。

## 接口

- `GET /api/v1/auth/wechat/binding/`：返回当前配置 AppID 的登录绑定渠道，不返回 OpenID/UnionID；支付用授权不视为登录绑定。
- `POST /api/v1/auth/wechat/binding/h5/start/`：须登录，发起公众号授权，State 绑定当前账号、安全版本和登录会话。
- `GET /api/v1/auth/login/wechat/h5/callback/`：复用现有登录回调地址；`bind_` State 使用独立绑定流程。回到固定个人资料页，仅签发一次性绑定票据，不直接修改账号。
- `POST /api/v1/auth/wechat/binding/h5/complete/`：当前原登录会话提交票据。验证归属、有效期、AppID 配置和账号状态后绑定。
- `POST /api/v1/auth/wechat/binding/mobile/`：当前登录会话提交原生 SDK 的一次性 Code；服务端兑换身份并绑定。

仅接受服务端兑换的身份，客户端不能指定目标账号或 OpenID。已有其他账号归属、当前账号已绑定不同微信、跨渠道 UnionID 归属冲突都会拒绝；不提供替换、解绑或自动合并账号。

H5 绑定复用 `WECHAT_OFFICIAL_ACCOUNT_APP_ID/APP_SECRET`、`WECHAT_H5_LOGIN_CALLBACK_URL` 和 `WECHAT_H5_LOGIN_RETURN_URL`。原生 App 复用 `WECHAT_MOBILE_APP_ID/APP_SECRET`，客户端使用 `onlyAuthorize: true` 获取 Code，AppSecret 仅在服务器。无需新迁移或额外微信回调域名，但 H5 必须在微信内打开，App 必须正确打包 OAuth 模块。

网页授权以登录返回 URL 的站点和部署根路径为基础，固定回到 `/pages/profile/edit`；不接受客户端自定义回跳地址。浏览器使用同标签页 sessionStorage 保留 State 和未保存的基本资料，回跳后立即删除票据参数，校验账号和 State 后才提交绑定；临时头像需要重新选择。

跨端共享微信登录仍遵循 `WECHAT_CROSS_CHANNEL_UNIONID_ENABLED`，仅在公众号和 App 已属于同一个微信开放平台主体时开启。未开启时，每个登录渠道须分别授权绑定。

验证：`uv run --env-file .env pytest accounts -q --reuse-db`；用户端 `npm run test:wechat-binding`。真实微信授权需部署后使用正确公众号/AppID 在真机联调，测试不调用微信或真实账号。
