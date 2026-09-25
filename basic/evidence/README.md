# evidence/ —— 证据文件（真机曲线 / 原始数据 / 合成样张）

| 文件 | 是什么 | 是否实测 |
| --- | --- | --- |
| **`curve_real_111.png`** | **真机实测曲线**：2026-09-25 21:14~21:19 在树莓派 5 上连续采集 **111 个样本**（3 秒/点，成功 111 / 失败 0）后导出 | ✅ **真机实测**（真传感器） |
| **`真机采集_111样本.csv`** | 上面那张图的**原始数据**（111 行：时间戳 / 温度 / 湿度 / 状态） | ✅ 真机实测 |
| `curve_demo.png` | 用**合成演示数据**（`data/sample_demo.csv`）画的样张，只说明"图上应该有什么" | ⚠️ **合成数据，非实测** |

生成真机曲线的那两条命令（在树莓派上）：

```bash
cd basic
python3 run.py --no-plot --interval 3 --duration 330          # 真机采集 → CSV
python3 run.py --replay data/dht11_日期_时刻.csv --window 200 --save evidence/curve_real_111.png
```

> 报告里引用时请照上表区分：只有前两个是"真机实测"，`curve_demo.png` 是合成样张。
> 曲线图上的中文需要"拉丁 + 中文都全"的字体：仓库自带
> `basic/fonts/NotoSansCJK-Regular.ttc`（程序会自动加载；SIL OFL 1.1 许可），
> 所以把整个 `basic/` 文件夹拷到任意树莓派上都能出一样的中文图，
> 不必先 `sudo apt install fonts-noto-cjk`。
