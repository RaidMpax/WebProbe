# WebProbe 演示流程

面试演示建议使用规则策略，整个流程只访问本地 TestLab，不需要 API Key，也不会调用外部模型。

## 演示前

在项目根目录运行：

```powershell
.\run_webprobe.ps1
```

确认页面地址是 `http://127.0.0.1:8501`（如果端口被占用，以终端显示为准）。

## 3 分钟演示

1. 在“运行测试”选择“购物车”，再选 `cart_mixed_products_001`，执行策略选“规则策略（复查疑似故障）”。预算保持默认并启动。报告应显示无 Bug、断言通过，并能查看截图和 Playwright Trace。
2. 改选 `cart_wrong_total_001` 再运行。页面总价故意错误；Agent 重新打开本地页面、重放加商品步骤后才报告 `wrong_cart_total`。展开执行轨迹，指出两次页面状态和 Python 的复现核验。
3. 打开 Evaluation Dashboard，选“购物车（7 题）”，保留“Baseline + 当前规则对照”后运行。展示每题结论、混淆矩阵和未分类数。结果说明复查策略让两道疑似错价题完成了确认；它是规则策略结果，不是 LLM 成绩。
4. 任选一题打开 Trace 下载按钮，说明 JSON/JSONL 保存机器可读的结论和步骤，Trace 可用 Playwright Trace Viewer 复查浏览器动作。

运行第三步会重新实际跑 14 条本地 Case（7 条 Baseline + 7 条当前策略），通常比单题演示更慢。赶时间时可跳过，改为打开已有的 Evaluation Dashboard 记录。

## 约 45 秒项目介绍

> WebProbe 是一个基于 Playwright 和 LLM 工具调用的本地 Browser QA Agent 原型。我做了搜索、反馈表单、购物车和登录四类 TestLab，并准备了 30 条带人工 Ground Truth 的固定 Case。Agent 每一步观察页面、选择工具动作，再由 Python 做确定性断言；对疑似故障会在预算内重新操作验证。执行失败、超时和证据不足不会直接算网站 Bug。Evals 按 Case 统计 TP、FP、FN、TN，并保存步骤、截图和 Playwright Trace。当前规则策略与 Baseline 的本地对照已跑完；购物车 LLM 路径通过离线假响应测试，但我没有把它说成真实模型成绩。当前版本只面向本地 TestLab，尚未支持任意网站。

## 常见追问

**为什么评测器不直接相信 Agent 的结论？**

模型可能选错元素、遇到加载延迟，或把自己的操作失败当成网站故障。Evals 使用独立维护的固定预期和浏览器观察来判定；没有结论的 Case 单独记为未分类，不塞进 TN。

**这里的 Baseline 是什么？**

它是同一个固定数据集上的规则策略消融：Baseline 不复查首次异常，当前规则策略会在预算内重开页面并重放。它不是一个基础 LLM，所以不能据此声称 LLM 胜过 Baseline。

**30 条 Case 是否代表 30 种 Bug？**

不是。它们覆盖正常任务、页面变化和故障，但若干正例来自同一故障家族。它证明评测闭环可运行，不足以证明对未知网站的泛化能力。

**为什么主流程没有强行换成 LangGraph？**

当前循环的状态和停止条件很少，普通 Python 循环更容易追踪和调试。项目里另有 LangChain、LangGraph 学习练习；是否引入框架应由流程复杂度决定。

## 演示边界

- Streamlit 绑定 `127.0.0.1`，界面运行本地固定 Case。
- 搜索、表单、购物车可选 LLM 策略；登录目前使用规则策略。
- 固定规则结果与真实模型结果分开描述。模型策略只有在运行时配置 API 后才会发起请求；可能使用服务商额度或产生费用。
- `evals/reports/2026-11-16_rule_comparison.json` 保存四套规则对照的聚合数据、数据集哈希、源码哈希和限制说明。截图、Trace 与逐题原始运行记录保存在本地 `artifacts/`，该目录被 Git 忽略。
