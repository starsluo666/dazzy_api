# Docker 生产部署（Ubuntu / 1Panel）

API（Gunicorn）、Celery Worker、Celery Beat 使用同一个版本镜像，分别托管。
继续使用现有 PostgreSQL/PostGIS 和 Redis；本编排不创建数据库，也不挂载数据库数据卷。
H5 与管理端仍使用各自的静态部署脚本。所有以下命令在服务器的 `dazzy_api` 目录执行。

## 1. 准备运行环境

需要 Linux Docker Engine 和 Docker Compose v2.24+，以及 `flock`（Ubuntu 的 util-linux）。
宿主机不再需要 Python、uv 或 GDAL；镜像包含 Python 3.12、锁定的 Python 依赖和地理库。
首次构建需要能够访问 Docker Hub、GHCR、Debian 软件源及 PyPI（或配置的镜像）。

### 构建下载慢时的软件源与缓存配置

`Dockerfile` 分别缓存 Debian 包、APT 索引与 uv 下载，并按架构隔离缓存。
缓存挂载不进入最终镜像；业务代码变化不会单独触发依赖重装。
构建失败重试可以复用已下载的完整包，首次构建仍需下载缺失内容。
不要为解决下载慢运行 `docker builder prune` 或 `--no-cache`，否则会失去可复用缓存。

默认使用 Debian 官方 HTTPS 源。如果服务器访问较慢，可以在已有 `.env.docker`
**追加或修改**下面配置（不要覆盖原来的网络、端口等配置）：

```dotenv
DAZZY_DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
# 安全更新优先使用官方源；若同样无法访问，再取消下一行的注释。
# DAZZY_DEBIAN_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security
```

源地址需按部署机网络实测选择；此处是可选示例，不承诺所有网络都更快。
主仓库和安全更新源单独配置；不填写则各自沿用官方源。使用受信镜像并关注安全更新同步延迟，
这也是[镜像站建议优先保留官方安全源](https://mirrors.tuna.tsinghua.edu.cn/help/debian/)的原因。
仅替换 Debian deb822 配置中的 URI，保留 bookworm 发行版、安全更新和 Debian 签名密钥；
不关闭 HTTPS 或包签名校验，不允许在源地址中写凭证、查询串等秘密。
参数只用于镜像构建，不是 `.env.online` 中的业务配置；调整后必须重新构建镜像。

如果日志停留在 `dependencies ... build_dependencies.py python`，慢的是 Python 依赖下载/构建，
和 APT 是两个独立步骤。在已有 `.env.docker` 追加或修改 Python 镜像配置：

```dotenv
DAZZY_PYTHON_PACKAGE_INDEX=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
```

上面使用[清华 PyPI 镜像文档](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)公布的地址；
也可换成部署机实测可达的受信 HTTPS simple-index，不支持含凭证、查询串的地址。
留空时仍执行 `uv sync --locked --no-dev --no-install-project`，沿用锁文件中的源。
启用镜像时，不直接修改 `uv.lock` 中的索引或下载 URL，而是执行：

- `uv export --locked --offline --no-dev --no-emit-project`：从现有锁文件临时导出版本、平台条件和哈希；锁文件与项目不一致时直接失败。
- `uv venv`：创建镜像内部的 `.venv`，使用基础镜像的 Python。
- `uv pip sync --require-hashes --default-index ...`：从选定镜像查找并安装锁定版本，每个依赖的下载内容必须匹配锁文件中的哈希。

临时清单构建后删除，不维护第二份依赖清单、不升级运行依赖，也不把镜像配置写入项目。
源码包仍使用隔离构建，其构建工具依赖也从选定镜像获取；构建工具版本不在项目运行依赖锁定范围内（与原流程相同）。
镜像未同步锁定版本或哈希不符时会停止，不会自动降级、回退其他源或关闭 TLS/哈希校验；
可以等待镜像同步、改用另一受信镜像，或清空该配置回到原始锁文件安装流程。
不同源的缓存不保证完全复用，切换后首次安装仍可能需要下载。

APT 下载连接/读超时 30 秒、失败最多重试 3 次；uv 连接超时 10 秒、读超时 30 秒、重试 3 次。
此外，APT 更新及安装整个步骤、Python 依赖安装整个步骤，**各自默认限制 1200 秒**。
超时先终止，30 秒内未退出则强制结束，构建失败并给出提示；不会继续发布不完整镜像。
这个限制不包括基础镜像拉取、等待缓存锁和后续发布检查，不是整个部署的总时限。
如果下载链路正常、机器较慢，可在 `.env.docker` 设置：

```dotenv
DAZZY_BUILD_DEPENDENCY_TIMEOUT=1800
```

允许范围 60–7200 秒，不允许设为 0 取消限制；启用 Python 镜像时，总时限包含导出、创建环境及安装三个步骤。
FFmpeg/FFprobe、地理库、中文字体及 HEIF 支持保持不变。

配置后仍按原命令发布（有迁移前先备份）：

```bash
sudo bash ./deploy-docker.sh deploy --migrate
```

如需保留完整构建日志，使用 `sudo env BUILDKIT_PROGRESS=plain bash ./deploy-docker.sh deploy --migrate`。
这次优化首次重建会使依赖层失效一次，后续相同配置可复用。构建阶段失败不会停止现有服务，
也不会执行数据库迁移；可用 `sudo bash ./deploy-docker.sh status` 核对旧服务状态。

```bash
cd /home/ubuntu/dazzy_api
docker version
docker compose version
cp .env.docker.example .env.docker
# 已有 .env.online 则继续使用，不要覆盖；首次配置时才复制 .env.example。
test -f .env.online || cp .env.example .env.online
chmod 600 .env.online .env.docker
```

`.env.docker` 只填写编排参数：

```dotenv
DAZZY_DOCKER_NETWORK=dazzy-backend
DAZZY_ENV_FILE=.env.online
DAZZY_API_PORT=8000
```

业务参数仍在 `.env.online`，包括数据库、Redis、COS、微信和支付配置。
这些文件被 Git 和镜像构建排除，通过 Compose `env_file` 注入。
Compose 会解析环境文件：包含 `$` 的密码请使用单引号包裹，例如 `POSTGRES_PASSWORD='a$b'`；
多行 PEM 可使用双引号和 `\n` 转义。不要用 `source .env.online` 加载密钥。

生产配置至少检查以下项目：

```dotenv
DJANGO_SECRET_KEY=使用已有的至少50位生产随机密钥
DJANGO_ALLOWED_HOSTS=api.ledaban.cn,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://api.ledaban.cn
POSTGRES_HOST=dazzy-postgis
POSTGRES_PORT=5432
POSTGRES_DATABASE=实际生产库名
POSTGRES_USER=实际数据库用户
POSTGRES_PASSWORD='实际密码'
REDIS_HOST=实际Redis容器名
REDIS_PORT=6379
REDIS_PASSWORD='实际密码'
DAZZY_DEMO_USER_PUBLIC_ID=
COS_PUBLIC_PREFIX=dazzy-prod/public/
COS_PRIVATE_PREFIX=dazzy-prod/private/
```

保留已有文件前缀和密钥，除非你明确要调整环境隔离；切换部署方式不需要轮换密钥。
其余 COS/地图等必需变量参照 `.env.example` 补齐。不要将新示例中的占位符直接投入运行。
`DJANGO_SETTINGS_MODULE` 和演示用户开关由编排强制设为生产值。
可选并发配置放在 `.env.online`：

```dotenv
DAZZY_GUNICORN_WORKERS=2
DAZZY_GUNICORN_TIMEOUT=90
DAZZY_WORKER_CONCURRENCY=2
DAZZY_CELERY_LOG_LEVEL=info
```

默认并发只作为起点，需要根据 CPU、内存和数据库连接数量调整。

## 2. 接入现有数据库网络

先查询现有容器和网络：

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}'
docker network ls
docker inspect dazzy-postgis --format '{{json .NetworkSettings.Networks}}'
```

如果数据库和 Redis 已在同一个自定义 bridge 网络中，直接把该网络名填入
`DAZZY_DOCKER_NETWORK`。不要填默认的 `bridge`，应使用支持容器名解析的自定义网络。

如果没有共同网络，可以在确认实际容器名后一次性建立：

```bash
docker network create dazzy-backend
docker network connect dazzy-backend dazzy-postgis
docker network connect dazzy-backend 实际Redis容器名
```

把网络同时写入数据库/Redis 所属的 1Panel 编排（external network），
否则它们重建后会失去手动连接。已有网络或已连接的容器无需重复执行这些命令。
不要把业务配置中的数据库地址继续写成 `127.0.0.1`，容器中的该地址指向自己。
如使用外部托管数据库，填写其内网地址并保证该网络可出站访问。

数据库原有公网端口映射不会被此脚本删除，需要在原编排/安全组中关闭或限制。

## 3. 首次发布

先停止旧 `start-dazzy-api.sh` 启动的 API、Worker、Beat；如果使用过 systemd，
也停止并禁用对应旧服务。旧 Beat 与新 Beat 不得同时运行。
迁移前在现有数据库系统完成备份，并确认能恢复。

```bash
bash ./deploy-docker.sh deploy --migrate
```

发布顺序：构建唯一版本镜像 → Django 部署检查 → PostGIS/缓存/Broker 连接验证
→ 迁移计划 → 停止 Beat、等待 Worker 和 API 结束 → 执行一次迁移
→ 启动三个服务并等待健康检查 → 记录成功版本。

普通更新且无新迁移时使用 `bash ./deploy-docker.sh deploy`；存在待执行迁移会提前退出，
保持旧服务运行。入口脚本不会在每个容器启动时自动迁移。
部署检查输出的 HSTS 等警告应按实际域名策略评估，ERROR 会阻止发布。

这是单机维护窗口发布，切换时有短暂停机，最长等待取决于正在执行的任务（每项 120 秒）。
超时任务可能被强制终止，应在任务中心检查失败/逾期任务；按业务需要增加优雅停止时间。
在停服或迁移之后失败，不会自动重启旧版本或逆向迁移；先查看报错并确认数据库状态。

## 4. 1Panel / OpenResty 配置

只在 `api.ledaban.cn` 入口配置 HTTPS，转发到 Gunicorn HTTP 服务。
先用实际 OpenResty 容器名查询网络模式：

```bash
docker inspect 实际OpenResty容器名 --format '{{.HostConfig.NetworkMode}}'
```

- `host` 模式（或 Nginx 直接运行在宿主机）：回源 `http://127.0.0.1:8000`。
- bridge 模式：将 OpenResty 加入同一个受信网络，回源 `http://dazzy-api-prod:8000`。
  同时在 OpenResty 所属编排保存网络配置，防止重建丢失。
  编排已为本 API 设置 `dazzy-api-prod` 别名，避免与其他项目常见的 `api` 服务重名。

在 1Panel 中后端域名填写 `api.ledaban.cn`，HTTP 回源无需 SNI。
请求路径保持不变：`/api/v1/health/` 必须原样到达 Django。
如果需要 `/django-admin/`，将该路径和 `/static/` 也转发，或者 API 域名直接代理整个 `/`。
Django 静态资源由 WhiteNoise 提供，启动时在容器内收集。

HTTPS 入口转发请求头至少包括：

```nginx
proxy_set_header Host api.ledaban.cn;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_read_timeout 100s;
client_max_body_size 60m;
```

该示例用于客户端直接到达此 HTTPS 入口的情况，覆盖而不是信任客户端传来的代理头。
`DRF_NUM_PROXIES` 按实际代理拓扑设置；使用上面单入口配置可设为 `1`。
H5/Admin 可继续通过各自 `/api/` 代理到 HTTPS API 域名，保持同源 Cookie 行为。
如果 API 入口还经过 CDN 或其他代理，需要按可信上游重新配置真实 IP 链路。
不要将宿主机 API 端口改为 `0.0.0.0:8000` 对外公开，因为 Django 信任代理协议头。

同机 HTTP 回源不会涉及后端证书校验；H5/Admin 到 API 域名若仍走 HTTPS，
应修复完整证书链/受信 CA 后开启校验。

## 5. 验证及日常操作

```bash
bash ./deploy-docker.sh status
bash ./deploy-docker.sh check
curl -i https://api.ledaban.cn/api/v1/health/ready/
```

公网请求应返回 200，`data.checks.database` 和 `data.checks.cache` 都为 `true`。
容器探针使用真实 Host 和受信协议头，防止 HTTPS 重定向被误判为就绪。
宿主机调试请求也必须这样发送（未附协议头的 HTTP 请求返回 301 是正常行为）：

```bash
curl -i http://127.0.0.1:8000/api/v1/health/ready/ \
  -H 'Host: api.ledaban.cn' -H 'X-Forwarded-Proto: https'
```

```bash
bash ./deploy-docker.sh logs api
bash ./deploy-docker.sh logs worker
bash ./deploy-docker.sh logs beat
bash ./deploy-docker.sh stop
bash ./deploy-docker.sh start
bash ./deploy-docker.sh manage grant_platform_admin 实际手机号
bash ./deploy-docker.sh manage changepassword 实际手机号
```

日志窗口 Ctrl+C 只退出日志跟踪，不停止服务。服务配置自动重启，Docker 引擎应启用开机启动。
日志按每容器 10MB × 5 文件轮转。API 日志不记录 URL 查询参数，保护 OAuth code 等信息；
1Panel 访问日志也应按相同原则配置。
Docker 标记 `unhealthy` 不会自动触发重启；需配置监控通知并排查，退出的进程才受 restart 策略处理。
Worker 探针验证当前节点回复，Beat 探针仅验证进程存在；调度延迟仍应监控任务中心。

## 6. 更新和回退

```bash
git pull --ff-only
bash ./deploy-docker.sh deploy
# 存在新迁移时，备份后改用 deploy --migrate。
```

脚本会保留镜像，`.deploy/current-image`、`previous-image` 和 `releases.log` 保存版本记录。
镜像标签包含源码提交号和构建时间，未提交源码也会进入镜像，正式发布前确认工作树内容。
不再需要拉取代码后在宿主机运行 uv sync。

```bash
bash ./deploy-docker.sh rollback
# 或指定本机仍保留的镜像（失败发布后按脚本打印的命令操作）
bash ./deploy-docker.sh rollback dazzy-api:实际旧版本标签
```

回退只切换应用镜像，保留当前 `.env.online`、Compose 配置和数据库结构，
数据库迁移必须保证旧代码兼容；删除字段等不兼容迁移不能靠切换镜像恢复。
回退健康检查通过后才更新版本记录。不要清理仍需回退的镜像或删除 `.deploy/`。
本项目不提供自动数据库恢复或 `down -v`，避免误删持久数据。

## 7. 本地回归验证

安装开发依赖后，可运行以下检查；测试使用模拟 Docker 和隔离配置，不连接业务数据库：

```bash
uv run python -m unittest deploy.test_build_dependencies deploy.test_python_mirror deploy.test_runtime deploy.test_release_script -v
uv run ruff check config/settings/production.py deploy
bash -n deploy-docker.sh
sh -n deploy/entrypoint.sh
```

镜像测试使用本机 uv、临时虚拟环境和回环测试索引，验证镜像实际供包、哈希拒绝及锁文件一致性，不访问外部包仓库。
这些测试不能替代实际 Linux 镜像构建、外部网络连通性和容器持久卷权限验证。
首次上线应预留维护窗口，在服务器完成前述健康检查和关键业务验收。

## 参考

- [Docker Compose 生产部署](https://docs.docker.com/compose/how-tos/production/)
- [uv 镜像构建与锁文件](https://docs.astral.sh/uv/guides/integration/docker/)
- [Docker 构建下载缓存](https://docs.docker.com/build/cache/optimize/)
- [清华 Debian 镜像配置说明](https://mirrors.tuna.tsinghua.edu.cn/help/debian/)
- [WhiteNoise 与 Django](https://whitenoise.readthedocs.io/en/stable/django.html)
- [Celery Beat 单实例要求](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html)
