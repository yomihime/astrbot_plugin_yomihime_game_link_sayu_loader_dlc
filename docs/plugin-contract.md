# Sayu 插件契约基线

原生参照固定为 Genshin-bots/gsuid_core 的 `96cd7353be3100ff982bccc33e554961837c778e`。插件版本见 `profiles/lock.json`。选择这个提交是为了同时得到实现、锁文件和上游回归测试。该选择不代表三个插件曾针对同一版 Core 适配。[参考源码、文档及测试](https://github.com/Genshin-bots/gsuid_core/tree/96cd7353be3100ff982bccc33e554961837c778e)

## 如何核查范围

用支持 Python 3.13 语法的解释器运行 `python scripts/contract_inventory.py <原生源码根目录>`。脚本校验参考包全部 756 个 Python 文件的 Git blob 集合，从中列出模块、函数、方法、类型、字段和导入，再标出三个固定插件的显式导入，以及有同源对照的接口所对应的脚本。缺少逐项证据的符号保留为 `unverified`。初次扫描产出 22,771 行符号，包含重载、包内辅助实现和再导出。行数不能衡量兼容率。动态导入、继承方法、装饰器生成的接口和消息时序需要行为测试。上游 tests 中的契约与实际插件调用用于为下面各行挑选样本。

| 接口及行为 | 原生预期和定位 | DLC 实现与缺口 | 对照测试或尚需补的样本 |
| --- | --- | --- | --- |
| 包加载、启动和关闭 | `server.GsServer.load_plugin/cached_import` 解析目录及元插件，执行包代码。启动前、启动、关闭分别调度，优先级分组、组内并发 | 普通包按代际启动。三个已固定的游戏明确使用声明发现模式，未解析的上游初始化、嵌套包和模块需要逐个接通 | 同一 `contract_probe` 插件，两次启动和停用，同进程模块变量及隔代文件计数 |
| 服务与触发器 | `sv.Plugins/SV/SL` 管理前缀、开关和触发器。原生 `trigger.dispatch_sort_key` 比较前缀、服务优先级、类型和部分类型的关键词长度，`block` 截断后续处理 | worker 注册表与调用排序；文件事件、元事件和运行时管理尚需覆盖 | `cporder`、相同优先级的关键词、近似命令、同名装饰器；参照 `tests/test_handler_and_triggers.py` |
| 身份与授权 | `handler.msg_process/get_user_pml/_sv_authorized` 区分用户、群、机器人账号、角色和权限，服务以及插件分别有允许/排除名单 | 入口沿用 YGL 权限；可提供的身份字段受 SDK 限制，普通权限样本不代表管理员或群员角色 | `cpinspect payload`、受限服务、普通用户、拒绝冒充管理员；仍需真实宿主签发角色 |
| 模型、消息段、发送 | `models.Event/MessageReceive/MessageSend`、`segment.MessageSegment`、`bot.Bot.send` 支持有序消息、异步队列、不同段和返回记录 | 当前调用可返回文本或一张承载有序图文的卡片。原生多次发送的分帧、返回记录、回复引用、撤回和流式输出需宿主支持 | 对比分段边界、张数、像素、接收次序和错误；参照 `tests/test_bot_and_models.py` |
| 跨消息状态、数据目录 | `data_store.get_res_path` 创建目录，模块变量随进程存活，文件可跨启动 | 每个 profile 独立持久目录，代际内复用 worker。限定路径在插件目录内。账号、配置及玩家数据不能共用实例目录 | 同用户连续调用、重载、越界路径；需补多用户并发及数据库事务 |
| 调度 | `aps.scheduler` 使用 AsyncIOScheduler，时区 Asia/Shanghai，支持 interval、cron、date、查询、移除任务和 misfire 策略 | 代际负责调度器的启动与退役。后台主动投递取决于独立的宿主权限 | 间隔任务有运行记录，停止后计数稳定；补错过触发、同步任务与主动目标投递 |
| 配置 | `utils.plugins_config.models/gs_config.StringConfig` 有命名实例、类型、缺省值、模板调和、路径迁移和修改落盘 | 已接入常用标量、字符串列表、字典、图片配置，支持隔离文件读写。重复组、时间范围、按钮、定时刷写、配置管理器、兼容老格式等仍待验证或实现 | `scripts/config_contract.py` 双端运行同一事务，比较初值、错型、缺项、同名身份、磁盘 JSON、路径迁移 |
| 账号、数据库与外部 API | `utils.database.models/base_models` 的绑定、会话、事务、读写门和授权；游戏 API 单独依赖服务 | 仅自有 SQLite 绑定子集已接入，其他数据库模型仍待实现。YGL 账号操作需要显式绑定。现有命令均声明只读，写绑定仅在测试夹具显式授予写权限后对照，不能把宿主凭据传入 worker | 已对比选取、切换、重复绑定、删除与跨进程重开。仍需对照回滚与只读事务；参照 `tests/test_database_and_config.py` |
| 多轮交互、文件 | `Bot.receive_resp/receive_mutiply_resp/send_option` 等待特定会话、超时、清理；`SV.on_file` 接收适配器文件段 | 当前 YGL 命令只附带文本与事件引用，无法提供完整原始文件或未匹配命令的后续应答 | 内存适配器已对照单用户和多人回复；超时、停用取消、宿主文件句柄与过期仍待接通 |
| 指定目标发送与订阅 | `Bot.target_send/unsend/ban`、`subscribe` 绑定目标、平台能力及发送回执 | SDK 提供受控的订阅和任务接口，缺少插件可调用的通用目标投递、撤回或禁言授权。不得从字符串目标重建授权 | 已在内存目标核对两帧目标、文本及文件字节；真实回执、撤销、过期与部分成功待验证 |
| 插件管理 | `Plugins.set`、`SV.set/enable/disable`、`GsServer`、`utils.plugins_update` 涉及服务开关、发现、安装和管理权限 | 同一 profile 的启用状态和优先级可持久化。普通包启动期或隔离管理夹具可写，聊天、退役任务和其他 profile 无权修改。YGL 管理 DLC 的启用退役。原生管理接口不可直接更改其他插件、宿主进程或宿主 ACL | 合成管理调用，核对持久化与拒绝未授权目标，无真实共享配置写入 |
| 工具、图片和资源 | `i18n`、`logger`、`pool`、`utils.image`、`help`、`download_resource`、模板及上传 API 有独立格式、日志、缓存和错误语义 | 部分图片/数据和公开素材适配已用于实际查询。语言、日志、上传、运行时下载与账号服务尚未逐项对照 | 输入缺失、损坏图片、限额、下载失败、语言切换；用真实游戏原回调校验 |
| 扩展面 | `ai_core` 的 hooks、kits、tools，`webconsole`、HTTP 服务、消息历史等提供额外可调用接口 | 仍待实现。无法把原生 Web 服务或管理凭据直接暴露给插件 | 原生 `tests/test_agent_hooks_registry.py`、`tests/test_tool_registry.py` 和 `docs/HTTP_AGENT_API.md`；从无副作用的注册与注销开始 |

## 需要 YGL 公共接口的部分

1. 给每次调用签发不可伪造的会话与角色视图，包括适配器、机器人身份、是否群聊及权限判定结果。公开的 `InvocationView` 本身不授予权限，普通聊天不保证有 `conversation_kind`。需要宿主执行授权并支持撤销，以便区分管理员和普通用户。
2. 增加一组绑定到调用或已授权订阅的发送操作，明确顺序、消息边界、部分失败、真实投递状态及只可在相应目标内使用的消息引用。`CapabilityResult` 只能描述本次计算给出的文档。资源已入缓存无法证明用户收到了每个消息段。
3. 为会话等待者提供可取消的事件订阅，将文件、引用消息和应答封装成带期限的读取句柄。代际变化、授权撤销、超时后失效。`MessageAccess.read` 的文本和事件引用不足以还原这些输入。
4. 为可管理的操作提供能力清单和目标级授权，将自身服务开关与跨模块管理分开。YGL 保留模块启停及账号权限控制，DLC 不操作共享数据库、宿主重启或其他模块。

## 复现对照

将固定 GsCore 提交检出到独立目录，以其 `uv.lock` 安装独立测试解释器。Loader 使用自己构建的私有解释器。运行原生用例前，脚本将固定 Git 清单中的文件校验并复制到临时目录。凭据、共享数据库、外部适配器和启动中的 GsCore 服务均无需参与对照。

| 可执行检查 | 实际观察与判定范围 |
| --- | --- |
| `python scripts/loader_contract.py --core <YGL源码> --python <Loader解释器> --output <loader.json>`，然后用原生解释器执行 `scripts/native_contract.py --reference <GsCore源码> --output <native.json> --compare-loader <loader.json>` | 同一插件，两个代际，13 次输入。12 项共享结果相符。三次状态回复为 `1/1`、`2/2`、`1/3`。顺序为 first、second，阻断第三个回调。两边记录受控异常，下一条命令仍可用。停止后定时器安静。普通用户拒绝管理命令；配置的原生 master 被允许，Loader 缺少宿主签发的同等身份，拒绝同名用户。比较保留了这项差异。 |
| 用两端解释器分别执行 `scripts/config_contract.py --output <结果.json>`，原生侧另加 `--reference <GsCore源码>` | 初值、修改、类型拒绝、缺项、迁移、磁盘及实例身份的观察结果相同。 |
| 用原生测试解释器执行 `scripts/binding_contract.py --reference <GsCore源码> --output <结果.json>` | 同一组 UID 插入、重复、选取和切换、删除、群字段及跨进程重开，在隔离数据库上结果一致。Loader 另测公共入口、错误用户、适配器与空身份拒绝。 |
| 用原生测试解释器执行 `scripts/service_contract.py --reference <GsCore源码> --output <结果.json>` | 同一插件的服务停用、重新启用、插件级开关及重开后的选择一致；对没有权限的用户和过期上下文仍拒绝。 |
| 用原生测试解释器执行 `scripts/target_contract.py --reference <GsCore源码> --output <结果.json>` | 内存收件器收到两个顺序一致的目标帧、文本及非 UTF-8 附件。单用户与多人应答的用户和文本相符。生产端没有这项授权时，显式拒绝调用。 |
| `python scripts/audit_package_init.py`，再加 `--writable-source` 运行一次 | 固定源码普通导入仍有三处最早缺口。星铁为 `gsuid_core.utils.image.image_tools`，原神为 `gsuid_core.utils.api`。鸣潮先尝试在源码树内创建目录，允许写临时副本后缺 `sqlmodel`。这些是 Loader 目前未支持的导入路径。 |

完整插件端契约还包括输出错误的传播方式。原生参考把处理器异常写入错误报告；Loader 将该错误返回 YGL。定时器对照验证了运行与停止，未要求两端恰好触发相同次数。文本和图片在 WebChat 中也可能被 YGL 合成为一条回复，原生发送帧和最终收件要分别核验。

## WebChat 验收记录

在本机 YGL Core 0.2.11、三个声明模式模块同时启用的条件下测试。第一次集成时，原神查询因为适配器日志帧没有接收端而退出。为此增加了代际内有界的日志通道；宿主清单只显示各级别计数。修复后，原三条指令分别收到三月七别名图、伤害乘区图和完整抽卡指南。停用组三路均返回模块不存在；重载组每条收到一次业务结果。实际重新安装上一轮包后，三路仍可使用旧包绑定的依赖完成查询。

首个冻结候选的三条新样本保持原判。星铁 `sr角色图鉴 姬子` 下载到了正确的原图，卡片的透明 PNG 在最小可读尺寸仍超出编码预算，未送达。原神 `gs查委托望舒须筑阶` 的四项攻略内容正确，但换行被宿主压平，没有满足预登记的四行格式。鸣潮 `ww菜单` 返回含横幅、绑定示例、业务分类和六个命令选项的完整目录图。修复采用支持透明度的调色板编码以及按公开宿主接口投递多行文本；这些已见样本仅作为后续回归。
