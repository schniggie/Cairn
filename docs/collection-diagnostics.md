# 采集故障核查：RDAP 与 Amass

核查时间：2026-09-09。依据Kali权威运行库的实际记录、保存的输出和本机版本探测。本轮没有手工发起外部域名查询或枚举，没有调用研究模型；联网检索仅用于协议文档。

## RDAP：404被混同为执行故障（已修复分类）

历史source_run_1396、source_run_1397、source_run_1398报错为curl退出码22。对应task_5750、task_5751、task_5752的stderr包含HTTP404；抽查task_5751、task_5752保存的stdout均为JSON对象，errorCode为整数404。source_run_1424另有成功登记查询，说明并非curl整体不可用。

[RFC 7480 §5.3](https://www.rfc-editor.org/rfc/rfc7480.html#section-5.3)将404定义为没有满足查询的数据。它不能证明域名不存在、未注册或已注销。本次不自动改查父域、不扩大输入范围。

RDAP适配器版本更新为1.1.0：
- curl把最终HTTP状态通过write-out写到stderr末尾，stdout保留原响应体。机制参考[curl官方手册](https://curl.se/docs/manpage.html#-w)。
- 只有普通执行错误、真实退出码22、最终状态404、JSON对象的errorCode严格为整数404同时成立，才记录为not_found。
- 负向结果保存http_status和解释，不生成漏洞发现、不改写退出码。coverage_complete为false，避免据此删除已有资产。
- 同批下一条查询仍沿用原限流和取消机制；后续失败时合并前面的原始响应，保留异常类型与原因。
- 缺状态标记、HTML错误页、状态/正文矛盾、429、5xx、DNS/TLS错误、超时和取消仍按失败处理。
- 旧失败历史保持原样，本次不回写为成功；新的有效负向答案不再因这一404触发失败重试。正常刷新周期保留。

## Amass：已确认v5不兼容当前适配器（已加执行前检查，完整接入未完成）

实际选用/home/kali/.local/bin/amass，版本v5.0.0，来自现有用户目录优先查找规则；正式服务没有CAIRN_AMASS_BINARY覆盖。其版本命令退出0，但stderr报告libpostal transliteration数据加载失败，当前health只取版本，未显示这个警告。

系统/usr/bin/amass是另一份包装脚本，非交互版本探测会请求sudo密码；它不是当前适配器选中的文件。因此不能将系统包装脚本的失败误报成正在使用的Amass未安装。

source_run_1502和source_run_1503分别运行约180秒后超时，保存的stderr包含同类libpostal警告及进度输出；source_run_1488另有成功返回、零新增资产的记录。现有证据不足以证明libpostal警告导致超时，也不足以证明枚举已覆盖目标。

初次核查没有延长超时、改写成功状态、下载数据包、修改系统工具或删除旧任务。

后续已核对官方v5.0.0源码：
- [main.go](https://github.com/owasp-amass/amass/blob/v5.0.0/cmd/amass/main.go)在enum前检查/启动engine，最多等待60秒响应。
- [process_unix.go](https://github.com/owasp-amass/amass/blob/v5.0.0/cmd/amass/process_unix.go)用独立进程组启动后台engine；终止enum进程组不等于停止该engine。
- [enum/cli.go](https://github.com/owasp-amass/amass/blob/v5.0.0/internal/enum/cli.go)使用engine会话和进度；[subs/cli.go](https://github.com/owasp-amass/amass/blob/v5.0.0/internal/subs/cli.go)从数据库读取并输出域名。
- 当前LineDomainDiscoveryAdapter把enum stdout逐行当域名，因此当前v5不满足这个结果契约。空stdout无法用来证明零发现，不能把v5的成功退出当作旧接口已工作。

在独立网络命名空间、临时HOME和输出目录中以example.invalid进行有界测试：60.09秒后退出1，报告engine未响应，没有stdout。该命名空间没有启用本机环回，结果仅证明引擎启动路径会阻塞，不能归因为生产环境DNS、被动源或libpostal。测试句柄已终止；按临时HOME核查没有遗留的测试引擎进程。输出保存在/tmp/cairn-amass-offline-40eqc65v/。

Amass适配器现更新为1.1.0，health识别版本，v5及更高未支持版本明确报告不兼容，无法识别版本也不宣称可用。直接execute同样先检查，避免绕过调用方预检。实际Kali v5.0.0预检约0.12秒返回不兼容，没有启动enum。其他被动采集来源继续按原流程运行。旧版路径保持原有行为，仅以模拟输出回归，不宣称所有旧版本已经验证。

这解决的是不兼容版本继续执行和空输出被误当有效结果的问题。没有安装/切换Amass版本，没有接入v5 engine与数据库导出，没有停止可能被其他原任务共用的现有引擎。libpostal警告与生产超时的因果仍未确认；完整v5支持仍需处理项目会话归属、输出导出和独立引擎生命周期。

## 验证

- RDAP专项及原采集流程组合23项通过，包括真实curl连接仅监听127.0.0.1的HTTPS夹具，使用临时证书显式信任，未关闭TLS验证。
- tests/test_vuln_adapters.py与tests/test_vulnerability_api.py完整回归139项通过。
- 完整回归暴露3项过时测试预期；在修改前适配器代码上逐项复现后，更新现有4个适配器清单、31至38迁移清单及默认Git元数据任务预期。仍精确检查清单、任务范围和审批状态，没有删除断言或放松产品限制。
- 原研究工作台的134项后端与27组报告相关浏览器结果属于前一次交付，本轮没有重跑或合并计数。
- 此项推进总纲M1中的旧采集故障核查，不代表新研究Worker、白盒审计或黑白盒联动已经完成。

最新Amass检查补齐后，上述两个模块完整回归为150项通过；本机v5预检和仅打印版本的本地脚本夹具证明不会启动enum。正式服务已加载兼容性检查，未进行真实目标枚举验收。
