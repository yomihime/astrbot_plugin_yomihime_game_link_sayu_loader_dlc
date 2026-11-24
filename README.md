# Sayu Loader DLC

Sayu Loader 将上游的聊天服务接入 AstrBot 的 Yomihime Game Link（YGL）。插件保留原来的服务装饰器、匹配词、回调和游戏数据。AstrBot 只加载生命周期外壳，游戏依赖运行在各自的私有 Python 环境中。运行时无需安装、启动或连接 GsCore。

| 上游 | 固定版本 | 路由 | 示例 |
| --- | --- | --- | --- |
| StarRailUID | 0.2.0 · `739a80a` | `sayusr` | `/ygl sayusr query sr三月七别名` |
| GenshinUID | 6.4.0 · `319b134` | `sayugs` | `/ygl sayugs query gs伤害乘区` |
| XutheringWavesUID | 3.8.1 · `7248f8c` | `sayuww` | 私聊 `/ygl sayuww query ww抽卡帮助` |

星铁和原神的 `query` 走 YGL 公开卡片。需要复制文本时，可在私聊中把 `query` 换成 `direct`。鸣潮的 `query` 沿用私聊投递。每个入口都由 YGL 校验调用权限。

## 服务如何加载

1. 原生参照为 GsCore 0.12.2 的 `96cd7353`。`profiles/lock.json` 固定三款插件的提交、完整 Git 文件树的归档摘要及包加载模式。快照包含外层目录、内层包、初始化文件和随包资源。
2. YGL 通过公开 SDK 启停 DLC。每个模块代际在自己的私有解释器中验证并展开已固定的源码。一个常驻进程承载该插件的服务、状态和调度器，重载会更换进程。
3. Loader 按原生包布局发现并执行各入口。插件的装饰器负责注册回调。和固定原生版本一样，子模块抛出异常后继续尝试其余模块。管理清单逐模块给出导入结果；离线双端检查可进一步定位错误。数据库迁移、启动钩子和实际聊天收件分别验收。
4. 每个 profile 独享数据、配置、SQLite 和用户化名。代际退役时释放写锁、调度与临时源码。依赖变更另建私有环境，旧包仍能使用原解释器。资源来源和投递能力受本地授权约束。
5. 插件公开接口、原生预期、当前差异与验证方法见[插件契约清单](docs/plugin-contract.md)。

公开卡片和私聊支持有界的静态图与文本。图文混排按发送顺序绘入同一张 PNG。账号请求、指定目标投递和会话应答可使用隔离测试身份与收件器做契约对照。真实宿主中的角色、目标投递回执和附件读取仍有公开接口缺口。

### 历史声明模式候选的 WebChat 验收

以下记录对应早期候选。三个模块同时启用，每条消息从空数据目录开始。候选冻结后独立抽取的四条新指令中，三条收到了可核对的业务结果。

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

用 `python scripts/build_local.py --fresh-data --offline` 可为离线验收绑定全新的私有数据目录。资源请求会留下失败诊断，需要外部数据的业务须另外验证。

将 `dist/` 中生成的 ZIP 通过本机 AstrBot 的插件安装入口上传。单元测试使用独立样例验证发现、匹配、消息与打包。SDK 检查使用上面的三条真实上游指令，核对原神 JPEG、鸣潮完整文本、星铁别名和停用重载行为。给检查脚本加上 `--list-commands` 可查看当前候选实际注册的服务、触发方式和前缀要求。该选项只列注册项；需要公开数据或外部素材的查询仍取决于数据是否可用。WebChat 实收需单独验收。

回退时，通过同一个安装入口上传预先保留的本机旧版 ZIP。旧包直接使用构建时写入的解释器、worker 和源码归档路径。请保留这些文件及解释器依赖的基础 Python，含早期版本使用的 `.sayu-local/<profile>/venv`。停用 DLC 可撤下其全部路由。

外壳沿用 yomihime 的 Steam DLC 生命周期实现，采用 AGPL-3.0。三个上游的许可证随各自的源码快照保存。
