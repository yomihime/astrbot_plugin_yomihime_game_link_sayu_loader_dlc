# 三个固定整包的原生对照

## 整包模式的 WebChat 收件

候选 8 使用 `--fresh-data --offline` 构建。安装前按三个归档、私有解释器、worker 和空资料目录封存，安装后核对实际运行的三个 worker。下表记录本机 WebChat 保存的回复编号。测试未使用真实游戏账户。

| 输入 | 候选 8 实收 |
| --- | --- |
| `/ygl sayusr query sr三月七别名` | #1067 收到含十个别名的卡片，附件与上一候选已人工核查的图逐字节相同。 |
| `/ygl sayugs query gs伤害乘区` | #1069 收到图文卡片，433,821 字节，与前次已核对原图和选项的附件相同。 |
| `/ygl sayuww query ww抽卡帮助` | #1071 收到 45 行、868 字的导入指南，已去掉宿主标题核对。 |
| 盲测 `/ygl sayusr direct sr添加丹恒别名列车小青龙` | #1073 收到上游确认，私有资料中角色 1002 的别名列表恰新增「列车小青龙」。 |
| 盲测 `/ygl sayuww query ww设置语言 en` | #1075 收到不支持提示，未保存偏好。原生普通用户能执行；Loader 的 ORM 写授权拦截了插件自有的个人设置。 |
| 盲测 `/ygl sayugs direct gs开启自动清红` | 前置绑定离线测试 UID 的 #1077 被写权限拒绝，#1079 随后只收到未绑定提示，未建立订阅。 |
| 补抽 `/ygl sayugs direct gs原神任务 神樱大祓` | #1081 六行正文逐行符合随包任务资料，包括稻妻、长流程、版本 2.0 和攻略地址。 |
| 补抽 `/ygl sayugs direct gs深渊怪物 5.8 12` | #1097 返回上游暂不可用。固定包缺 15 张怪物图标，空目录离线样本未收图。 |

前两轮的失败仍保留。候选 6 的星铁资源状态与原生离线阶段不同，原神御神签及鸣潮自定义权重帮助实收成功。候选 7 调整了通用资源下载的失败汇总，星铁已见题的阶段状态重新对齐。该候选的新星铁输入重复了已见别名回调，鸣潮导入抽卡只返回操作指引，原神帮助在 worker 的图片预算处失败。

对原神帮助的已见回归中，候选 8 的大图绘制产出与原生逐字节相同的 2970 × 5584 JPEG。公开入口 #1061 实收 1040 × 942 的宿主卡片，其中附图按高度 640 缩放；字号随之明显变小。私聊入口 #1063 仍受单张 PNG 的尺寸与可读性限制。普通私聊的配置卡 #1065 明确报告缺少读取账户清单的端口。

候选 8 停用后三路均未产生业务回复。重载后星铁 #1089 和原神 #1091 各收到一次；鸣潮 #1093 在加载期间返回暂不可用，等三个模块均达到 `loaded` 后 #1095 正常实收一次。回装保留的旧包并重新核对三个运行进程的文件摘要后，#1099、#1101、#1103 分别实收三路原有指令。

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

### 原神配置卡与管理员列表查询

第七个冻结候选的原神配置卡盲测返回“上游服务暂时不可用”。使用同一冻结 Worker 和原神快照在独立私聊、群聊重现时，`draw_config_card.py` 第 41 行调用 `GsBind.get_all_uid_list_by_game` 得到 `AttributeError`。同一原生整包的 `send_config_card` 允许普通权限 6 和全部会话类型。在全新空库中，普通私聊与普通群聊都实际调用了该适配器的 UID 列表、全库有效 Cookie 列表与 Stoken 列表，三个结果的长度均为 0。两个回调均发出 850 × 3640 的 PNG。插件须先取得原始列表，之后才对列表取长度并绘制数字。

Loader 的列表接口受独立的 profile 管理员授权保护。生产使用默认安装参数，私聊和群聊都无法开启管理员库存读源；调用受限方法会报告缺少受信端口。隔离测试通过安装者显式提供的源读取合成记录，授权限定到管理员、适配器、私聊作用域、当前任务和本次 profile 实例。授权期内返回完整列表，重复 UID、顺序、筛选条件及字段均可核查。运行 `scripts/account_enumeration_contract.py --output <私有路径>` 可核对四条绑定和五条账户记录上的 19 组原生查询。这个新接口没有改变冻结候选当时的盲测结果。

接线后的 Worker `6e3b0d64f5c8a1c2eea2da2c65af4ccc6015b673508f590a30a4d877078df6df` 使用同一原神快照完成了整包复测。普通私聊、群聊都在首次枚举时返回 `EnumerationUnavailable`，协议错误码为 `missing_port`。同一 Worker 在隔离私聊中获得管理员授权及一行合成绑定、一行合成账户后，实际回调发送 850 × 3640、1,522,987 字节的 PNG。同一进程随后处理的普通群聊仍被拒绝。

## 隔离适配器的入站帧

`worker/plugin_inbound.py` 复用现有的 `MessageReceive` 和 `Event`。专用适配器为每次投递新建 `EventResources()`，将原始帧、连接自身提供的 `connection_id` 和资源表传给 `decode_adapter_event`。解码失败返回 `None`，可跳过该帧；有效 Event 可交给 `sayu_compat.dispatch_event(event, mapped=True)`。图片、音频、视频的随机标识可在本次资源表中通过 `peek` 取回原始数据。解析器不会读取文件或访问媒体 URL。符合帧结构但内容损坏的文件段保留原生转换异常。

只有能验证帧来源、用户身份和适配器权限值的调用方才提供 `TrustedAdapterRoles`。此时受信 master 对应 0、superuser 对应 1；其余身份的原始权限小于 1 时，原生将其归为 2。缺少该权限来源时，解码器固定返回普通权限 6。帧内的 `sender.role`、额外权限字段和伪造的连接 ID 均不能赋予权限。YGL 的文本入口继续通过 `dispatch` 使用它实际提供的字段。

运行 `scripts/inbound_adapter_contract.py --output <私有路径>` 可用同一组 26 个输入比对原生 `decode_inbound_frame → handle_event` 与适配器结果。观察点位于原生段解析之后，连接标识和有效权限取自真实 handler 分支。比较包含 Event 所有字段、会话标识和随机媒体标识对应的载荷，涵盖群聊、私聊、频道、提及、引用图片、文件、合并转发、meta 身份回填及畸形输入。另用 `tests/test_inbound_adapter.py` 验证解码所得 Event 实际触发文件和 meta 回调。

## 候选 8 的 SQLite 事务

固定候选 `5ccbe1626bd8169ca12dc7bfe2d1d636ff4e45f2be83ac1c7a9febc927b69c55` 的星铁 Worker 为 `2b018d9f3892f4083997f4f21123dc329c37e69a6fb31723b413aba5d76e5b92`。对照从该归档加载 ORM，使用原生和 Loader 的公开 `with_session`、`with_read_session` 操作同一结构的合成表。两侧均使用 Python 3.12.10、SQLite 3.47.1、SQLAlchemy 2.0.57、aiosqlite 0.22.1。每侧都关闭第一个进程，再用第二个进程从原临时数据库读取。既有鸣潮 ORM 回执覆盖了上游模型与迁移；本次单独核对冻结 Worker 的事务行为。

| 场景 | 实际结果 |
| --- | --- |
| 成功返回；显式回滚后继续写 | 两侧自动提交成功调用。回滚前的写入消失，回滚后新写入的行保留。 |
| 显式提交后再写入并抛异常 | 两侧保留显式提交的第一行，丢弃第二行。 |
| 业务异常、重复主键、中途取消 | 两侧撤销未提交写入，分别传回业务异常、`IntegrityError` 和 `CancelledError`。取消后下一笔写入成功。 |
| 内层保存点失败，外层捕获错误 | 外层已有写入时，嵌套读取能看到它。两侧回滚内层失败的行，外层随后成功提交。 |
| 外层先写，再成功释放内层保存点，最后外层抛错 | 两侧均回滚内外两笔。 |
| 外层的首个数据库操作就是成功释放的内层保存点，随后外层抛错 | 固定原生保留内层行，冻结 Worker 回滚该行。进程重开后仍可确认此差异。 |
| 并发读写 | 同一用户的独立读者在写者提交前就能完成读取，未看到未提交的行。两位并发写者依次读到计数 0、1，最终值为 2。 |
| 原始 SQL 的跨用户可见性 | 原生共用 profile 数据库，另一用户及同一用户的另一适配器可读相同行集。Loader 的私有用户库按用户与适配器分开，另一用户仅看到自己的新行，另一适配器为空。 |

SQLite 实际执行的指令解释了首个保存点的差异。原生默认连接发出 `SAVEPOINT → INSERT → RELEASE`。由于之前没有 `BEGIN`，释放最外层保存点已提交写入。外层抛错时 SQLite 已没有可回滚的事务。Loader 主动设置连接的 `isolation_level=None`，由 SQLAlchemy 的事务开始事件发出 `BEGIN`。实际轨迹为 `BEGIN → SAVEPOINT → INSERT → RELEASE → ROLLBACK`。在已先发生普通 DML 的对照组，两侧均先发出 `BEGIN`。

固定原生 `base_models._session_wrapper` 仅在回调正常返回后显式提交，嵌套写使用 `begin_nested()`。源码注释与原生测试 `test_session_read_only_and_savepoint_isolation` 具体约束了内层失败时保留外层写入。该测试的外层已先执行 INSERT，未规定首个保存点成功、外层失败时必须保留内层行。另起原生对照进程，仅在引擎添加 Loader 的显式 BEGIN 配置，重新执行相同负载，首个保存点和整笔回滚即与 Loader 一致。这个诊断进程与原生基线分别保留。

若要在两侧保留完整的外层回滚，同样的显式 BEGIN 策略在隔离原生引擎上已验证可行。若改为复刻原生遗留行为，则需让驱动重新决定何时开始事务，影响还包括 DDL 和连续读取。纯 sqlite3 的双连接 WAL 对照中，驱动默认模式下建表后回滚仍留表，前后两次 SELECT 能看到期间另一连接的提交；显式 BEGIN 下建表可回滚，两次 SELECT 保持同一读视图。这里没有调整产品事务策略，也没有测量两种策略的性能。

从原生 3.12.10 环境运行 `scripts/sqlite_transaction_contract.py --freeze .sayu-local/freeze-8.json --profile starrail --output .sayu-local/receipts/freeze8-sqlite-transactions-trace.json`。脚本校验冻结 Worker 与依赖哈希，只使用新建的临时数据库。回执同时保存真实 SQLite 执行轨迹、可见行和重开结果；早先未加跟踪器的同候选回执单独保留。
