# 用户功能限制

后台「用户管理 → 用户详情 → 功能限制」使用 `user.status.manage` 权限，沿用用户管理的数据范围，并排除后台员工、超级管理员。

- 达人下单：拦截创建新达人订单；已创建订单的支付、履约、取消、售后和退款保持原流程。
- 发布活动：拦截创建活动、创建发布支付单，以及已有未支付发布单再次发起支付。查单、支付回调和退款继续处理，避免丢失已支付资金。已付发布费的活动继续原审核流程。
- 评价／评论：目前拦截用户提交达人订单评价；系统到期默认评价继续执行。以后新增评论写入口时需要复用相同限制校验。

功能可多选、分别添加或解除，持续至手动解除。功能限制不修改账号状态、认证版本、达人接单资格，不退出用户登录。操作原因必填，当前限制记录操作人、时间和原因；每次添加和解除均写审计日志并发送站内系统通知。恢复封禁账号不会清除功能限制。

新增 `accounts.0008_userfunctionrestriction`，上线需执行迁移。迁移不修改历史 `restricted` 账号，这类账号仍禁止登录，需要运营确认后手动恢复。旧 `/account-action/` 不再接受 `restrict`，使用新增接口：

```
POST /api/v1/admin/users/{public_id}/function-action/
{"action":"restrict","functions":["provider_order","review"],"reason":"投诉核查中"}
```

`action` 为 `restrict` 或 `restore`。接口按所选功能增量操作，不替换整个限制集合。客服修改和用户写业务均锁定同一用户行，再读取实时限制，防止并发覆盖或旧登录对象绕过。新增功能限制不参与认证层的全账号拦截。

验收：`uv run --env-file .env pytest backoffice/test_user_restrictions.py accounts/test_function_restrictions.py -q --reuse-db`。使用 Django 隔离测试库，支付、地图调用使用测试桩。
