# 注册默认头像

6 张同系列动物头像：猫、狗、熊、兔、熊猫、水獭。由内置 image_gen 生成，
完整提示词见 [prompts.json](prompts.json)。线上文件在 `v1/`：256×256 WebP，
每张约 3–6 KB。原始 PNG 交付在主工作区 `design-assets/default-avatars-20261007/originals/`。

## 部署后首次发布

沿用项目已配置的 COS 存储，不增加图片域名。需已有有效超级管理员，
默认将素材归属到最早创建的超级管理员；可用 `--owner <管理员 public_id>` 指定。

```bash
sudo bash ./deploy-docker.sh manage publish_default_avatars --dry-run
sudo bash ./deploy-docker.sh manage publish_default_avatars
```

发布会上传 COS 并登记到「运营配置 → 资源素材」；所有上传完成后才启用新素材。
可重复执行，已发布且内容一致的图片不重复上传。已有同名但内容或状态不一致的记录会报错，
不会覆盖正在使用的对象。无需数据库迁移，不修改历史用户，也不创建测试账号。

## 行为与保护

- 手机号注册、微信小程序新用户、微信绑定手机号新用户统一由 `UserManager.create_user` 随机分配一次并存库。
- 显式提供自定义头像时保留原值；已注册用户登录、刷新资料均不会重新分配。
- 尚未发布素材时继续使用原先文字占位，避免出现无效图片地址。
- 不按性别分配；不会覆盖用户自行上传的头像。
- 默认头像为共享系统素材，资源库禁止删除；更换个人头像不会删除共享图片。
- 请勿直接在 COS 删除或覆盖 `defaults/avatars/v1/`。后续更换图案应新增版本，保留旧对象供已有用户使用。
- 只在注册时查询一次可用素材，不在登录、列表接口中重复查库，也不在注册请求中访问 COS。

## 回归测试

本地无业务数据库验证（沿用项目的内存 SpatiaLite 测试工具）：

```bash
uv run python scripts/check_wechat_auth.py mediafiles.test_default_avatars accounts.tests mediafiles.tests backoffice.test_assets
```

在配置好的独立测试库上：

```bash
uv run python manage.py test mediafiles.test_default_avatars --settings=config.settings.test
```

覆盖随机持久化、已注册账号不变、手机与微信创建入口、素材状态过滤、共享图片保护、
发布幂等、试运行、上传失败与版本冲突。测试 COS 上传全部使用 mock，不会写真实存储。
