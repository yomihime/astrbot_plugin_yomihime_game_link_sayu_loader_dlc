# Sayu Loader DLC

Sayu Loader 将上游的聊天服务接入 AstrBot 的 Yomihime Game Link（YGL）。插件保留原来的服务装饰器、匹配词、回调和游戏数据。AstrBot 只加载生命周期外壳，游戏依赖运行在各自的私有 Python 环境中。运行时无需安装、启动或连接 GsCore。

| 上游 | 固定版本 | 路由 | 示例 |
| --- | --- | --- | --- |
| StarRailUID | 0.2.0 · `739a80a` | `sayusr` | `/ygl sayusr query sr三月七别名` |
| GenshinUID | 6.4.0 · `319b134` | `sayugs` | `/ygl sayugs query gs伤害乘区` |
| XutheringWavesUID | 3.8.1 · `7248f8c` | `sayuww` | 私聊 `/ygl sayuww query ww抽卡帮助` |

星铁和原神的 `query` 走 YGL 公开卡片。需要复制文本时，可在私聊中把 `query` 换成 `direct`。鸣潮的 `query` 沿用私聊投递。每个入口都由 YGL 校验调用权限。

## 服务如何加载

1. `profiles/lock.json` 固定 Git 提交与源包 SHA-256。构建器从 Git 对象生成快照，收录完整上游 Python 包、随包资源、根图标与许可证。三个 profile 同时声明加载模式。
2. YGL 通过 `ygl.control/1` 接入 DLC 并注入 SDK。每个模块代际在激活时验证 worker 和源包的摘要，展开一份自己的上游快照。停用时先取消正在运行的请求、回收进程，再清理该代际的源码目录。
3. 每个活动模块有一个常驻 worker。普通包执行包代码和生命周期钩子，按代际管理调度器。三个固定游戏显式使用声明发现模式，选择能解析依赖的上游回调，并在 worker 清单中记录跳过原因。普通包导入失败会返回启动错误。
4. 每个 profile 独享持久数据目录及稳定的用户化名。重载保留文件并重建 Python 模块状态。独占写锁防止同一 profile 同时启动两个调度器。星铁和原神可按上游声明的静态目录取图，鸣潮资源按固定 Git 树校验。
5. 原生参照版本、行为差异、双端测试和需要扩展的 YGL 接口见[插件契约清单](docs/plugin-contract.md)。

鸣潮资源版本固定为 `88d84d8d73855dd443c1e568164324ff5ff090d0`。涉及线上素材的指令需要能访问对应发布方。

当前实现面向只读聊天。可用性取决于回调和初始化代码的依赖，包括本地数据是否随固定版本提供。发现器会排除需要未接入接口、受限权限或不支持初始化行为的服务。已接入的接口可以供多个服务共用。登录、签到、账户库、跨会话投递、主动推送、Web 服务、文件交互和完整游戏资源下载流程超出当前范围。公开卡片接受有界的 PNG/JPEG。私聊可直接复制纯文本，图文混排时将各段按发送顺序绘入一张 PNG。图片超过宿主单次投递预算时会等比缩放。静态图须能安全解码，并在缩放后保持可读尺寸。

### 初版冻结候选的 WebChat 验收

三个模块同时启用，每条消息从空数据目录开始。候选冻结后独立抽取的四条新指令中，三条收到了可核对的业务结果。

| WebChat 指令 | 收件核对 |
| --- | --- |
| `/ygl sayusr query sr突破材料 刃` | 刃的图片列出等级突破、技能和行迹材料，等级突破含 65 个天人遗垢。 |
| `/ygl sayugs direct gs腐殖之剑能给谁` | 私聊文本列出命中武器名的配装记录，包括班尼特、琴和阿贝多。 |
| `/ygl sayuww query ww替换帮助` | 收到上游面板替换说明图，含角色共鸣链、武器谐振和合鸣套装示例。 |
| `/ygl sayusr direct sr遗器 云无留迹的过客` | 未收到遗器图，原因见下节。 |

### 已知上游限制

StarRailUID 0.2.0 的遗器图鉴按中文套装名遍历 `SetId2Name` 的键，但固定版本的数据以编号为键。输入 `sr遗器 云无留迹的过客` 时，上游找不到编号；资源库中存在编号 101 的原图。该查询保留上游实际行为。

## 本机构建与检查

下例使用 Windows Python Launcher。每个 profile 的解释器和依赖安装到仓库忽略的 `.sayu-local/<profile>/env-*`。变更依赖清单或基础 Python 后，prepare 会新建环境，安装成功才更新 `environment.json`。构建器将选中的解释器绝对路径写入安装 ZIP。移动源码仓库或更换机器后需重新构建。

```powershell
py -3.12 scripts/prepare.py starrail
py -3.13 scripts/prepare.py genshin
py -3.13 scripts/prepare.py waves
python scripts/build_local.py
foreach ($profileName in 'starrail', 'waves') {
    $dir = ".sayu-local/$profileName"
    $envName = (Get-Content "$dir/environment.json" -Raw | ConvertFrom-Json).environment
    & "$dir/$envName/Scripts/python.exe" -m unittest discover -s tests
}
python scripts/check_local.py --core <本地-YGL-Core-源码目录>
```

将 `dist/` 中生成的 ZIP 通过本机 AstrBot 的插件安装入口上传。单元测试使用独立样例验证发现、匹配、消息与打包。SDK 检查使用上面的三条真实上游指令，核对原神 JPEG、鸣潮完整文本、星铁别名和停用重载行为。给检查脚本加上 `--list-commands` 可查看当前候选实际注册的服务、触发方式和前缀要求。该选项只列注册项；需要公开数据或外部素材的查询仍取决于数据是否可用。WebChat 实收需单独验收。

回退时，通过同一个安装入口上传预先保留的本机旧版 ZIP。旧包直接使用构建时写入的解释器、worker 和源码归档路径。请保留这些文件及解释器依赖的基础 Python，含早期版本使用的 `.sayu-local/<profile>/venv`。停用 DLC 可撤下其全部路由。

外壳沿用 yomihime 的 Steam DLC 生命周期实现，采用 AGPL-3.0。三个上游的许可证随各自的源码快照保存。
