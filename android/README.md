# 安卓监护端（Kotlin + Jetpack Compose）

树莓派「居家老人健康与安全监护系统」的**手机监护端**：子女在手机上远程查看老人的
心率 / 血氧 / 体温 / 室温湿度 / 活动状态，接收报警，并能**消音**或**长按触发求助**。

> 接口的唯一依据是 `docs/05-安卓通信协议.md`。本目录下任何代码与该文档冲突时，
> **以文档为准**，并且必须同步改文档（协议 §6）。

---

## 1. 这个 App 做了什么

| 页面 | 内容 | 用到的接口 | 刷新频率 |
| --- | --- | --- | --- |
| 监护（首页） | 心率 / 血氧 / 体温 / 室温湿度 / 活动状态 五张卡片；`active_alarms` 非空时红色横幅高亮；`sensor_failures` 非空时警告条；「消音」按钮 | `GET /api/v1/current`、`POST /api/v1/silence` | **4 秒** |
| 报警 | 当前报警横幅 + 报警事件列表（严重度降序 → 时间降序）+ 本次下发过的报警；**长按 1.5 秒**触发 `/sos`；「消音」 | `GET /api/v1/alarms`、`GET /api/v1/current`、`POST /api/v1/sos`、`POST /api/v1/silence` | **5 秒** |
| 关于 / 硬件 | 树莓派服务版本、在线时长、是否模拟模式、每个设备状态与失败次数、`/devices` 的接线说明（总线与物理脚） | `GET /api/v1/health`、`GET /api/v1/devices` | **10 秒** |
| 设置 | 树莓派地址（自动归一化）+ 可选 token，**持久化**；一键「测试连接」 | `GET /api/v1/health`（测试用） | 进页面读一次 |

**渲染硬要求**（协议 §4.1，代码里有单测守着）：

- JSON 的 `null` 一律显示 `--` / 「未知」，**绝不显示 0**（0 bpm 会吓到人）；
- `finger_detected == false` → 心率 / 血氧区域显示「请将手指放好」；
- `sensor_failures` 非空 → 警告色列出「X 传感器异常（连续 N 次）」；
- `motion_state == "unknown"` → 显示「未知」，**不推断成「无人」**；
- 请求失败不弹崩溃框：显示「连接中… / 连接失败」，**保留上次数据**并标注「数据可能已过期」。

---

## 2. 目录结构

```
android/
├── settings.gradle.kts / build.gradle.kts / gradle.properties / gradlew(.bat)
├── gradle/libs.versions.toml              # 版本集中管理（AGP/Kotlin/Compose/Retrofit…）
├── local.properties                       # sdk.dir（不入库）
├── tools/build.ps1                        # 本机构建/安装/看日志的一键脚本（纯 ASCII）
├── README.md
└── app/
    ├── build.gradle.kts
    ├── proguard-rules.pro
    └── src/
        ├── main/
        │   ├── AndroidManifest.xml                     # INTERNET 权限 + networkSecurityConfig
        │   ├── res/xml/network_security_config.xml     # 允许明文 HTTP（局域网必需）
        │   ├── res/values/{strings,themes,colors}.xml
        │   ├── res/drawable/ic_launcher_foreground.xml
        │   ├── res/mipmap-anydpi-v26/ic_launcher.xml
        │   └── java/com/dboycht/healthmonitor/
        │       ├── HealthMonitorApp.kt        # Application：建依赖容器
        │       ├── MainActivity.kt            # 唯一 Activity + 底部导航 + 生命周期轮询
        │       ├── data/                      # DTO / JSON 配置 / Retrofit 接口 / 仓库 / 地址归一化
        │       │   ├── MonitorDtos.kt             # 线上数据模型（全部可空 + 默认 null）
        │       │   ├── JsonConfig.kt              # kotlinx.serialization 配置（说明为什么不用 Gson）
        │       │   ├── MonitorApi.kt              # Retrofit 接口（协议 §3 一一对应）
        │       │   ├── MonitorApiResult.kt        # 成功/失败统一包装
        │       │   ├── MonitorRepository.kt       # 仓库接口 + Retrofit 实现 + 错误翻译
        │       │   ├── UrlNormalizer.kt           # 地址归一化
        │       │   ├── ApiClient.kt               # OkHttp + Retrofit 组装（8 秒超时）
        │       │   └── AppContainer.kt            # 手写依赖注入
        │       ├── domain/                    # 纯 Kotlin 业务规则（可单测）
        │       │   ├── AlarmCatalog.kt            # 报警码 → 中文名（协议 §4.4 抄表）
        │       │   ├── Severity.kt                # 严重度排序 + 颜色映射
        │       │   ├── Formatters.kt              # null 安全格式化（"--" 的唯一来源）
        │       │   └── MonitorModels.kt           # DTO → 领域模型映射
        │       ├── settings/AppSettings.kt    # DataStore 持久化（地址 + token）
        │       └── ui/
        │           ├── theme/Theme.kt
        │           ├── component/{MetricCard,SeverityBadge,ConnectionBar,AlarmBanner,SosButton}.kt
        │           ├── dashboard/{DashboardScreen,DashboardCards,DashboardViewModel}.kt
        │           ├── alarms/{AlarmsScreen,AlarmsViewModel}.kt
        │           ├── about/{AboutScreen,AboutViewModel}.kt
        │           └── settings/{SettingsScreen,SettingsViewModel}.kt
        └── test/java/com/dboycht/healthmonitor/     # JVM 单元测试（离线，不碰网络）
            ├── domain/{AlarmCatalogTest,FormattersTest,SeverityTest}.kt
            ├── data/{MonitorJsonParsingTest,UrlNormalizerTest}.kt
            ├── settings/AppSettingsTest.kt
            ├── ui/dashboard/{DashboardCardsTest,DashboardViewModelTest}.kt
            ├── ui/alarms/AlarmsViewModelTest.kt
            └── testing/{FakeMonitorRepository,JsonSamples}.kt
```

- **包名**：`com.dboycht.healthmonitor`（debug 变体是 `com.dboycht.healthmonitor.debug`）
- **版本**：AGP `8.13.0`、Kotlin `2.1.20`、Compose BOM `2025.04.01`、compileSdk/targetSdk `36`、minSdk `26`、JDK `17`（用 JDK 21 编译）

---

## 3. 怎么构建

### 3.1 前置

| 项 | 本机取值 |
| --- | --- |
| JDK | `C:\Program Files\Microsoft\jdk-21.0.8.9-hotspot` |
| Android SDK | `D:\Program\Android\SDK`（已写入 `local.properties`） |

### 3.2 本机唯一的构建方式（重要）

本机的 Gradle wrapper 下载被 TLS 解密代理阻断（`PKIX path building failed`），
**所以不要用 `gradlew.bat`**，改用已经解包好的 Gradle 8.13：

```powershell
cd D:\code\DeepSeekHarness\raspberry-health-monitor\android
$env:JAVA_HOME='C:\Program Files\Microsoft\jdk-21.0.8.9-hotspot'
& "$env:USERPROFILE\.gradle\wrapper\dists\gradle-8.13-bin\5xuhj0ry160q40clulazy9h7d\gradle-8.13\bin\gradle.bat" --no-daemon testDebugUnitTest assembleDebug
```

或者用随附脚本（等价，且能顺手装到手机上看日志）：

```powershell
powershell -File D:\code\DeepSeekHarness\raspberry-health-monitor\android\tools\build.ps1 -All
powershell -File D:\code\DeepSeekHarness\raspberry-health-monitor\android\tools\build.ps1 -Install
powershell -File D:\code\DeepSeekHarness\raspberry-health-monitor\android\tools\build.ps1 -Log
```

在**别的机器**上（网络正常）可以直接 `.\gradlew.bat testDebugUnitTest assembleDebug`，
`gradle-wrapper.jar` 与 `gradle-wrapper.properties` 已经一并放在工程里。

### 3.3 产物位置

| 产物 | 路径 |
| --- | --- |
| 单元测试报告 | `D:\code\DeepSeekHarness\raspberry-health-monitor\android\app\build\reports\tests\testDebugUnitTest\index.html` |
| debug APK | `D:\code\DeepSeekHarness\raspberry-health-monitor\android\app\build\outputs\apk\debug\app-debug.apk` |

---

## 4. 怎么连树莓派

1. **启动树莓派服务**（在树莓派上）：
   ```bash
   cd ~/raspberry-health-monitor/rpi
   python -m health_monitor serve --port 8080          # 需要鉴权时再加 --token <你的token>
   ```
2. **查树莓派 IP**：`hostname -I` → 记下 `192.168.x.x`。
3. **手机连同一个 WiFi**（不能是移动数据；很多路由器还开了「AP 隔离」，那样手机和树莓派
   互相看不见，需要在路由器里关掉）。
4. 打开 App → 底部「⚙ 设置」→ 填地址：
   - 只填 `192.168.1.20` → App 自动补成 `http://192.168.1.20:8080/`；
   - 已带端口/协议就原样使用（`http://192.168.1.20:9000/`、`https://pi.example.com/`）。
5. 点「测试连接」：成功会显示树莓派上的**服务版本号**；失败会给出具体原因
   （超时 / 拒绝连接 / 401 鉴权失败 / 404 版本太旧）。
6. 保存后回「♥ 监护」页，卡片会在 4 秒内出数。

> 装了 `--token` 的服务必须在设置页填 token，否则每个请求都会 401（协议 §2）。

---

## 5. 怎么打包 APK

```powershell
# debug APK（课设演示够用，可直接安装）
powershell -File D:\code\DeepSeekHarness\raspberry-health-monitor\android\tools\build.ps1 -Apk
# 或
& "<gradle 8.13 路径>\gradle.bat" --no-daemon assembleDebug
adb install -r D:\code\DeepSeekHarness\raspberry-health-monitor\android\app\build\outputs\apk\debug\app-debug.apk
```

- **release**：`assembleRelease` 也能出包，但工程里**没有配签名**（`isMinifyEnabled = false` 且
  未设置 `signingConfig`），所以产出的是未签名包、不能直接装。课设演示用 debug 包即可；
  真要发 release，需要自己生成 keystore 并在 `app/build.gradle.kts` 里加 `signingConfigs`。

---

## 6. 技术选型说明

| 选择 | 理由 |
| --- | --- |
| **Retrofit + kotlinx.serialization**（不用 Gson） | 协议 §2 要求"数值缺失一律是 JSON `null`，绝不是 0"。Gson 会把缺失的 `Double` 留成 `0.0`，界面上就出现「心率 0 bpm」——正是协议禁止的。本工程的 DTO 全部是 `Double?` + 默认 `null`，kotlinx.serialization 在**字段缺失**与**显式 null** 两种情况下都得到 `null`，并且类型严格（服务器把数字写成字符串会立刻报错，能第一时间发现接口改了）。 |
| **DataStore(Preferences)**（不用 SharedPreferences） | 地址/token 用 Flow 暴露，改完下一次轮询自动生效；不在主线程做磁盘 IO。 |
| **底部导航用 `rememberSaveable` 下标**（不引 Navigation-Compose） | 只有 4 个互不传参的顶层页面，少一个依赖少一个版本风险；旋转屏幕后仍记住当前页。 |
| **不引 material-icons-extended** | 单它就能让 debug APK 涨几十 MB；底部导航用符号（♥ ⚠ ⓘ ⚙）+ 文字。 |
| **手写依赖容器**（不引 Hilt） | 课设规模用不上 DI 框架，`AppContainer` 三个字段一眼看得懂。 |
| **不用动态取色（Material You）** | 「红色 = 紧急」必须在任何手机上一致，不能被系统主题改掉。 |

---

## 7. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `CLEARTEXT communication to 192.168.x.x not permitted` | 没配明文 HTTP。本工程已在 `AndroidManifest.xml` 的 `<activity>` 上设 `networkSecurityConfig`，并在 `res/xml/network_security_config.xml` 里 `cleartextTrafficPermitted="true"`。若自己新建 Activity，记得同样设置。 |
| 一直「连接中…」/「连接超时」 | 手机不在同一 WiFi；路由器开了 AP 隔离；树莓派服务没启动；IP 变了（建议在路由器里给树莓派绑定静态 IP）。 |
| 「鉴权失败（401）」 | 树莓派用了 `--token` 启动，设置页里要填同样的 token。 |
| 「接口不存在（404）」 | 树莓派上的服务版本太旧，更新 `rpi/` 代码后重启服务。 |
| 心率一直是 `--` | 真实场景是手指没放好或算法未收敛；配了 mock 数据时看「关于」页的「模拟模式」是否为「是」。 |
| 构建时下载依赖报 `PKIX path building failed` | 本机 TLS 解密代理的根证书不被 JDK 信任。`gradle.properties` 里已把 `javax.net.ssl.trustStore` 指向 `keyforge/.certs/local-truststore.p12`（本机专用，文件缺失时自动回退默认信任库）。 |

---

## 8. 单元测试覆盖（`testDebugUnitTest`，全离线）

| 组 | 文件 | 覆盖点 |
| --- | --- | --- |
| ① 报警码 → 中文文案 | `domain/AlarmCatalogTest.kt` | 协议 §4.4 的 14 个码逐一断言中文名与默认严重度；未知码标注并带出原码；`null`/空码不崩；服务器 `severity` 优先 |
| ② `null` 值格式化 | `domain/FormattersTest.kt` | 所有格式化入口 `null → "--"` 且**不含字符 `0`**；NaN/无穷/负时长按缺失处理；缺失时不显示单位；`finger_detected=false` → 「请将手指放好」；`motion_state="unknown"` → 「未知」；`sensor_failures` 非空才出警告；不依赖系统区域设置 |
| ③ 严重度排序/颜色 | `domain/SeverityTest.kt` | 0<1<2<3 可比大小；四个颜色互不相同、容器色更浅；越界值被夹取并有兜底色；事件「严重度降序 → 时间降序」；`active_alarms` 最高严重度用于横幅配色 |
| ④ 接口 JSON 解析 | `data/MonitorJsonParsingTest.kt` | 用协议里的**真实响应样本字符串**（含 `null` 字段与 `{}` 空对象）：`null` 必须是 `null` 而不是 `0.0`；`data` 整体缺失不崩；未知字段被忽略（协议 §6）；`history` 中 `code: null` 的事件被过滤；`/health`、`/devices`、`/silence`、`/sos` 逐一解析 |
| ⑤ 地址归一化 | `data/UrlNormalizerTest.kt` | `192.168.1.20 → http://192.168.1.20:8080/`；带端口/scheme 不动；`https` 不加 8080；粘贴的完整接口地址只留主机端口；非法输入返回 `null`；幂等；用 `HttpUrl` 真解析一次确认 Retrofit 能用 |
| 附加 | `ui/dashboard/DashboardCardsTest.kt`、`ui/*/[Dashboard\|Alarms]ViewModelTest.kt`、`settings/AppSettingsTest.kt` | 五张卡片的硬要求渲染；请求失败保留上次数据；连续失败停轮询；消音文案提醒「报警未解除」；SOS 成功/失败反馈；设置持久化与归一化 |

测试**不依赖真实网络**：HTTP 层用 `FakeMonitorRepository`，JSON 层直接用样本字符串喂给
与运行时同一个 `Json` 配置。

---

## 9. 还没在真机 / 真树莓派上验证过的部分

诚实清单（本机只跑了构建与单测，没有连接真机与真树莓派）：

1. **真实网络请求**：`/current`、`/alarms`、`/health`、`/devices`、`/silence`、`/sos`
   的实际 HTTP 往返（含 401、超时、服务端 500）**未在真机联调**；只在 JVM 单测里用假数据与样本字符串验证过解析与状态机。
2. **明文 HTTP 放行是否真的生效**：`network_security_config.xml` 与 `usesCleartextTraffic`
   是按 Android 9+ 要求配的，但**没有在真机上跑过一次真实明文请求**来确认。
3. **DataStore 落盘持久化**：单测用的是内存实现；「杀掉 App 再打开地址还在」需要在真机上确认。
4. **长按 1.5 秒 SOS 的真实手势与震动反馈**：手势逻辑写了，但**没在真机上按过**；
   震动依赖设备 `Vibrator`（模拟器通常没有，代码里已做安全跳过）。
5. **UI 布局/深色模式/不同屏幕尺寸的实际观感**：未在真机或模拟器上截图确认。
6. **release 签名与上架**：未配签名；也未验证 R8（当前 release 未开混淆）。
7. **后台省电行为**：轮询用 `repeatOnLifecycle(STARTED)` 实现，理论上 `onStop` 即停，
   但未用真机 + 电池统计验证过。
