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
   └─ exports\mobs\<id>\{逐帧 PNG, manifest.csv}   manifest 含 anim/frame/w/h/ox/oy/delay

wztool map-export
   └─ exports\maps\<id>\{map.json, img\, window.png}
                                    map.json 含 life（刷怪表）/ foothold / ladderRope / portal；
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
  接口 `POST /api/export/clear-cache`），正式导出放 `exports\`。
- 转换 `Map.wz`（约 640MB）需要几分钟，页面会显示进度日志。

## 怪物素材

- `wztool mob-export <id>`：把一只怪的全部动作（stand/move/attack/hit/die…）
  逐帧导出 PNG + `manifest.csv`（anim/frame/w/h/**ox/oy**/delay）；
  `ox/oy` 是锚点（脚底中心），把各帧对齐到同一画布就得到游戏内的真实包围盒。
- 链接怪（`info/link`、自身无帧）自动顺链导出目标怪，返回 JSON 带 `link` 字段。
- Web 页面「👾 怪物素材」：按 id/名字搜索全部怪物（可筛 BOSS / 大BOSS），
  右侧按锚点对齐展示每个动作的逐帧网格 + 动画播放器（可切动作、1x/2x/4x 缩放，
  蓝线=站地线），一键导出到 `exports/mobs/<id>/`。
- 地图详情的「怪物图鉴」点卡片也可直接跳进素材查看器。

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
build.ps1        一键编译（cl.exe 直接编，不依赖 msbuild）
wztool.py        CLI 主程序（wztool.exe 由 PyInstaller 打包，含 CLI + web 两用）
wztool.exe       编译好的独立 CLI（放项目根即可找到 bin\ 与 vendor\）
vendor\          全部源码与依赖（NoLifeWzToNx、nxmap/nxmob/nxscan、NoLifeNx、render_map、Mob.txt）
bin\             原生工具编译产物
static\cli.html  Web 页面（wztool web 服务的可视化壳，与 CLI 交互）
```

来源致谢：NoLifeWzToNx（Peter Atashian / Ryan Payton，AGPL）、NoLifeNx、
stb_image_write；怪物名表取自 HeavenMS 的 handbook。
