# 注册默认头像

10 张同系列动物头像：猫、狗、熊、兔、熊猫、水獭、狐狸、考拉、企鹅、水豚。由内置 image_gen 生成，
提示词见 [原 6 张](prompts.json) 和 [新增 4 张](prompts-20261119.json)。线上文件在 `v1/`：256×256 WebP，
每张约 3–6 KB。原始 PNG 在主工作区 `design-assets/default-avatars-20261007/originals/`、`design-assets/default-avatars-20261119/originals/`。

## 部署后首次发布

沿用项目已配置的 COS 存储，不增加图片域名。素材归属账号可以是正常启用的
Django 超级管理员，也可以是拥有 `asset.manage` 权限的平台后台成员
（包括通过 `grant_platform_admin` 开通的平台运营账号，无需设置 `is_superuser`）。
默认优先选择有效超级管理员，否则选择最早创建且具有上述权限的平台后台账号；
可用 `--owner <后台用户 public_id>` 指定，账号 ID 可在后台用户详情查看。
指定账号时仍检查同样的权限，不会自动回退到其他账号或提升账号权限。

```bash
sudo bash ./deploy-docker.sh manage publish_default_avatars --dry-run
sudo bash ./deploy-docker.sh manage publish_default_avatars
```

发布会上传 COS 并登记到「运营配置 → 资源素材」；所有上传完成后才启用新素材。
可重复执行，已发布且内容一致的图片不重复上传。已有同名但内容或状态不一致的记录会报错，
不会覆盖正在使用的对象。无需数据库迁移，不修改历史用户，也不创建测试账号。

清空数据库会清掉发布登记，即使 COS 原文件还在，也需要重新运行此命令恢复默认头像池。
已发布原 6 张时重新执行，仅上传新增的 4 张，已有默认头像保持原图案。
如需给头像为空的正常普通账号（含达人用户）补齐默认头像，可显式执行：

```bash
sudo bash ./deploy-docker.sh manage publish_default_avatars --fill-missing --dry-run
sudo bash ./deploy-docker.sh manage publish_default_avatars --fill-missing
```

补齐只处理空头像，跳过封禁账号、后台账号和正在审核自定义头像的账号；锁定用户后再次校验，避免覆盖并发上传的头像。
已存在的默认或自定义头像不变。新注册用户默认昵称为“用户＋手机号后四位”，通过统一创建入口兜底；昵称不随登录或换绑手机号变化。

若提示“未找到可用的素材归属账号”，检查后台成员、所属平台组织是否启用、
用户账号是否正常，以及当前角色是否有素材管理权限。旧版命令仅识别
Django 超级管理员，可能错误拒绝正常的平台运营账号；请更新代码并重新构建部署后再执行。
归属账号检查在上传和数据库写入前完成，单独出现此项报错不会改变已部署的服务或头像，
无需因为部署脚本的通用回滚提示而回滚。

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
