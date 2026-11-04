@echo off
rem ============================================================
rem  WzToolkit 怪物识别模型 · YOLO 训练全流程一键脚本
rem  流程：批量导出训练图 -> train/val 切分 -> YOLOv8n 训练
rem        -> 弱类分析补数据重训 -> 全新测试图终验 -> 训练报告
rem
rem  用法（在项目根目录双击或命令行运行）：
rem    run.bat                          默认参数：350图 x4窗口 100epochs 960尺寸
rem    run.bat --maps 500 --epochs 150  透传给 overnight_run.py 的参数
rem
rem  全部参数见：python train\overnight_run.py --help
rem  依赖：pip install ultralytics ；根目录需有 yolov8n.pt 预训练权重
rem  产物：train\train_report.md（报告） train\runs\（模型） train\test_out\（测试截图）
rem ============================================================
cd /d "%~dp0.."
python train\overnight_run.py %*
echo.
echo 训练流程结束。报告: train\train_report.md
pause
