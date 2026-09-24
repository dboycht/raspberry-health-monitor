# 截图证据（curve_demo.png）

`curve_demo.png` 是基础版动态曲线的**样张**，用下面这条命令生成：

```bash
cd basic
python3 run.py --replay --headless --save evidence/curve_demo.png
```

- 数据来自 `basic/data/sample_demo.csv`（**mock 合成数据**，不是真实测量）；
- 它的用途是说明"图上应该有什么"：标题里的最新读数、温度（红，左轴 ℃）与
  湿度（蓝，右轴 %）、横轴时间（秒）、底部两行状态行；
- 真机截图请自己跑 `python3 run.py --save real_curve.png` 生成，
  并在报告里注明采集时间与地点（**不要把这张合成图当成真实数据**）。
