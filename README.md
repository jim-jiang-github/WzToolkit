# WzToolkit

把冒险岛的 `.wz` 资源文件**导入 → 转换 → 浏览 → 导出**的工具：
**CLI 优先**（`wztool`，供 AI / 其他程序直接调用），附带可选 Web 页面。

```
选一个放 .wz 的目录（工作区）
   │  wztool convert …  把选中的 .wz 转成 .nx（.nx 落在 .wz 旁边）
   ▼
wztool mobs / maps   列出怪物与地图（--json 全字段）
   ▼
wztool mob-export / map-export
   ├─ exports\mobs\<id>\*.png         逐帧 sprite（直接可用于训练合成 / 图鉴）
   └─ exports\maps\<id>\{map.json, img\, window.png}
                                    map.json 含 life（刷怪表）/ foothold / ladderRope；
                                    window.png = 1366×768 导出窗口（--view 选位置）
wztool map-minimap 101040000        渲染游戏内小地图（miniMap canvas + 标记）→ PNG
```

## 首次使用

```powershell
# 1) 编译四个原生工具（需要 VS 的 C++ 工具集；一次即可）
powershell -File build.ps1

# 2) CLI（推荐 / 自动化入口；已附带编译好的 wztool.exe）
wztool --help                           # 全部子命令与示例（面向 AI/程序编写）
wztool workspace "D:\MapleStory\nxrun"   # 设置工作区
wztool mobs --json                      # 机器可读输出
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
- **常见顺序**：`Map.wz` 和 `Mob.wz` 是最有用的两个；想要怪物名字出现在目录名里，
  顺带转一下 `String.wz`（nxmob 的 --named 会用它）。
- **缓存**：预览产物放 `.cache\`（可随时清：`wztool clear-cache` 或
  接口 `POST /api/export/clear-cache`），正式导出放 `exports\`。
- 转换 `Map.wz`（约 640MB）需要几分钟，页面会显示进度日志。

## 帧尺寸 / manifest.csv

WZ 里**每一帧的画布都是按自己的包围盒裁出来的**（同一只怪 stand_00 是 28×28、
stand_01 可能是 31×28、die 帧甚至 46×18），所以单看 PNG 尺寸会觉得"忽大忽小" ——
那是原图如此，不是导错。真正决定画面位置的是 `origin`（锚点，通常在脚底中心），
导出时会一并写进 `manifest.csv`：

```
id,name,anim,frame,file,w,h,ox,oy,delay
2130103,"Jr. Necki",stand,0,"stand_00.png",28,28,14,28,500
```

- `ox,oy`：PNG 左上角到锚点的像素距离。把每帧按 `(-ox,-oy)` 摆到同一张画布上，
  帧与帧才对得齐；这张公共画布的尺寸就是该动画**在客户端里的真实包围盒**。
- `delay`：该帧停留毫秒数（WZ 没写时是 0，客户端按默认 ~120ms 播）。

WebUI 预览默认 **1x = 客户端真实渲染尺寸**（缩放按钮切 2x/4x 只为看清楚），
播放器里蓝横线是站地线、蓝点是锚点像素 —— 要和游戏里量到的大小/识别框比对时直接用 1x。

## BOSS 识别

列表里 BOSS 会用颜色标出来，判定只用 WZ 官方字段（`nxmob info` 一次全量导出）：

- **info/boss=1** → BOSS（客户端画长血条、放 boss 音乐看的就是这个标记）；
  再按 **maxHP** 分档：
  - **大 BOSS**（深红底 + 红字 + 徽标）：maxHP ≥ 1000 万（Zakum / Horntail / Pink Bean 一档）
  - **BOSS**（暗橙底 + 橙字）：其余 boss 标记的（如 Mano、King Clang）
- 悬停任意一行可以看到 Lv / HP；列表上方有「全部 / BOSS / 大 BOSS」筛选。
- **链接怪**：`info/link` 指向另一只怪、自己没帧的（如 Greatest Oldies →
  Latest Hits Compilation），预览时自动导出目标怪并标注「画面同 xxx」，
  和客户端行为一致。
- 状态图标、检查用假怪（如 "A transparent item for checking elimination"）按 id 段
  和名字剔除，不算 BOSS。
- 地图详情的「刷怪」标签用同一套判定着色（大部分大 boss 是脚本刷的，`life` 里
  未必有，标签没色就是普通怪）。

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
    背景横向滚动的效果。窗口默认按系统 DPI 放大显示（可切 1x），导出 /
    导出YOLO 都是窗口里的画面（传送门蓝圈点击直接跳目标图，NPC / 刷怪点可开关）；
  - 还有「**小地图 HUD**」：把小地图窗口按客户端的画法直接贴在**游戏窗口**上，
    固定不随画布移动。
    位置/尺寸严格照源码——`UIMiniMap` 继承 `UIDragElement<PosMINIMAP>`，
    位置 = 设置项 `PosMINIMAP`（**默认 `(0,0)` 即视口左上角**，拖动后写回设置）；
    窗口尺寸 = `update_canvas()` 算的 `max(178, canvas宽+20)`，canvas 画在
    `(max(10, 窗宽/2 - canvas宽/2), 20)`。页面里同样可拖动（等价改 PosMINIMAP）、
    双击归位，下拉框可切 左上/右上/关闭。

## 目录

```
build.ps1        一键编译（cl.exe 直接编，不依赖 msbuild）
wztool.py        CLI 主程序（wztool.exe 由 PyInstaller 打包，含 CLI + web 两用）
wztool.exe       编译好的独立 CLI（放项目根即可找到 bin\ 与 vendor\）
vendor\          全部源码与依赖（NoLifeWzToNx、nxmap/nxmob/nxscan、NoLifeNx、render_map、Mob.txt）
bin\             原生工具编译产物
static\cli.html  Web 页面（wztool web 服务的可视化壳，与 CLI 交互）
```

来源致谢：NoLifeWzToNx（Peter Atashian / Ryan Payton，AGPL）、NoLifeNx、
stb_image_write；怪物名表取自 HeavenMS 的 handbook。
