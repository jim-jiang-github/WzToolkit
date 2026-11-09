# WzToolkit

把冒险岛的地图 / 怪物 `.wz` 资源**导入 → 转换 → 浏览 → 导出**的工具：
**CLI 优先**（`wztool`，供 AI / 其他程序直接调用），附带可选 Web 页面。
做**怪物素材导出**（逐帧动画 + 锚点 manifest）与**地图导出**
（map.json / 贴图 / 1366×768 画面窗口 / 小地图 / 截图找图），
不含训练集、自动打怪之类的功能。

```
选一个放 .wz 的目录（工作区）
   │  wztool convert …  把选中的 .wz 转成 .nx（.nx 落在 .wz 旁边）
   ▼
wztool mobs / maps    列出全部怪物 / 地图（--json 全字段）
   ▼
wztool mob-export 0100100
   └─ .exports\mobs\<id>\{逐帧 PNG, manifest.csv}   manifest 含 anim/frame/w/h/ox/oy/delay

wztool map-export
   └─ .exports\maps\<id>\{map.json, img\, window.png}
                                    map.json 含 life（刷怪表）/ foothold / ladderRope / portal；
                                    window.png = 1366×768 导出窗口（--view 选位置）
wztool map-minimap 101040000        渲染游戏内小地图（miniMap canvas + 标记）→ PNG
```

## 首次使用

```powershell
# 1) 直接用：五个原生工具（NoLifeWzToNx / nxmap / nxmob / nxscan / nxchar）和
#    打包好的 bin\wztool.exe 都随仓库提供，不用编译任何东西，双击 bin\wztool.exe 即可。

# 2) 只有改了 src/wztool.py / webui / vendor 之后，才需要重新打包 wztool.exe：
#    双击根目录的 build.cmd（等价于 scripts\build.bat）——唯一的构建入口，四步全包：
#      ① 下载便携版 Python 到 .pyruntime\（项目内，不碰本机 Python；已就绪则跳过）
#      ② requirements.txt 里的依赖装进 .pylibs\（已就绪则跳过）
#      ③ bin\ 里缺原生工具就现编（有 VS 用的 C++ 工具集才行）
#      ④ PyInstaller onefile → bin\wztool.exe
build.cmd                        # 平时只需要这一条（双击也行）
#    全新机器 clone 下来直接双击就能编：连 Python 都不用预装，脚本自己下载
#    解释器（约 15MB）+ 依赖（约 200MB），全部落在项目目录，可随时删掉重来。
#    想从零重来：删 .pyruntime\ 和 .pylibs\ 再跑一次。

# 本地调试免打包：双击根目录 run.cmd —— 直接用源码起 WebUI（端口同 8000）。
#    前端 webui\cli.html 改完自动刷新；后端 src\wztool.py 改完重开一次 run.cmd 即可，
#    不用再等几分钟的 PyInstaller 打包。前提：先跑过一次 build.cmd（拉好 .pyruntime\.pylibs）。
.pyruntime\python.exe scripts\deps.py          # 只同步依赖、不打包（--check 只检查，--force 重装）

# 只想单独重编原生工具（改了 vendor\cpp 时）：
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\_compile-tools.ps1

# 3) CLI（推荐 / 自动化入口）
#    下面用 wztool 代指 bin\wztool.exe；把 bin\ 加进 PATH 后可以直接敲 wztool
bin\wztool --help                       # 全部子命令与示例（面向 AI/程序编写）
wztool workspace "D:\MapleStory\nxrun"   # 设置工作区
wztool mobs --json                      # 全部怪物（tier/level/hp/link/frames）
wztool mob-export 0100100               # 导出一只怪的全部动画帧（逐帧 PNG + manifest.csv）
wztool maps --json                      # 机器可读输出
wztool map-info 000050000               # 查地图尺寸/世界坐标/刷怪数（不渲染，秒级）
wztool map-export 000050000 --mobs      # 导出地图并把怪物画进预览图
wztool map-export 000050000 --no-bg     # 不画背景（天幕/云/山全跳过，PNG 透明），
                                        # 只出地形+建筑+物件 → preview_nobg.png
wztool map-minimap 101040000            # 游戏内小地图（canvas+传送门/刷怪点标记）→ PNG

# 3) Web 页面（可选，人看；页面只是 CLI 的可视化壳）
wztool web            # → http://127.0.0.1:8000  自动开浏览器
```

CLI 与 Web 共用同一套导出逻辑；所有子命令支持 `--json`，
退出码 0/1/2（成功 / 业务错误 / 参数错误），方便程序判定。

## 说明

- **不上传文件**：wz 动辄几百 MB，WebUI 只选本地目录路径，转换 / 导出全部在本机跑。
- **工作区约定**：所有原生工具按「当前目录 + 裸文件名」找 `Map.nx` / `Mob.nx`，
  所以转换和导出都以你选的目录为工作目录；`.nx` 永远生成在 `.wz` 旁边。
- **常见顺序**：`Map.wz` 必需；想把怪物画进画面/图鉴，再转 `Mob.wz`；
  想要怪物、地图名字显示出来，顺带转 `String.wz`（nxmob / 页面都会用它）。
- **缓存**：预览产物放 `.cache\`（可随时清：`wztool clear-cache` 或
  接口 `POST /api/export/clear-cache`），正式导出放 `.exports\`。
- 转换 `Map.wz`（约 640MB）需要几分钟，页面会显示进度日志。

## 怪物素材

- `wztool mob-export <id>`：把一只怪的全部动作（stand/move/attack/hit/die…）
  逐帧导出 PNG + `manifest.csv`（anim/frame/w/h/**ox/oy**/delay）；
  `ox/oy` 是锚点（脚底中心），把各帧对齐到同一画布就得到游戏内的真实包围盒。
- 链接怪（`info/link`、自身无帧）自动顺链导出目标怪，返回 JSON 带 `link` 字段。
- Web 页面「👾 怪物素材」：按 id/名字搜索全部怪物（可筛 BOSS / 大BOSS），
  右侧按锚点对齐展示每个动作的逐帧网格 + 动画播放器（可切动作、1x/2x/4x 缩放，
  蓝线=站地线），一键导出到 `.exports/mobs/<id>/`。
- 地图详情的「怪物图鉴」点卡片也可直接跳进素材查看器。

## 人物素材（角色）

和怪物、地图并列的第三块：把所有人物外观素材（肤色 / 发型 / 脸 / 帽子 / 上衣 /
裤裙 / 套服 / 鞋 / 手套 / 披风 / 盾牌 / 武器）拆开自由组合，实时合成动画。

- 底层 `nxchar.exe`（新增，`vendor/cpp/nxchar.cpp`）只负责把一帧拆成图层：
  `nxchar list <部位>` 列素材（id+名字），`nxchar icon` 出列表缩略图，
  `nxchar dump` 输出每层 PNG + `origin` / `map` 锚点 + 每帧 `delay`
  （`synchronized` 与客户端 `Character/Look/*.cpp` 一致）。
- 合成在 Python 里做：按 `map` 锚点把部件对到身体 / 头 / 手（navel / neck / brow /
  hand / handMove），再按 `CharLook::draw` 的图层顺序叠（头发→头→脸→帽子→武器…），
  所以换装不会出现头飞出去、衣服穿反这类问题。
- Web 页面「🧑 人物素材」：左边选部位、搜素材（带缩略图），右边实时看动画
  （站立 / 走 / 警戒 / 跳 / 趴 / 爬绳，1x~8x 缩放），一键导出到
  `.exports/chars/<slug>/`（逐帧 PNG + `manifest.csv` + `look.json`）。
- CLI 等价：`wztool char-parts hair`、`wztool char-export --skin 0 --hair 00030000
  --face 00020000 --coat 01040000 --pants 01060000 --shoes 01070000`。
- 需要工作区里有 `Character.nx`（由 `Character.wz` 转换而来，约 380MB）。
  注意：这套 WZ 的武器帧是**横放**的原始素材，这里按图层位置直出、没有做手部角度旋转。

## 怪物 / BOSS 判定

怪物的 BOSS 档位只用 WZ 官方字段判定（`nxmob info` 一次全量导出）：
`info/boss=1` 算 BOSS，再按 **maxHP** 分档——**大 BOSS** = maxHP ≥ 1000 万
（Zakum / Horntail / Pink Bean 一档），其余 BOSS 次一档。

- 地图详情的「刷怪点」按档位着色（红=普通怪 / 橙=BOSS / 品红=大BOSS）；
- 「怪物图鉴」卡片上同样标 BOSS / 大BOSS；
- **合成怪物时大 BOSS 一律不出现**（只刷大 BOSS 的图会得到没有怪的画面），
  高阶 BOSS 与普通怪照常出现；
- 状态图标、检查用假怪按 id 段和名字剔除。

## 地图

- 地图列表可按「全部 / 有怪 / 无怪」筛（有无怪 = life 刷怪表里有没有 type=m 的条目，
  这正是客户端进图刷怪的机制；脚本刷的大 boss 图会被算成"无怪"）。
- **链接图**：`info/link` 指向另一张图、自己的 img 是空的（任务图 / 副本变种图
  很常见），预览时自动渲染目标图并标注「画面同 xxx」。
- 地图详情可勾选**显示刷怪点**：在预览图上标出每个出生点
  （红=普通怪 / 橙=BOSS / 品红=大BOSS，悬停看怪名）。
- **游戏内小地图**：客户端 `UIMiniMap` 用的就是 WZ 里每张图自带的
  `miniMap/canvas`（预烘焙缩略图）+ `centerX/centerY/mag` 做坐标换算
  （`画布像素 = (世界坐标 + 中心偏移) / 2^mag`）。本工具照搬同一套算法：
  - `map.json` 现在带 `miniMap`（canvas PNG 路径与换算参数）、
    `name`/`streetName`（String.wz 的地图名），每个 portal 还带 `tmn`（目标图名）；
  - `wztool map-minimap <id>` 输出合成好的小地图 PNG（canvas 放大 + 
    传送门蓝圈 / NPC 黄点 / 刷怪点红橙品红）；
  - Web 页面地图详情：详情面板里是一块固定 **1366×768 的「游戏窗口」**
    （= 客户端画面 = 唯一导出尺寸），整张 1:1 地图是窗口里的**画布**：
    按住拖动 / 滚轮平移（不能缩放），窗口固定不动 —— 就是游戏里人物走动时
    背景横向滚动的效果。窗口 CSS 固定 1366×768，屏幕上的物理大小 = 1366×系统缩放
    （浏览器自然处理 DPI），与真实客户端窗口在屏幕上的大小一致；导出 都是窗口里的画面（传送门蓝圈点击直接跳目标图，NPC / 刷怪点可开关）；
  - 还有「**小地图 HUD**」：把小地图窗口按客户端的画法直接贴在**游戏窗口**上，
    固定不随画布移动。
    位置/尺寸严格照源码——`UIMiniMap` 继承 `UIDragElement<PosMINIMAP>`，    位置 = 设置项 `PosMINIMAP`（**默认 `(0,0)` 即视口左上角**，拖动后写回设置）；
    窗口尺寸 = `update_canvas()` 算的 `max(178, canvas宽+20)`，canvas 画在
    `(max(10, 窗宽/2 - canvas宽/2), 20)`。页面里同样可拖动（等价改 PosMINIMAP）、
    双击归位。
- **截图找地图**：地图列表的「🖼 找图」按钮 → 弹窗里 Ctrl+V 粘贴游戏截图
  （截小地图或整窗都行）、也可拖入图片文件 → 与全库地图的 `miniMap/canvas`
  做 OpenCV 双向模板匹配（自动试缩放，模板过小按面积惩罚压假阳性），
  按相似度列出候选，点结果直接打开对应地图。小地图索引由
  `nxmap mini-all` 一次建好（`.cache/minimap/`，全库约 10 秒）。

## 目录

```
build.cmd               双击即构建（根目录入口，转调 scripts\build.bat）
run.cmd                 本地调试入口（源码直跑 WebUI，免打包；改完重开即可）
scripts\build.bat       构建主脚本：下解释器 → 装依赖 → 缺原生工具就编 → 打包 wztool.exe
scripts\_fetch-python.ps1   下载便携版 CPython 到 .pyruntime\（build.bat 自动调用）
scripts\_compile-tools.ps1  原生工具编译逻辑（内部脚本，build.bat 缺 exe 时自动调用）
scripts\deps.py         把 requirements.txt 装进 .pylibs\（打包前自动调用，可单独跑）
requirements.txt        Python 依赖（pyinstaller / pillow / numpy / opencv-headless）
src/wztool.py    CLI 主程序（bin\wztool.exe 由 PyInstaller 打包，含 CLI + web 两用）
vendor\          全部源码与依赖（NoLifeWzToNx、nxmap/nxmob/nxscan/nxchar、NoLifeNx、render_map、Mob.txt）
bin\             原生工具（随仓库提供，直接能用）+ 打包好的 wztool.exe（双击它 = WebUI）
webui\cli.html   Web UI 页面（wztool web 服务的前端，浏览/导出/图鉴都在这一页）
.build\          打包中间产物（wztool\ rsp\ 等），可随时整个删掉
.pylibs\         Python 依赖安装目录（由 requirements.txt 生成，可随时删掉重装）
.pyruntime\      便携版 CPython（由 build.cmd 自动下载，可随时删掉；删了会重新拉）
```

来源致谢：NoLifeWzToNx（Peter Atashian / Ryan Payton，AGPL）、NoLifeNx、
stb_image_write；怪物名表取自 HeavenMS 的 handbook。
