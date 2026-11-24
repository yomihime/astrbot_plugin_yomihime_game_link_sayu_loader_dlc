# 三个固定整包的原生对照

2026-11-24 在 Windows 上，将同一个游戏归档分别交给固定版 GsCore 和 Loader。三组结果逐模块比对了导入成功、失败异常及其原因，并统计可触发的服务、回调和触发词。原生参照为 `Genshin-bots/gsuid_core@96cd7353be3100ff982bccc33e554961837c778e`，运行前核对了清单中的 1,592 个 Git blob。游戏包提交和归档 SHA-256 由 [profiles/lock.json](../profiles/lock.json) 固定。

| 插件 | 上游提交 | 两侧成功导入 / 发现模块 | 有路由的服务 | 不同回调 | 触发项 | 原生调度任务 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| GenshinUID 6.4.0 | `319b13465a60f96a2db2d5d569ca6a15128a9f2f` | 39 / 42 | 57 | 89 | 213 | 10 |
| StarRailUID 0.2.0 | `739a80a26b7a137b6b7b7d45435c769822de06d0` | 23 / 23 | 28 | 41 | 102 | 2 |
| XutheringWavesUID 3.8.1 | `7248f8cda8aa3164aca560e145e306a829e3c49d` | 33 / 33 | 85 | 106 | 653 | 5 |

表中三个路由计数列在两侧一致。原生创建的服务实例总数分别为 57、30、86，其中部分服务未挂载触发器。回调按解开装饰器后的函数对象去重，触发项按服务、匹配类型、关键词及回调去重。原生前缀展开后的数量也分别为 213、102、653。此轮没有逐一调用表中的业务回调。

原生解释器均为 CPython 3.12.10，优先从自己的依赖环境导入，再搜索游戏 profile 的 site-packages。Loader 的原神、星铁、鸣潮环境分别为 CPython 3.13.9、3.12.10、3.13.9。不同 ABI 的扩展只可在与之兼容的解释器中导入。每次运行均单独解包，使用新建的临时数据目录，拦截外部网络及进程。插件能够在归档的临时副本下建立资源目录；星铁建立了一个，鸣潮建立了两个。原归档保持哈希一致。

## 导入与生命周期

原神两个功能模块在两侧都触发了同一条上游导入异常。`genshinuid_gachalog/lelaer_tools.py` 第 9 行引用 `GET_GACHA_LOG_URL`，`genshinuid_mysbbscoin/get_mihoyo_bbs_coin.py` 第 9 行引用 `BBS_LIKE_URL`。固定 Core 的端点模块没有导出这两个旧名。原生独立执行第一条导入语句也得到 `ImportError`。第三项来自 `genshinuid_update`，隔离进程没有可用的 Git 可执行程序。两个加载器均继续导入其余模块。

| 原生阶段 | 原神错误记录 | 星铁错误记录 | 鸣潮错误记录 |
| --- | ---: | ---: | ---: |
| 启动前钩子 | 0 | 0 | 0 |
| 启动钩子 | 1 | 3 | 3 |
| 调度器启动、停止和关闭钩子 | 0 | 0 | 0 |

原神启动钩子的资源请求遇到离线 DNS 限制。星铁的启动钩子两次请求资源域名、一次尝试删除允许目录之外的文件，三次均受隔离边界拦截。鸣潮导入时记录过 `waves_build.wuwacalc` 缺失，模块本身完成导入；启动时两次记录该模块缺失，另记一次资源更新失败。三个原生进程实际启动并停止了调度器。原生会捕获部分钩子异常，记录日志后继续；回执按错误出现的阶段计数。

Loader 的原神返回 `started_and_stopped_with_module_errors`，星铁、鸣潮返回 `started_and_stopped`。三者当次分别记录了 1、4、5 项错误级日志。两侧日志的记录位置不同，退出和调度状态只能证明阶段已执行。远端资源更新、已登录账户业务和真实宿主消息收发均需独立验证。

## 重现

准备 `.sayu-local/reference` 下的固定 GsCore 源码、其 `uv.lock` 对应的 3.12.10 环境及三个游戏 profile。参照可用该提交的 Git 工作树；使用归档时需将对应的 Git 树清单放在相邻的 `reference-tree.json`。`scripts/prepare.py <profile>` 准备游戏快照及私有依赖环境。下列 PowerShell 命令从仓库根目录运行，所有原始回执留在忽略目录。

```powershell
$nativePython = ".sayu-local/reference-env/venv/Scripts/python.exe"
$receipts = ".sayu-local/receipts"
New-Item -ItemType Directory -Force $receipts | Out-Null
foreach ($game in @("genshin", "starrail", "waves")) {
    & $nativePython scripts/native_package_contract.py --profile $game --output "$receipts/native-$game.json"
    if ($LASTEXITCODE) { throw "Native run failed for $game" }
    python scripts/audit_package_init.py --profile $game --writable-source --lifecycle --output "$receipts/loader-$game.json"
    if ($LASTEXITCODE) { throw "Loader run failed for $game" }
    python scripts/native_package_contract.py --compare-native "$receipts/native-$game.json" --compare-loader "$receipts/loader-$game.json" --output "$receipts/paired-$game.json"
    if ($LASTEXITCODE) { throw "Comparison failed for $game" }
}
```

查看配对回执的 `same_import_outcomes`、`module_observations`、`native_routes` 与 `loader_routes`。比较器校验两侧使用同一归档和 profile 依赖哈希。原生回执还给出各阶段错误、包内栈与临时源码写入记录。历史探针先后遇到插件搜索路径遗漏、数据库引擎初始化顺序错误，以及 Windows 的配置临时文件 `WinError 32`。最后一项来自审计器拒绝包装刚打开的私有文件描述符。修正审计器并在全新临时目录重跑后，上述故障消失；对应早期回执仍保留为探针失败记录。

## 账户和公开帖子请求

用上述原生解释器运行 `scripts/mys_account_contract.py --output <私有路径>` 及 `scripts/mys_bbs_contract.py --output <私有路径>`。账户用例覆盖 17 个结果，包括 UID、地区、缓存、缺失字段、接口错误及海外星铁查询。帖子与合集用例覆盖 5 个结果。原生侧从临时 SQLite 读取测试身份并实际生成请求，aiohttp 将请求写入内存传输，原响应解析器读取模拟的 HTTP/1.1 回复。Loader 侧的 httpx2 MockTransport 接收已准备的请求。请求行、查询参数、脱敏请求头、异常和回复内容均可从回执核查。

账户所属的私聊事件、适配器及执行任务必须保持活动。组聊、其他发送人、另一个适配器及继承上下文的后台任务均没有取得本次凭据的权限。并发用户和取消中的请求另由 `tests/test_mys_account.py` 在 MockTransport 处交错执行。公开帖子请求不会附加账户 Cookie 或设备标识。需要签名的 Loader 成功样本使用显式注入的测试发行者；生产宿主没有签发凭据的接口，缺少发行者的公开帖子请求在发出 HTTP 前停止。原生签名摘要、移动客户端请求头、设备认证及线上接受结果只能在具备相应条件时另行验证。

## 隔离适配器的入站帧

`worker/plugin_inbound.py` 复用现有的 `MessageReceive` 和 `Event`。专用适配器为每次投递新建 `EventResources()`，将原始帧、连接自身提供的 `connection_id` 和资源表传给 `decode_adapter_event`。解码失败返回 `None`，可跳过该帧；有效 Event 可交给 `sayu_compat.dispatch_event(event, mapped=True)`。图片、音频、视频的随机标识可在本次资源表中通过 `peek` 取回原始数据。解析器不会读取文件或访问媒体 URL。符合帧结构但内容损坏的文件段保留原生转换异常。

只有能验证帧来源、用户身份和适配器权限值的调用方才提供 `TrustedAdapterRoles`。此时受信 master 对应 0、superuser 对应 1；其余身份的原始权限小于 1 时，原生将其归为 2。缺少该权限来源时，解码器固定返回普通权限 6。帧内的 `sender.role`、额外权限字段和伪造的连接 ID 均不能赋予权限。YGL 的文本入口继续通过 `dispatch` 使用它实际提供的字段。

运行 `scripts/inbound_adapter_contract.py --output <私有路径>` 可用同一组 26 个输入比对原生 `decode_inbound_frame → handle_event` 与适配器结果。观察点位于原生段解析之后，连接标识和有效权限取自真实 handler 分支。比较包含 Event 所有字段、会话标识和随机媒体标识对应的载荷，涵盖群聊、私聊、频道、提及、引用图片、文件、合并转发、meta 身份回填及畸形输入。另用 `tests/test_inbound_adapter.py` 验证解码所得 Event 实际触发文件和 meta 回调。
