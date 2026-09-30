# 短信接入前测试注册

生产环境未接入短信时，公开的“获取验证码”接口会返回 `503`，不会假装发送成功。**不要在生产环境打开 `DEBUG`，也不要为所有手机号启用固定 `123456`。**

若需通过真实注册表单测试，在服务器 `.env.online` 中设置仅自己控制的测试手机号：

```dotenv
SMS_TEST_REGISTRATION_PHONES=你的测试手机号
```

部署含 `issue_test_registration_code` 命令的新 API 版本后，从服务器终端执行：

```bash
cd /home/ubuntu/dazzy_api
bash ./deploy-docker.sh manage issue_test_registration_code 你的测试手机号
```

命令只接受白名单中尚未注册的手机号，生成随机六位码并写入与正常注册相同的缓存键；它仅可用于**注册**，有效期由 `SMS_CODE_TTL_SECONDS` 控制，默认 5 分钟，验证后即失效。将终端输出的验证码填入用户端注册页即可；不用先点“获取验证码”。普通访客无法调用此管理命令，也不会从公开接口获得该验证码。

该机制不能证明测试手机号的实际归属，只用于你控制的测试号码。测试结束后从 `.env.online` 移除 `SMS_TEST_REGISTRATION_PHONES`，后续管理命令将拒绝签发。正式短信服务接入后应继续使用正常发送流程。
