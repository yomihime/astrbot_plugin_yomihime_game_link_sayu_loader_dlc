# 匿名图标的版本核对

原神固定插件的 `icon2Name_mapping_6.4.0.json` 声明 292 个圣遗物图标 ID。genshin-db 的固定发布索引包含 305 个不同的官方装备图标 URL。两份清单有 285 个交集。2026 年 11 月 25 日从米哈游原始 HTTPS 地址匿名读取时，215 个返回 PNG，70 个返回 HTTP 404。打包清单仅包含成功返回且核验通过的 215 项。[固定发布索引](https://github.com/theBowja/genshin-db/blob/5dd639f66982fb8659d618bea6f9a3ee5b0f7d53/src/data/image/artifacts.json)

每项成功响应均核对官方 HTTP `Content-MD5`、字节数及 PNG 尺寸，再计算写入本版本清单的 SHA-256。发布索引证明 URL 的归属。清单中的 SHA-256 来自本次官方响应的完整字节。212 张图片为 256×256，另 3 张为 512×512。

构建时复核插件压缩包、清单摘要和每项 ID。将对应发布索引保存到本机后，可再用 `python scripts/verify_published_images.py --profile genshin --snapshot <固定插件归档> --publisher-index <索引文件>` 独立核对每个完整 URL。生产只对登记地址接受匿名 GET。资源代理在交付字节前核对媒体类型、字节数、SHA-256 和 PNG 尺寸，沿用原有 HTTPS、连接目标、请求头和解码限制。官方文件变动时需要重新核定清单。

星铁固定 Wiki 回调从发行目录按 ID 读取图片。鸣潮部分资源使用自己的 SHA-256 清单。两者沿用原有资源通道；运行时返回的 URL 和 API 模型示例均不能登记新来源。MiniGG 提供圣遗物资料的可用性仍单独决定原神回调能否进入图标下载阶段。
