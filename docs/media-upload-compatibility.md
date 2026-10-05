# 全端照片与视频上传兼容

## 覆盖范围

所有已有照片入口共用服务端内容校验：用户头像、活动封面、入驻生活照、评价图片、
售后/投诉/举报附件、达人身份认证、履约照片和后台运营素材。JPG/PNG/WebP 保持原流程，
HEIC/HEIF 自动转 JPEG，不信任手机的扩展名/MIME。认证正反面水印、私有照片权限不变。
Live Photo 的静态主照片可用；不会从 HEIF 自动提取实况动画。客户应用目前无视频上传入口，
本次不新增发布视频功能；已有达人展示视频入口统一兼容 MOV/MP4 和 HEVC。

视频先校验容器，再完整解码转 H.264/yuv420p + AAC MP4（无音轨不添加声音）。按原方向等比
缩放，最高横屏 1920×1080 / 竖屏 1080×1920、30fps；HLG/PQ HDR 基础层转 SDR，不保留
杜比视界动态效果。输出使用 faststart，移除位置等额外元数据。保留第一条正常视频与首条音轨，
不公开原始文件；确认输出大小、编码及视频时长后才上传 COS。

现有 5/8/10MB 图片、2500 万像素、50MB 视频限制保留，转换结果也受大小限制。达人展示最多 2 个视频，每个不超过 10 秒，服务端在转码前后核验并保存时长。
视频输入最多 4096×4096 像素（最长边 8192），不支持损坏文件、纯音频或其他视频容器。
超限、解码失败、转码超时会明确报错，不通过改后缀或关闭校验绕过。历史素材不批量修改。

## 部署

1. **重建并部署 API 镜像**，不能只重启旧镜像。Dockerfile 已安装 FFmpeg（包括 FFprobe），
   Python 锁文件已有 pillow-heif；本批视频时长规则需执行 `mediafiles.0004_mediaasset_duration_ms` 迁移。首次接单学习还需 `providers.0023_first_order_training`，见[开通流程](provider-onboarding-review-workflow.md)。
2. 非 Docker 部署安装含 MOV/HEVC 解码器、libx264/AAC 编码器、zscale/tonemap 过滤器的
   FFmpeg，并运行 `uv sync --locked`。默认使用 PATH 内 `ffmpeg`、`ffprobe`；可用
   `MEDIA_FFMPEG_PATH` / `MEDIA_FFPROBE_PATH` 指定完整可执行路径。缺失工具返回 503，
   不静默保存无法播放的原视频。保持解码库及镜像安全更新。
3. 同步发布用户端、达人端、后台。小程序配置正确的绝对 `VITE_API_BASE_URL`；在微信后台
   配置 API 的 uploadFile 合法域名及实际媒体域名。格式兼容不替代微信域名白名单。
4. 网关请求体至少 60MB；沿用 90 秒 API worker、100 秒代理读超时和 180 秒客户端视频
   上传超时。处理阶段两次探测各最多 8 秒，转码最多 45 秒，超时终止子进程并清理临时文件。
   大型 4K/长视频超时请压缩/裁剪重试，不承诺任何长度都能同步处理。
5. 视频处理是同步且 CPU 密集型，解码/编码各限制 2 线程、过滤器 1 线程。监控 API
   worker 饱和、内存与临时磁盘空间；高并发放量前应迁移至有队列和状态查询的独立转码任务，
   不宜仅增加 API worker。每个请求暂存源文件、结果与上传临时副本；生产 Compose 的 API
   `/tmp` 临时内存盘从 64MB 调至 512MB（仅上限，按需占用），适配默认两个同步 worker。
   容器重建时必须应用新 Compose 配置；预留这部分内存和解码内存，增加并发前重新评估。

## 回归

安装 FFmpeg/FFprobe 后运行（隔离内存数据库、模拟 COS，不接触业务数据）：

```bash
uv run python scripts/check_wechat_auth.py mediafiles.tests mediafiles.test_heif_uploads mediafiles.test_identity_watermark mediafiles.test_video mediafiles.test_video_normalization backoffice.test_assets providers.test_media
```

真实编解码用例依赖 FFmpeg；缺失时会标记跳过，**部署验收不可接受这组被跳过**。
合成样本覆盖 H.264、HEVC、10-bit HLG、旋转矩阵、有声/无声、错误 MIME、截断、输出超限、
依赖缺失、超时与临时文件清理。另在用户端运行 `npm run test:media-uploads`，达人端运行
`npm run test:identity-upload`，构建两端 H5/小程序及后台。

上线还需真机验收：

- iPhone 相册 HEIC、相机拍摄、Live Photo 静态图分别测试头像、封面、评价和附件；检查审核端预览。
- iPhone 竖屏/横屏普通与 HDR MOV、有声与静音视频上传，安卓小程序及 H5 验证方向、色彩、声音和完整时长。
- 权限拒绝、取消选择、弱网、超大文件、会话过期和再次上传应有明确结果。
- 身份证正反面仍有“仅用于达人验证”水印，私人材料仍只能通过受控签名地址访问。

参考：[FFmpeg 命令行](https://ffmpeg.org/ffmpeg.html)、
[MOV 解复用器及外部引用开关](https://ffmpeg.org/ffmpeg-formats.html#mov_002fmp4_002f3gp)、
[色彩转换过滤器](https://ffmpeg.org/ffmpeg-filters.html)。
