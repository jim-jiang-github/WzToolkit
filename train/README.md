# WzToolkit 怪物识别 YOLO 训练管线

一键从 WZ 数据训练「怪物种类 + 动作状态」识别模型，全程脚本化、可复现。

## 快速开始

```bat
train\run.bat                          :: 默认参数
train\run.bat --maps 500 --epochs 150  :: 自定义参数
```

或直接：

```bat
python train\overnight_run.py --maps 350 --win 4 --epochs 100 --imgsz 960 --tag ov
```

**必须在项目根目录运行**（`wztool.exe` 和 `yolov8n.pt` 都在根目录）。

## 依赖

- `pip install ultralytics`（会自动带 torch CPU 版）
- 根目录放 `yolov8n.pt` 预训练权重（联网环境首次运行会自动下载；
  离线/被墙时手动下载后放到根目录）

## 流程（overnight_run.py 编排）

| 步骤 | 脚本 | 说明 |
|---|---|---|
| 1. 批量导出 | `batch_export.py` | 随机选有怪的地图，每张导 N 个 1366×768 随机窗口（客户端同款分辨率）。正样本 `--mobs --yolo`（合成随机动作状态的怪 + 全部标注），负样本只 `--yolo`（无怪，防误检），占比 `--neg`（默认 15%） |
| 2. 切分 | `prep_split.py` | 摊平图收进 `exports/yolo/images/{train,val}`（8:2），重写 `dataset.yaml`。幂等：已分过的不动，新导的自动补进去 |
| 3. 训练 | overnight_run.py 内联 | YOLOv8n，CPU，`epochs/--imgsz/--batch` 可调，patience=25 早停 |
| 4. 弱类补数据 | `boost_weak.py` | val per-class AP50 < `--target`(0.90) 的类，找含这些怪的图多导窗口，重训（最多 `--rounds` 轮） |
| 5. 终验 | `predict_test.py` | 导出模型**没见过**的全新随机图 → 推理 → 画框截图 → 与 ground truth 对比精确率/召回率 |
| 6. 报告 | overnight_run.py 内联 | 写 `train/train_report.md`：每类 AP50 表 + 弱类清单 + 测试实测 |

## 常用参数（overnight_run.py）

- `--maps N` 选多少张可用地图（受探测结果限制，如 2026-11 时可用约 154 张）
- `--win N` 每张图导几个随机窗口（总量 = maps × win）
- `--epochs / --imgsz / --batch` 训练超参（CPU 建议 100 / 960 / 8）
- `--target 0.90` 弱类判定线；`--rounds 2` 最多训练几轮
- `--tag` 导出 seed 前缀（换 tag 可在同一数据集上追加新导出而不撞名）

## 重新训练 / 复现

- **同一批数据重训**：直接删 `train/runs/` 再跑 `run.bat`（导出与切分幂等，已有图不会重复导）
- **完全从零**：先清旧数据集再跑：

  ```bat
  rd /s /q exports\yolo
  rd /s /q train\runs
  train\run.bat
  ```

- 同参数导出的图 seed 固定，标注可复现；`--seed` 参数可指定合成种子

## 产物位置

- `train/train_report.md` 最终报告（指标 + 每类 AP50 + 测试实测）
- `train/runs/roundN/weights/best.pt` 模型（N = 训练轮次）
- `train/test_out/pred_*.png` 测试图带预测框截图、`match_report.json` 命中统计
- `train/progress.log` 全程进度日志
- `exports/yolo/` 数据集（images/labels + classes.json 类别注册表 + dataset.yaml）

## 历史备注

2026-11-05 之前管线脚本放在 `.cache/_check/`（会被 `wztool clear-cache` 连缓存一起删掉），已迁入本目录并进 git。`.cache/_check/` 下的旧副本仅保留至当次训练结束。
