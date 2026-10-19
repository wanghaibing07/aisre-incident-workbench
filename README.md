# AI SRE Incident Investigation Workbench

**让 AI Agent 协助 SRE 排查 Kubernetes 服务故障。** Agent 自动读取工作负载、服务路由和网关日志，分析故障证据、定位错误；工程师根据调查结果执行恢复并验证业务。

基于 HolmesGPT 的 AI SRE 故障调查工作台。重点解决的问题：**Pod 看起来正常，业务接口却失败时，如何从 Kubernetes 资源和日志中找到真正的故障点？**

## 为什么是 AI SRE（2026 趋势视角）

2026 年，"AI for Ops" 正在分化成两个不同的技术品类，面试时能说清楚这个区分很加分：

| | AIOps（2016 年的老词） | AI SRE（2024–2026 的新品类） |
|---|---|---|
| 技术底座 | 统计机器学习 | LLM Agent + 工具调用 |
| 处理阶段 | 告警**之前**：降噪、关联、去重 | 告警**之后**：取证、根因分析、修复建议 |
| 代表 | BigPanda、Dynatrace Davis | HolmesGPT、K8sGPT、Datadog Bits AI SRE |

本项目属于后者：**AI SRE 的故障后调查（post-alert investigation）**。

### 本项目验证了什么

1. **Agent 的价值不在"知道答案"，而在"会取证"**：T1 案例中，Agent 自主调用 Pod/Service/EndpointSlice 只读工具定位 Selector 不匹配——这正是 AI SRE 的定义动作：用工具获取新证据，而非复述已知信息。
2. **证据缺口是真实瓶颈**：T3 第一轮调查里，原生工具集只能圈定故障范围，定位不到端口 81/80 不匹配。这对应行业现状——2026 年 AI SRE 工具的差异化不在模型，而在**工具链的完备性**。补了一个日志工具后，第二轮调查直接命中根因。
3. **人机边界是刻意设计的**：Agent 只读、只调查，不碰恢复；命名空间限定；故障注入与恢复由人工执行。这对应 2026 年行业共识：AI 负责高频取证与初判，人保留最终决策权（human-in-the-loop）。

### 行业坐标

- HolmesGPT 2025 年 10 月进 CNCF Sandbox；Datadog Bits AI SRE 2025 年 12 月 GA；Resolve.ai 2026 年 2 月以 10 亿美元估值完成 A 轮——AI SRE 是 2026 年资本与社区同时下注的方向。
- New Relic 2026 AI 影响报告：用 AI 的团队排障快约 25%，事件关联降噪可压 MTTR 40~58%。本项目的 T1/T3 对照实验是同一逻辑的缩小版验证：**结构化取证流程 + 完备工具链 = 更快的根因定位**。

### 一句话面试版

"2026 年 AIOps 和 AI SRE 正在分化，我这个项目做的是 AI SRE 的告警后调查：用 HolmesGPT 的 Agent 能力，在 K8s 里对真实注入的故障做取证式排障。过程中发现原生工具链有证据缺口，我补了一个日志工具，把根因定位从'圈定范围'推进到'精确定位'。Agent 只读不写，恢复决策权在人手里。"

## 故障调查流程

```text
业务请求异常 → Agent 只读取证 → 对照服务路由与日志 → 定位故障 → 人工恢复 → HTTP 验证
```

AI SRE 在本项目中的实际能力是**辅助故障诊断**：自动调查、证据关联和原因判断；变更与恢复仍由工程师执行。

## 架构

```text
业务请求 → Service/t3-gateway:8080 → Nginx Gateway
                                   → Service/t1-web:80 → Web Pod

HolmesGPT → namespace 限定的只读取证工具 → Kubernetes API / 网关日志
```

实验环境：Kubernetes v1.36.4、HolmesGPT v0.43.0、Nginx 1.25，命名空间为 `incident-lab`。Agent 仅调查，故障注入和恢复由人工执行。

## 真实故障案例

| 案例 | 故障现象 | Agent 调查结果 |
|---|---|---|
| [T1：Service Selector 不匹配](docs/t1-investigation.md) | Pod Ready，但 Service 没有可用后端 | 根据 Pod 标签、Service Selector、EndpointSlice 定位匹配错误 |
| [T3：Gateway 上游端口错误](docs/t3-investigation.md) | `/healthz` 为 200，`/api/` 为 502 | 原有工具仅定位故障范围；增加日志工具后识别端口 81 与 Service 端口 80 不匹配 |

T3 的两轮调查使用相同故障描述，Agent 自主调用工具。**增强前只判断问题范围，增强后通过日志与 Service 端口对照识别具体错误。** 人工恢复配置后，业务返回 HTTP 200。

[查看技术复盘与对照结果](docs/technical-report.md)

## 仓库内容

- [toolsets/incident-lab-readonly.yaml](toolsets/incident-lab-readonly.yaml)：三个固定范围的只读调查工具，分别读取 Pod、服务路由和 Gateway 日志。
- [k8s/incident-lab.yaml](k8s/incident-lab.yaml)：Namespace、两个 Deployment、两个 Service 与 Gateway ConfigMap。
- [docs/architecture.md](docs/architecture.md)：业务调用链、取证过程与边界。
- [docs/technical-report.md](docs/technical-report.md)：T1/T3 关键证据、实验结果及工程取舍。

## 复现与验证

- Kubernetes 资源 YAML 已通过 `kubectl create --dry-run=client --validate=false` 客户端解析；全新集群部署验收尚未进行。
- 仓库提供业务配置、只读调查工具和案例记录。复现时需配置 HolmesGPT、Kubernetes 访问身份与模型服务。
- 两个案例的调查结果已有记录；Agent 未直接读取 Nginx ConfigMap，也未自动恢复服务。生产级可靠性与长周期指标仍需另行评估。

## 设计重点

复用 HolmesGPT 进行调查；围绕 T3 故障补充 Nginx 日志取证工具，使 Agent 从定位异常链路进一步识别错误端口。调查权限限定为只读，恢复操作由工程师控制。
